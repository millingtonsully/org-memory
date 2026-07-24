"""Conflict ranking and precedence."""

from __future__ import annotations

from datetime import UTC, datetime

from org_memory.db.repositories.graph import GraphRepository
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


def test_supersede_slot_rivals_respects_precedence() -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    graph = GraphRepository.__new__(GraphRepository)
    graph._session = MagicMock()
    winner = SimpleNamespace(
        claim_id="w",
        subject_type="person",
        subject_id="p1",
        predicate="title",
        object_text="VP",
        created_by="agent_promote:user:1",
        evidence_doc_ids=["d1"],
    )
    lower = SimpleNamespace(
        claim_id="l",
        subject_type="person",
        subject_id="p1",
        predicate="title",
        object_text="IC",
        created_by="extraction",
        evidence_doc_ids=["d1"],
    )
    higher = SimpleNamespace(
        claim_id="h",
        subject_type="person",
        subject_id="p1",
        predicate="title",
        object_text="CEO",
        created_by="structured_field:ground_truth",
        evidence_doc_ids=["d1"],
    )
    graph.active_claims_for_slot_locked = MagicMock(return_value=[winner, lower, higher])
    superseded: list[str] = []

    def _supersede(rival, winner_id, decided_by):
        superseded.append(rival.claim_id)

    graph.supersede_claim = _supersede  # type: ignore[method-assign]
    leftover = GraphRepository.supersede_slot_rivals(graph, winner, "test")
    assert superseded == ["l"]
    assert [c.claim_id for c in leftover] == ["h"]
