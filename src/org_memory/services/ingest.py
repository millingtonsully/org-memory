"""Ingest Change Envelopes into documents, parent/child chunks, and jobs.

Keyword search is live in the same transaction. Embedding (children only) and
extraction are enqueued for the worker when content changes; ingest never calls
a vendor on the request path. doc_id replays are idempotent; blob keys are
versioned per event. Collaboration rebuilds are enqueued with queue debounce.

Blob order: apply DB → on accepted only, object_store.put. If put fails, the
request transaction rolls back (no rows pointing at a missing blob). If put
succeeds and a later step fails before commit, best-effort delete the blob.
Stale envelopes return skipped_stale without archiving.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy.orm import Session

from org_memory.core.settings import get_settings
from org_memory.db.orm import Chunk, Document, DocumentParticipant, utcnow
from org_memory.db.repositories import (
    ConnectorStatusRepository,
    DocumentRepository,
    DocumentVersionRepository,
    GraphRepository,
    JobRepository,
    StaleEnvelopeError,
)
from org_memory.domain.jobs import JobType
from org_memory.domain.models import ChangeEnvelope, ChangeKind
from org_memory.ports.object_store import ObjectStore
from org_memory.services.chunking import chunk_document
from org_memory.services.entity_resolution import EntityResolutionService
from org_memory.services.structured_writers import (
    RegistryBackedStructuredFieldWriter,
    StructuredFieldWriter,
)

logger = structlog.get_logger(__name__)

IngestStatus = Literal["accepted", "skipped_stale"]


@dataclass(frozen=True)
class IngestResult:
    doc_id: str
    status: IngestStatus


def make_doc_id(source_system: str, external_id: str) -> str:
    return f"{source_system}:{external_id}"


class IngestService:
    def __init__(
        self,
        session: Session,
        object_store: ObjectStore,
        entity_resolution: EntityResolutionService,
        structured_writer: StructuredFieldWriter | None = None,
    ):
        self._session = session
        self._docs = DocumentRepository(session)
        self._versions = DocumentVersionRepository(session)
        self._connectors = ConnectorStatusRepository(session)
        self._jobs = JobRepository(session)
        self._graph = GraphRepository(session)
        self._objects = object_store
        self._entities = entity_resolution
        self._structured = structured_writer or RegistryBackedStructuredFieldWriter()

    def ingest_envelope(self, envelope: ChangeEnvelope, raw_payload: bytes) -> IngestResult:
        """Process one envelope in the caller transaction."""
        settings = get_settings()
        doc_id = make_doc_id(envelope.source_system, envelope.external_id)

        payload_hash = hashlib.sha256(raw_payload).hexdigest()[:12]
        blob_key = (
            f"{settings.workspace_id}/envelopes/{doc_id.replace(':', '/')}/"
            f"{envelope.event_time.strftime('%Y%m%dT%H%M%SZ')}-{payload_hash}.json"
        )
        result = self._apply_envelope(envelope, doc_id, blob_key, payload_hash, settings.workspace_id)
        if result.status != "accepted":
            return result
        archived = False
        try:
            self._objects.put(blob_key, raw_payload, "application/json")
            archived = True
            self._on_blob_archived(blob_key)
            return result
        except Exception:
            if archived:
                try:
                    self._objects.delete(blob_key)
                except Exception as cleanup_exc:
                    logger.error(
                        "ingest.blob_orphan",
                        blob_key=blob_key,
                        error=str(cleanup_exc),
                    )
            raise

    def _on_blob_archived(self, blob_key: str) -> None:
        """Called after object_store.put succeeds. Override in tests; keep empty in prod."""
        return

    def _apply_envelope(
        self,
        envelope: ChangeEnvelope,
        doc_id: str,
        blob_key: str,
        payload_hash: str,
        workspace_id: str,
    ) -> IngestResult:
        if envelope.change_kind == ChangeKind.delete:
            self._connectors.record_envelope(envelope.source_system, envelope.event_time)
            self._docs.tombstone(doc_id)
            self._graph.remove_document_evidence(doc_id)
            self._versions.record(doc_id, "delete", envelope.event_time, blob_key, payload_hash)
            logger.info("ingest.tombstoned", doc_id=doc_id)
            return IngestResult(doc_id=doc_id, status="accepted")

        if envelope.change_kind == ChangeKind.permission_change:
            try:
                self._docs.update_permissions(
                    doc_id,
                    envelope.org_visible,
                    envelope.allowed_principals,
                    envelope.event_time,
                )
            except StaleEnvelopeError as exc:
                logger.warning("ingest.stale_acl_skipped", doc_id=doc_id, reason=str(exc))
                return IngestResult(doc_id=doc_id, status="skipped_stale")
            self._connectors.record_envelope(envelope.source_system, envelope.event_time)
            self._versions.record(doc_id, "permission_change", envelope.event_time, blob_key, payload_hash)
            logger.info("ingest.acl_updated", doc_id=doc_id)
            return IngestResult(doc_id=doc_id, status="accepted")

        incoming = Document(
            doc_id=doc_id,
            workspace_id=workspace_id,
            source_system=envelope.source_system,
            external_id=envelope.external_id,
            source_type=envelope.source_type,
            title=envelope.title,
            rendered_text=envelope.text,
            author_external_id=envelope.author_external_id,
            author_display_name=envelope.author_display_name,
            author_email=envelope.author_email,
            event_time=envelope.event_time,
            updated_at=utcnow(),
            org_visible=envelope.org_visible,
            allowed_principals=envelope.allowed_principals,
            acl_event_time=envelope.event_time,
            parent_external_id=envelope.parent_external_id,
            deep_link=envelope.deep_link,
            doc_metadata=_document_metadata(envelope),
            raw_blob_key=blob_key,
            deleted=False,
        )
        existing = self._session.get(Document, doc_id)
        content_changed = (
            existing is None
            or existing.deleted
            or existing.rendered_text != envelope.text
            or existing.title != envelope.title
        )
        try:
            stored = self._docs.upsert_content(incoming)
        except StaleEnvelopeError as exc:
            logger.warning("ingest.stale_envelope_skipped", doc_id=doc_id, reason=str(exc))
            return IngestResult(doc_id=doc_id, status="skipped_stale")
        self._connectors.record_envelope(envelope.source_system, envelope.event_time)
        self._versions.record(doc_id, envelope.change_kind.value, envelope.event_time, blob_key, payload_hash)

        chunk_count = 0
        if content_changed:
            parents = chunk_document(envelope.text, title=envelope.title)
            chunks: list[Chunk] = []
            for parent in parents:
                parent_id = f"{doc_id}#p{parent.index}"
                chunks.append(
                    Chunk(
                        chunk_id=parent_id,
                        doc_id=doc_id,
                        workspace_id=workspace_id,
                        chunk_index=parent.index,
                        chunk_role="parent",
                        parent_chunk_id=None,
                        text=parent.text,
                        embedding=None,
                        embedding_model=None,
                        source_type=envelope.source_type,
                        title=envelope.title,
                        author_display_name=envelope.author_display_name,
                        event_time=envelope.event_time,
                        updated_at=utcnow(),
                        deep_link=envelope.deep_link,
                        org_visible=stored.org_visible,
                        allowed_principals=stored.allowed_principals,
                        deleted=False,
                    )
                )
                for child in parent.children:
                    chunks.append(
                        Chunk(
                            chunk_id=f"{doc_id}#{child.index}",
                            doc_id=doc_id,
                            workspace_id=workspace_id,
                            chunk_index=child.index,
                            chunk_role="child",
                            parent_chunk_id=parent_id,
                            text=child.text,
                            embedding=None,
                            embedding_model=None,
                            source_type=envelope.source_type,
                            title=envelope.title,
                            author_display_name=envelope.author_display_name,
                            event_time=envelope.event_time,
                            updated_at=utcnow(),
                            deep_link=envelope.deep_link,
                            org_visible=stored.org_visible,
                            allowed_principals=stored.allowed_principals,
                            deleted=False,
                        )
                    )
            self._docs.replace_chunks(doc_id, chunks)
            child_count = sum(1 for c in chunks if c.chunk_role == "child")
            chunk_count = child_count
            if child_count:
                self._jobs.enqueue(
                    JobType.embed_chunks,
                    {
                        "doc_id": doc_id,
                        "content_hash": hashlib.sha256(envelope.text.encode("utf-8")).hexdigest(),
                    },
                )
        else:
            self._docs.sync_chunk_metadata(
                doc_id,
                org_visible=stored.org_visible,
                allowed_principals=stored.allowed_principals,
                title=stored.title,
                author_display_name=stored.author_display_name,
                event_time=stored.event_time,
                deep_link=stored.deep_link,
                source_type=stored.source_type,
            )

        self._session.query(DocumentParticipant).filter(DocumentParticipant.doc_id == doc_id).delete(
            synchronize_session=False
        )
        for identity in envelope.source_identities():
            person_id = self._entities.observe_identity(envelope.source_system, identity)
            self._session.add(
                DocumentParticipant(
                    doc_id=doc_id,
                    workspace_id=workspace_id,
                    role=identity.role,
                    identity_kind=identity.identity_kind.value,
                    source_system=envelope.source_system,
                    external_id=identity.external_id,
                    display_name=identity.display_name,
                    emails=[email.model_dump() for email in identity.emails],
                    identifiers=[key.model_dump() for key in identity.identifiers],
                    person_id=person_id,
                    observed_person_id=person_id,
                )
            )

        if envelope.text.strip() and content_changed:
            self._jobs.enqueue(
                JobType.extract_graph,
                {
                    "doc_id": doc_id,
                    "content_hash": hashlib.sha256(envelope.text.encode("utf-8")).hexdigest(),
                },
            )

        written = self._structured.apply(
            self._session,
            doc_id=doc_id,
            fields=envelope.structured_fields,
        )
        self._jobs.enqueue(JobType.aggregate_collaboration_edges, {})

        logger.info(
            "ingest.upserted",
            doc_id=doc_id,
            chunks=chunk_count,
            content_changed=content_changed,
            structured_facts=len(written),
        )
        return IngestResult(doc_id=doc_id, status="accepted")


def _document_metadata(envelope: ChangeEnvelope) -> dict:
    """Merge connector metadata with structured_fields for later deterministic writers."""
    meta = dict(envelope.metadata)
    if envelope.structured_fields:
        meta["structured_fields"] = [field.model_dump(mode="json") for field in envelope.structured_fields]
    return meta
