"""SQL repositories package modules.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from org_memory.core.settings import get_settings
from org_memory.db.orm import (
    SynthesisTrace,
)


class SynthesisTraceRepository:
    def __init__(self, session: Session):
        self._session = session
        self._ws = get_settings().workspace_id

    def record(
        self,
        principal_id: str,
        tool: str,
        subject: str,
        model: str,
        input_doc_ids: list[str],
        output_text: str,
        tokens: int,
    ) -> str:
        trace = SynthesisTrace(
            workspace_id=self._ws,
            principal_id=principal_id,
            tool=tool,
            subject=subject,
            model=model,
            input_doc_ids=input_doc_ids,
            output_text=output_text,
            tokens=tokens,
        )
        self._session.add(trace)
        self._session.flush()
        return trace.trace_id

    def latest_reusable(
        self,
        *,
        tool: str,
        subject: str,
        input_doc_ids: list[str],
        max_age_seconds: int,
    ) -> SynthesisTrace | None:
        """Return newest trace for subject when evidence doc set matches and is fresh."""
        from datetime import UTC, datetime, timedelta

        rows = self.for_subject(tool, subject, limit=5)
        wanted = sorted(set(input_doc_ids))
        cutoff = datetime.now(UTC) - timedelta(seconds=max_age_seconds)
        for trace in rows:
            if sorted(set(trace.input_doc_ids or [])) != wanted:
                continue
            created = trace.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if created < cutoff:
                continue
            return trace
        return None

    def for_subject(self, tool: str, subject: str, limit: int = 20) -> list[SynthesisTrace]:
        return (
            self._session.query(SynthesisTrace)
            .filter(
                SynthesisTrace.workspace_id == self._ws,
                SynthesisTrace.tool == tool,
                SynthesisTrace.subject == subject,
            )
            .order_by(SynthesisTrace.created_at.desc())
            .limit(limit)
            .all()
        )
