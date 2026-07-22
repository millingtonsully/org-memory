"""SQL repositories package modules.
"""

from __future__ import annotations

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from org_memory.core.errors import SpendLimitError
from org_memory.core.settings import get_settings
from org_memory.db.orm import (
    SpendEntry,
)


class SpendRepository:
    def __init__(self, session: Session):
        self._session = session

    def _lock_workspace_spend(self) -> None:
        self._session.execute(
            sql("SELECT pg_advisory_xact_lock(hashtext(:ws))"),
            {"ws": get_settings().workspace_id},
        )

    def record(self, job_class: str, vendor: str, model: str, tokens: int) -> None:
        settings = get_settings()
        self._lock_workspace_spend()
        used = self.tokens_used_this_month()
        if used + max(tokens, 0) > settings.spend_hard_limit_tokens_monthly:
            raise SpendLimitError(used, settings.spend_hard_limit_tokens_monthly)
        self._session.add(
            SpendEntry(
                workspace_id=settings.workspace_id,
                job_class=job_class,
                vendor=vendor,
                model=model,
                tokens=tokens,
            )
        )

    def tokens_used_this_month(self) -> int:
        return sum(self.totals_by_class_this_month().values())

    def assert_under_hard_limit(self) -> None:
        settings = get_settings()
        self._lock_workspace_spend()
        used = self.tokens_used_this_month()
        if used >= settings.spend_hard_limit_tokens_monthly:
            raise SpendLimitError(used, settings.spend_hard_limit_tokens_monthly)

    def totals_by_class_this_month(self) -> dict[str, int]:
        rows = self._session.execute(
            sql("""
                SELECT job_class, COALESCE(SUM(tokens), 0) AS total
                FROM spend_ledger
                WHERE workspace_id = :ws
                  AND created_at >= date_trunc('month', now())
                GROUP BY job_class
            """),
            {"ws": get_settings().workspace_id},
        ).fetchall()
        return {r.job_class: int(r.total) for r in rows}


