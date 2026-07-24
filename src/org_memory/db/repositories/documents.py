"""SQL repositories package modules.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from org_memory.core.errors import NotFoundError
from org_memory.db.orm import (
    Chunk,
    Document,
    utcnow,
)


class StaleEnvelopeError(Exception):
    """Raised when an envelope is older than stored document state."""


class DocumentRepository:
    def __init__(self, session: Session):
        self._session = session

    def upsert_content(self, incoming: Document) -> Document:
        existing = self._session.get(Document, incoming.doc_id)
        if existing is None:
            self._session.add(incoming)
            return incoming

        if incoming.event_time < existing.event_time:
            raise StaleEnvelopeError(
                f"envelope event_time {incoming.event_time.isoformat()} is older than "
                f"stored {existing.event_time.isoformat()} for {incoming.doc_id}"
            )

        apply_acl = incoming.event_time >= existing.acl_event_time
        acl_changed = apply_acl and (
            existing.org_visible != incoming.org_visible
            or set(existing.allowed_principals) != set(incoming.allowed_principals)
        )
        # Envelope fields only; history fields stay put
        existing.source_type = incoming.source_type
        existing.title = incoming.title
        existing.rendered_text = incoming.rendered_text
        existing.author_external_id = incoming.author_external_id
        existing.author_display_name = incoming.author_display_name
        existing.author_email = incoming.author_email
        existing.event_time = incoming.event_time
        existing.parent_external_id = incoming.parent_external_id
        existing.deep_link = incoming.deep_link
        existing.doc_metadata = incoming.doc_metadata
        existing.raw_blob_key = incoming.raw_blob_key
        existing.deleted = False  # updates revive tombstones
        existing.updated_at = utcnow()
        if apply_acl:
            existing.org_visible = incoming.org_visible
            existing.allowed_principals = incoming.allowed_principals
        if acl_changed:
            existing.acl_version += 1
            existing.acl_event_time = incoming.event_time
        return existing

    def get(self, doc_id: str) -> Document | None:
        return self._session.get(Document, doc_id)

    def tombstone(self, doc_id: str) -> None:
        """Mark a document deleted. Raises NotFoundError if doc_id is unknown."""
        doc = self._session.get(Document, doc_id)
        if doc is None:
            raise NotFoundError(f"cannot tombstone unknown document: {doc_id}")
        doc.deleted = True
        doc.updated_at = utcnow()
        self._session.execute(
            sql("UPDATE chunks SET deleted = true WHERE doc_id = :doc_id"),
            {"doc_id": doc_id},
        )

    def update_permissions(
        self,
        doc_id: str,
        org_visible: bool,
        allowed_principals: list[str],
        event_time: datetime,
    ) -> None:
        """Update ACL without re-embedding."""
        doc = self._session.get(Document, doc_id)
        if doc is None:
            raise NotFoundError(f"cannot change permissions of unknown document: {doc_id}")
        if event_time < doc.acl_event_time:
            raise StaleEnvelopeError(
                f"permission_change event_time {event_time.isoformat()} is older than "
                f"stored acl_event_time {doc.acl_event_time.isoformat()} for {doc_id}"
            )
        doc.org_visible = org_visible
        doc.allowed_principals = allowed_principals
        doc.acl_version += 1
        doc.acl_event_time = event_time
        doc.updated_at = utcnow()
        self._session.execute(
            sql("""
                UPDATE chunks
                SET org_visible = :org_visible,
                    allowed_principals = :allowed_principals,
                    updated_at = now()
                WHERE doc_id = :doc_id
            """),
            {
                "doc_id": doc_id,
                "org_visible": org_visible,
                "allowed_principals": allowed_principals,
            },
        )

    def replace_chunks(self, doc_id: str, chunks: list[Chunk]) -> None:
        """Replace live chunks for a document. History is in blobs and document_versions."""
        # Clear child→parent links first so a bulk delete does not trip the self-FK.
        self._session.execute(
            sql("UPDATE chunks SET parent_chunk_id = NULL WHERE doc_id = :doc_id"),
            {"doc_id": doc_id},
        )
        self._session.execute(sql("DELETE FROM chunks WHERE doc_id = :doc_id"), {"doc_id": doc_id})
        for chunk in chunks:
            self._session.add(chunk)

    def sync_chunk_metadata(
        self,
        doc_id: str,
        *,
        org_visible: bool,
        allowed_principals: list[str],
        title: str,
        author_display_name: str,
        event_time: datetime,
        deep_link: str,
        source_type: str,
    ) -> None:
        self._session.execute(
            sql("""
                UPDATE chunks
                SET org_visible = :org_visible,
                    allowed_principals = :allowed_principals,
                    title = :title,
                    author_display_name = :author_display_name,
                    event_time = :event_time,
                    deep_link = :deep_link,
                    source_type = :source_type,
                    updated_at = now()
                WHERE doc_id = :doc_id
                  AND deleted = false
            """),
            {
                "doc_id": doc_id,
                "org_visible": org_visible,
                "allowed_principals": allowed_principals,
                "title": title,
                "author_display_name": author_display_name,
                "event_time": event_time,
                "deep_link": deep_link,
                "source_type": source_type,
            },
        )


