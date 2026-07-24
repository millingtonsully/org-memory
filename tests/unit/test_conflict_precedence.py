"""Conflict ranking and precedence."""

from __future__ import annotations

from datetime import UTC, datetime

from org_memory.domain.fact_lifecycle import ConflictCandidate, rank_conflict_candidates
from org_memory.domain.proposals import (
    PRECEDENCE_AGENT_PROMOTE,
    PRECEDENCE_GROUND_TRUTH,
    precedence_rank,
)


def test_structured_field_outranks_newer_extraction() -> None:
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 6, 1, tzinfo=UTC)
    ranked = rank_conflict_candidates(
        [
            ConflictCandidate(
                claim_id="extract",
                object_text="Bob",
                confidence=0.99,
                latest_evidence_at=newer,
                updated_at=newer,
                created_by="extraction",
                evidence_count=1,
            ),
            ConflictCandidate(
                claim_id="structured",
                object_text="Alice",
                confidence=1.0,
                latest_evidence_at=older,
                updated_at=older,
                created_by="structured_field:ground_truth",
                evidence_count=1,
            ),
        ]
    )
    assert ranked[0].claim_id == "structured"


def test_agent_promote_between_structured_and_extraction() -> None:
    assert precedence_rank(created_by="structured_field:x", evidence_count=1) == PRECEDENCE_GROUND_TRUTH
    assert precedence_rank(created_by="agent_promote:user:1", evidence_count=1) == PRECEDENCE_AGENT_PROMOTE
    assert precedence_rank(created_by="extraction", evidence_count=1) < PRECEDENCE_AGENT_PROMOTE
