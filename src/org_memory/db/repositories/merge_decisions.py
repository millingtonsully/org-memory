"""SQL repositories package modules.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from org_memory.core.settings import get_settings
from org_memory.db.orm import (
    PersonMergeDecision,
    utcnow,
)


class PersonMergeDecisionRepository:
    """Append-only automatic identity decisions and reversals."""

    def __init__(self, session: Session):
        self._session = session
        self._ws = get_settings().workspace_id

    def add(
        self,
        subject_kind: str,
        a_id: str,
        b_id: str,
        verdict: str,
        confidence: float,
        reason: str,
        status: str,
        signals: list[str],
        evidence_fingerprint: str,
        decided_by: str = "",
    ) -> PersonMergeDecision:
        decision = PersonMergeDecision(
            workspace_id=self._ws,
            subject_kind=subject_kind,
            a_id=a_id,
            b_id=b_id,
            verdict=verdict,
            confidence=confidence,
            reason=reason,
            signals=signals,
            evidence_fingerprint=evidence_fingerprint,
            status=status,
            decided_by=decided_by,
            decided_at=utcnow(),
        )
        self._session.add(decision)
        self._session.flush()
        return decision

    def get(self, decision_id: str) -> PersonMergeDecision | None:
        decision = self._session.get(PersonMergeDecision, decision_id)
        if decision is not None and decision.workspace_id != self._ws:
            return None
        return decision

    def find_by_fingerprint(self, fingerprint: str) -> PersonMergeDecision | None:
        return (
            self._session.query(PersonMergeDecision)
            .filter(
                PersonMergeDecision.workspace_id == self._ws,
                PersonMergeDecision.evidence_fingerprint == fingerprint,
            )
            .order_by(PersonMergeDecision.created_at.desc())
            .first()
        )

    def recent(self, limit: int = 100) -> list[PersonMergeDecision]:
        return (
            self._session.query(PersonMergeDecision)
            .filter(PersonMergeDecision.workspace_id == self._ws)
            .order_by(PersonMergeDecision.created_at.desc())
            .limit(limit)
            .all()
        )


