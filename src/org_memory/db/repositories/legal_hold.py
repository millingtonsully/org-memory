from __future__ import annotations

from sqlalchemy.orm import Session

from org_memory.core.errors import NotFoundError
from org_memory.core.settings import get_settings
from org_memory.db.orm import Document, DocumentParticipant, LegalHold, utcnow


class LegalHoldRepository:
    def __init__(self, session: Session):
        self._session = session
        self._ws = get_settings().workspace_id

    def place(self, scope_type: str, scope_value: str, reason: str, placed_by: str) -> LegalHold:
        hold = LegalHold(
            workspace_id=self._ws,
            scope_type=scope_type,
            scope_value=scope_value,
            reason=reason,
            placed_by=placed_by,
        )
        self._session.add(hold)
        self._session.flush()
        return hold

    def release(self, hold_id: str, released_by: str) -> LegalHold:
        hold = self._session.get(LegalHold, hold_id)
        if hold is None or hold.workspace_id != self._ws:
            raise NotFoundError(f"legal hold not found: {hold_id}")
        hold.released_by = released_by
        hold.released_at = utcnow()
        return hold

    def active_holds(self) -> list[LegalHold]:
        return (
            self._session.query(LegalHold)
            .filter(
                LegalHold.workspace_id == self._ws,
                LegalHold.released_at.is_(None),
            )
            .all()
        )

    def is_held(self, doc: Document) -> bool:
        participant_person_ids: set[str] | None = None
        for hold in self.active_holds():
            if hold.scope_type == "doc" and hold.scope_value == doc.doc_id:
                return True
            if hold.scope_type == "source_system" and hold.scope_value == doc.source_system:
                return True
            if hold.scope_type == "person":
                if hold.scope_value in (doc.author_external_id, doc.author_email):
                    return True
                if participant_person_ids is None:
                    participant_person_ids = self._participant_person_ids(doc.doc_id)
                if hold.scope_value in participant_person_ids:
                    return True
        return False

    def _participant_person_ids(self, doc_id: str) -> set[str]:
        rows = (
            self._session.query(DocumentParticipant.person_id)
            .filter(
                DocumentParticipant.doc_id == doc_id,
                DocumentParticipant.person_id.isnot(None),
            )
            .all()
        )
        return {row.person_id for row in rows if row.person_id}
