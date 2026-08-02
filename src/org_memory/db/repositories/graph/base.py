"""Shared GraphRepository state and evidence ACL helpers."""

from __future__ import annotations

from sqlalchemy.orm import Session

from org_memory.core.settings import get_settings
from org_memory.db.orm import Document
from org_memory.domain.models import Principal

# Viewer-visible documents for all-visible evidence checks (claims/edges/entities/paths).
VISIBLE_DOCS_SELECT = """
        SELECT d.doc_id
        FROM documents d
        WHERE d.workspace_id = :workspace_id
          AND d.deleted = false
          AND (
              d.org_visible = true
              OR d.allowed_principals && CAST(:viewer_principals AS text[])
          )
"""
VISIBLE_DOCS_CTE = f"visible_docs AS ({VISIBLE_DOCS_SELECT})"


def evidence_lateral_sql(alias: str) -> str:
    """LATERAL join collecting viewer-visible evidence docs for ``alias``."""
    return f"""
    CROSS JOIN LATERAL (
        SELECT array_agg(v.doc_id) AS doc_ids
        FROM visible_docs v
        WHERE v.doc_id = ANY({alias}.evidence_doc_ids)
    ) evidence
    """


def all_visible_sql(alias: str) -> str:
    """True when every evidence doc id is present in the lateral visible set."""
    return f"""
    cardinality({alias}.evidence_doc_ids) > 0
    AND cardinality(evidence.doc_ids) = (
        SELECT count(DISTINCT x) FROM unnest({alias}.evidence_doc_ids) AS x
    )
    """


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
