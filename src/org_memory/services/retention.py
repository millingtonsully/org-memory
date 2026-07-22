from __future__ import annotations

import contextlib
from datetime import timedelta

import structlog
from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from org_memory.core.errors import NotFoundError
from org_memory.core.settings import get_settings
from org_memory.db.orm import Document, DocumentVersion, utcnow
from org_memory.db.repositories import GraphRepository, LegalHoldRepository
from org_memory.ports.object_store import ObjectStore

logger = structlog.get_logger(__name__)


class RetentionService:
    def __init__(self, session: Session, object_store: ObjectStore):
        self._session = session
        self._objects = object_store
        self._holds = LegalHoldRepository(session)
        self._graph = GraphRepository(session)

    def purge_expired(self, batch_limit: int = 500) -> dict:
        """Purge up to batch_limit expired documents. No-op when retention is off."""
        settings = get_settings()
        if settings.retention_days <= 0:
            logger.info("retention.disabled", retention_days=settings.retention_days)
            return {"purged": 0, "held": 0, "enabled": False}

        cutoff = utcnow() - timedelta(days=settings.retention_days)
        candidates = (
            self._session.query(Document)
            .filter(
                Document.workspace_id == settings.workspace_id,
                Document.event_time < cutoff,
                Document.rendered_text != "",
            )
            .limit(batch_limit)
            .all()
        )

        purged = 0
        held = 0
        for doc in candidates:
            if self._holds.is_held(doc):
                held += 1
                continue
            self._purge_document(doc)
            purged += 1

        logger.info("retention.purge_completed", purged=purged, held=held, cutoff=str(cutoff))
        return {"purged": purged, "held": held, "enabled": True}

    def _purge_document(self, doc: Document) -> None:
        self._session.execute(sql("DELETE FROM chunks WHERE doc_id = :doc_id"), {"doc_id": doc.doc_id})
        self._graph.remove_document_evidence(doc.doc_id)
        version_keys = [
            v.blob_key
            for v in self._session.query(DocumentVersion).filter(DocumentVersion.doc_id == doc.doc_id).all()
        ]
        for key in {*version_keys, doc.raw_blob_key} - {""}:
            with contextlib.suppress(NotFoundError):
                self._objects.delete(key)
        doc.title = ""
        doc.rendered_text = ""
        doc.doc_metadata = {**doc.doc_metadata, "purged_at": utcnow().isoformat()}
        doc.deleted = True
        doc.updated_at = utcnow()
