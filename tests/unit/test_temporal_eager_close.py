"""Unit tests for eager exclusive close gates."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from org_memory.domain.fact_lifecycle import FactStatus
from org_memory.services.temporality.eager_close import (
    eager_close_claim_slot,
    eager_close_relationship_slot,
)


def test_eager_close_skips_non_exclusive_predicate() -> None:
    graph = MagicMock()
    claim = SimpleNamespace(
        status=FactStatus.active.value,
        predicate="team",  # multi-valued in ontology
        subject_type="person",
        subject_id="p1",
    )
    assert eager_close_claim_slot(graph, claim) == 0
    graph.active_claims_for_slot_locked.assert_not_called()


def test_eager_close_skips_proposed() -> None:
    graph = MagicMock()
    claim = SimpleNamespace(
        status=FactStatus.proposed.value,
        predicate="title",
        subject_type="person",
        subject_id="p1",
    )
    assert eager_close_claim_slot(graph, claim) == 0


def test_eager_close_supersedes_rival_title() -> None:
    graph = MagicMock()
    now = datetime(2026, 6, 1, tzinfo=UTC)
    winner = SimpleNamespace(
        claim_id="c-new",
        status=FactStatus.active.value,
        predicate="title",
        subject_type="person",
        subject_id="p1",
        object_text="Manager",
        confidence=0.9,
        evidence_doc_ids=["d2"],
        updated_at=now,
        created_by="extraction",
        valid_from=now,
    )
    loser = SimpleNamespace(
        claim_id="c-old",
        status=FactStatus.active.value,
        predicate="title",
        subject_type="person",
        subject_id="p1",
        object_text="Engineer",
        confidence=0.8,
        evidence_doc_ids=["d1"],
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_by="extraction",
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
    )
    graph.active_claims_for_slot_locked.return_value = [loser, winner]
    graph.latest_evidence_time.side_effect = (
        lambda docs: datetime(2026, 1, 1, tzinfo=UTC)
        if docs == ["d1"]
        else now
    )
    assert eager_close_claim_slot(graph, winner) == 1
    graph.supersede_claim.assert_called_once()
    args, kwargs = graph.supersede_claim.call_args
    assert args[0] is loser
    assert args[1] == "c-new"
    assert kwargs.get("valid_to") == now


def test_eager_close_relationship_skips_member_of() -> None:
    graph = MagicMock()
    rel = SimpleNamespace(
        status=FactStatus.active.value,
        relationship_type="member_of",
        from_type="person",
        from_id="p1",
    )
    assert eager_close_relationship_slot(graph, rel) == 0
