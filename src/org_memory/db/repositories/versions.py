"""SQL repositories package modules.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from org_memory.core.settings import get_settings
from org_memory.db.orm import (
    DocumentVersion,
)


class DocumentVersionRepository:
    def __init__(self, session: Session):
        self._session = session
        self._ws = get_settings().workspace_id

    def record(
        self, doc_id: str, change_kind: str, event_time: datetime, blob_key: str, payload_hash: str
    ) -> None:
        """Skip insert when the same payload_hash already exists."""
        existing = (
            self._session.query(DocumentVersion)
            .filter(
                DocumentVersion.doc_id == doc_id,
                DocumentVersion.payload_hash == payload_hash,
            )
            .first()
        )
        if existing is not None:
            return
        self._session.add(
            DocumentVersion(
                doc_id=doc_id,
                workspace_id=self._ws,
                change_kind=change_kind,
                event_time=event_time,
                blob_key=blob_key,
                payload_hash=payload_hash,
            )
        )

    def history(self, doc_id: str) -> list[DocumentVersion]:
        return (
            self._session.query(DocumentVersion)
            .filter(
                DocumentVersion.workspace_id == self._ws,
                DocumentVersion.doc_id == doc_id,
            )
            .order_by(DocumentVersion.event_time)
            .all()
        )

