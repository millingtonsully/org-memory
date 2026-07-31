"""Load cited documents and graph records under viewer ACL.

Backs the read_source tool: agents pass ids they received in citations and get
the full underlying material. Each id resolves to an explicit outcome (ok,
not_found, forbidden) so callers can distinguish missing from denied.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from org_memory.core.settings import get_settings
from org_memory.db.orm import Chunk, Claim, Document, Relationship
from org_memory.db.repositories import GraphRepository
from org_memory.domain.models import Principal


class SourceReader:
    def __init__(self, session: Session):
        self._session = session
        self._graph = GraphRepository(session)

    def read(
        self,
        principal: Principal,
        *,
        document_ids: list[str] | None = None,
        record_ids: list[str] | None = None,
    ) -> dict:
        doc_ids = [d for d in (document_ids or []) if d.strip()]
        rec_ids = [r for r in (record_ids or []) if r.strip()]
        if not doc_ids and not rec_ids:
            raise ValueError(
                "read_source requires source_document_ids and/or source_record_ids"
            )

        sources: list[dict] = []
        outcomes: list[dict] = []
        if doc_ids:
            doc_result = self._read_documents(principal, doc_ids)
            sources.extend(doc_result["sources"])
            outcomes.extend(doc_result["outcomes"])
        if rec_ids:
            rec_result = self._read_records(principal, rec_ids)
            sources.extend(rec_result["sources"])
            outcomes.extend(rec_result["outcomes"])
        return {"sources": sources, "outcomes": outcomes}

    def _read_documents(self, principal: Principal, doc_ids: list[str]) -> dict:
        results: list[dict] = []
        outcomes: list[dict] = []
        viewer = principal.all_principals()
        workspace_id = get_settings().workspace_id
        for doc_id in doc_ids:
            doc = self._session.get(Document, doc_id)
            if doc is None or doc.deleted or doc.workspace_id != workspace_id:
                outcomes.append({"id": doc_id, "kind": "document", "outcome": "not_found"})
                continue
            if not (doc.org_visible or set(doc.allowed_principals) & set(viewer)):
                outcomes.append({"id": doc_id, "kind": "document", "outcome": "forbidden"})
                continue
            chunks = self._document_chunks(doc_id, "parent") or self._document_chunks(
                doc_id, "child"
            )
            results.append(
                {
                    "kind": "document",
                    "doc_id": doc.doc_id,
                    "source_type": doc.source_type,
                    "source_system": doc.source_system,
                    "title": doc.title,
                    "author_display_name": doc.author_display_name,
                    "event_time": doc.event_time.isoformat(),
                    "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
                    "deep_link": doc.deep_link,
                    "rendered_text": doc.rendered_text,
                    "chunks": [{"chunk_id": c.chunk_id, "text": c.text} for c in chunks],
                }
            )
            outcomes.append({"id": doc_id, "kind": "document", "outcome": "ok"})
        return {"sources": results, "outcomes": outcomes}

    def _document_chunks(self, doc_id: str, role: str) -> list[Chunk]:
        return (
            self._session.query(Chunk)
            .filter(
                Chunk.doc_id == doc_id,
                Chunk.deleted == False,  # noqa: E712
                Chunk.chunk_role == role,
            )
            .order_by(Chunk.chunk_index)
            .all()
        )

    def _read_records(self, principal: Principal, record_ids: list[str]) -> dict:
        results: list[dict] = []
        outcomes: list[dict] = []
        for record_id in record_ids:
            claim = self._session.get(Claim, record_id)
            if claim is not None:
                visible = self._graph.visible_evidence_doc_ids(
                    list(claim.evidence_doc_ids or []), principal
                )
                if not visible or len(visible) != len(set(claim.evidence_doc_ids or [])):
                    outcomes.append(
                        {"id": record_id, "kind": "claim", "outcome": "forbidden"}
                    )
                    continue
                results.append(
                    {
                        "kind": "claim",
                        "source_record_id": claim.claim_id,
                        "subject_type": claim.subject_type,
                        "subject_id": claim.subject_id,
                        "predicate": claim.predicate,
                        "object": claim.object_text,
                        "confidence": claim.confidence,
                        "status": claim.status,
                        "evidence_doc_ids": visible,
                        "evidence_quotes": [
                            q
                            for q in (claim.evidence_quotes or [])
                            if q.get("doc_id") in set(visible)
                        ],
                    }
                )
                outcomes.append({"id": record_id, "kind": "claim", "outcome": "ok"})
                continue

            rel = self._session.get(Relationship, record_id)
            if rel is None:
                outcomes.append(
                    {"id": record_id, "kind": "record", "outcome": "not_found"}
                )
                continue
            visible = self._graph.visible_evidence_doc_ids(
                list(rel.evidence_doc_ids or []), principal
            )
            if not visible or len(visible) != len(set(rel.evidence_doc_ids or [])):
                outcomes.append(
                    {"id": record_id, "kind": "relationship", "outcome": "forbidden"}
                )
                continue
            results.append(
                {
                    "kind": "relationship",
                    "source_record_id": rel.relationship_id,
                    "relationship_type": rel.relationship_type,
                    "from": {"type": rel.from_type, "id": rel.from_id},
                    "to": {"type": rel.to_type, "id": rel.to_id},
                    "confidence": rel.confidence,
                    "status": rel.status,
                    "evidence_doc_ids": visible,
                }
            )
            outcomes.append({"id": record_id, "kind": "relationship", "outcome": "ok"})
        return {"sources": results, "outcomes": outcomes}
