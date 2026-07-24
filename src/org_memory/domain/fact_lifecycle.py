"""Explicit state machine for extracted relationships and claims.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol

from org_memory.domain.proposals import precedence_rank


class FactStatus(str, Enum):
    proposed = "proposed"
    active = "active"
    superseded = "superseded"
    retracted = "retracted"


class FactRow(Protocol):
    status: str
    decided_by: str
    updated_at: datetime


_ALLOWED_TRANSITIONS = {
    FactStatus.proposed: {FactStatus.active, FactStatus.retracted},
    FactStatus.active: {FactStatus.superseded, FactStatus.retracted},
    FactStatus.superseded: set(),
    FactStatus.retracted: {FactStatus.proposed, FactStatus.active},
}


def status_for_confidence(confidence: float, activation_threshold: float) -> FactStatus:
    return FactStatus.active if confidence >= activation_threshold else FactStatus.proposed


def transition_fact(row: FactRow, target: FactStatus, decided_by: str) -> None:
    current = FactStatus(row.status)
    if target == current:
        return
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"Invalid fact lifecycle transition: {current.value} -> {target.value}")
    row.status = target.value
    row.decided_by = decided_by
    row.updated_at = datetime.now(UTC)


# A subject can legitimately hold many values for one predicate (eg "member
# of" many teams), so a same-predicate collision isn't automatically a
# contradiction. Only a mutually exclusive predicate has a single current
# value. When a slot is exclusive, the winner is picked by precedence then
# evidence recency — never by the model's guess about which is "right".
_EPOCH = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class ConflictCandidate:
    """One competing value for a mutually-exclusive (subject, predicate) slot."""

    claim_id: str
    object_text: str
    confidence: float
    latest_evidence_at: datetime | None
    updated_at: datetime
    created_by: str = ""
    evidence_count: int = 0


def rank_conflict_candidates(candidates: list[ConflictCandidate]) -> list[ConflictCandidate]:
    """Order competing values so the current winner is first.

    Precedence (structured_field > agent_promote > multi-evidence extraction >
    single-evidence) wins first. Evidence recency, confidence, and claim_id
    break ties. Selection is fully deterministic and independent of input order.
    """
    return sorted(
        candidates,
        key=lambda candidate: (
            precedence_rank(
                created_by=candidate.created_by,
                evidence_count=candidate.evidence_count,
            ),
            candidate.latest_evidence_at or _EPOCH,
            candidate.confidence,
            candidate.updated_at,
            candidate.claim_id,
        ),
        reverse=True,
    )
