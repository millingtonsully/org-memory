"""Retrieval audit rows: who searched, with what query, and what came back."""

from __future__ import annotations

from sqlalchemy.orm import Session

from org_memory.core.settings import get_settings
from org_memory.db.orm import (
    AdminAudit,
    RetrievalAudit,
)
from org_memory.domain.models import Principal


class AuditRepository:
    def __init__(self, session: Session):
        self._session = session

    def record_retrieval(
        self,
        principal: Principal,
        tool: str,
        query: str,
        params: dict,
        chunk_ids: list[str],
        fact_ids: list[str] | None = None,
        memory_ids: list[str] | None = None,
    ) -> str:
        audit = RetrievalAudit(
            workspace_id=get_settings().workspace_id,
            principal_id=principal.principal_id,
            tool=tool,
            query=query,
            params=params,
            result_chunk_ids=chunk_ids,
            result_fact_ids=fact_ids or [],
            result_memory_ids=memory_ids or [],
        )
        self._session.add(audit)
        self._session.flush()
        return audit.audit_id

    def record_admin(
        self,
        principal: Principal,
        action: str,
        params: dict | None = None,
    ) -> str:
        """Append-only record of a mutating admin action. Never logs secrets."""
        audit = AdminAudit(
            workspace_id=get_settings().workspace_id,
            principal_id=principal.principal_id,
            action=action,
            params=params or {},
        )
        self._session.add(audit)
        self._session.flush()
        return audit.audit_id

    def recent(self, principal_id: str | None, limit: int = 50) -> list[RetrievalAudit]:
        q = self._session.query(RetrievalAudit).filter(
            RetrievalAudit.workspace_id == get_settings().workspace_id
        )
        if principal_id:
            q = q.filter(RetrievalAudit.principal_id == principal_id)
        return q.order_by(RetrievalAudit.created_at.desc()).limit(limit).all()

    def recent_admin(self, limit: int = 50) -> list[AdminAudit]:
        return (
            self._session.query(AdminAudit)
            .filter(AdminAudit.workspace_id == get_settings().workspace_id)
            .order_by(AdminAudit.created_at.desc())
            .limit(limit)
            .all()
        )


# Ontology and governance


