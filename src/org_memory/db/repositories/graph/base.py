"""Shared GraphRepository state and evidence ACL helpers."""

from __future__ import annotations

from sqlalchemy.orm import Session

from org_memory.core.settings import get_settings
from org_memory.db.orm import Document
from org_memory.domain.models import Principal


class GraphRepositoryBase:
    """Workspace-scoped session holder and all-visible evidence ACL."""

    def __init__(self, session: Session):
        self._session = session
        self._ws = get_settings().workspace_id

    @staticmethod
    def normalize_name(name: str) -> str:
        return " ".join(name.lower().split())

    def visible_evidence_doc_ids(self, evidence_doc_ids: list[str], principal: Principal) -> list[str]:
        """Intersect evidence with current document ACLs.
        """
        if not evidence_doc_ids:
            return []
        rows = (
            self._session.query(Document.doc_id)
            .filter(
                Document.workspace_id == self._ws,
                Document.doc_id.in_(evidence_doc_ids),
                Document.deleted == False,  # noqa: E712
                (
                    (Document.org_visible == True)  # noqa: E712
                    | Document.allowed_principals.overlap(principal.all_principals())
                ),
            )
            .all()
        )
        allowed = {row.doc_id for row in rows}
        return [doc_id for doc_id in evidence_doc_ids if doc_id in allowed]
