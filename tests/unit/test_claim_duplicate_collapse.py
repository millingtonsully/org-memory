"""Unit tests for same-object claim collapse on add_claim."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from sqlalchemy.exc import IntegrityError

from org_memory.db.orm import Claim
from org_memory.db.repositories.graph.claims import GraphClaimsMixin
from org_memory.domain.fact_lifecycle import FactStatus


class _Repo(GraphClaimsMixin):
    def __init__(self) -> None:
        self._ws = "ws-dup"
        self._session = MagicMock()


def _claim(**kwargs) -> Claim:
    defaults = dict(
        claim_id="c1",
        workspace_id="ws-dup",
        subject_type="person",
        subject_id="p1",
        predicate="title",
        object_text="Engineer",
        confidence=0.8,
        status=FactStatus.active.value,
        evidence_doc_ids=["d1"],
        evidence_quotes=[],
        created_by="extraction",
        decided_by="automatic:confidence_gate",
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
        invalidated_at=None,
        time_grain="day",
        origin_subject_id="",
        superseded_by_claim_id="",
    )
    defaults.update(kwargs)
    return Claim(**defaults)


def test_collapse_live_claims_merges_evidence_and_supersedes() -> None:
    repo = _Repo()
    a = _claim(claim_id="a", evidence_doc_ids=["d1"], confidence=0.7)
    b = _claim(claim_id="b", evidence_doc_ids=["d2"], confidence=0.9)
    repo.latest_evidence_time = MagicMock(  # type: ignore[method-assign]
        return_value=datetime(2026, 2, 1, tzinfo=UTC)
    )
    repo.supersede_claim = MagicMock()  # type: ignore[method-assign]

    keeper = repo.collapse_live_claims_for_object([a, b])

    assert keeper.claim_id in {"a", "b"}
    assert set(keeper.evidence_doc_ids) == {"d1", "d2"}
    assert keeper.confidence == 0.9
    assert repo.supersede_claim.call_count == 1
    loser_id = "b" if keeper.claim_id == "a" else "a"
    assert repo.supersede_claim.call_args.args[0].claim_id == loser_id
    assert repo.supersede_claim.call_args.args[2] == "automatic:duplicate_collapse"


def test_add_claim_integrity_error_merges_into_raced_row() -> None:
    repo = _Repo()
    existing = _claim(claim_id="kept", evidence_doc_ids=["d1"])
    incoming = _claim(claim_id="new", evidence_doc_ids=["d2"])

    nested = MagicMock()
    nested.__enter__ = MagicMock(return_value=None)
    nested.__exit__ = MagicMock(return_value=False)
    repo._session.begin_nested.return_value = nested
    repo._session.flush.side_effect = IntegrityError("stmt", {}, Exception("uq"))

    with (
        patch.object(repo, "_live_claims_for_object_locked", side_effect=[[], [existing]]),
        patch.object(
            repo, "collapse_live_claims_for_object", return_value=existing
        ) as collapse,
        patch.object(
            repo, "_merge_incoming_into_claim", return_value=existing
        ) as merge,
    ):
        out = repo.add_claim(incoming)

    assert out is existing
    collapse.assert_called_once_with([existing])
    merge.assert_called_once()
    assert merge.call_args.args[1] is incoming


def test_active_claim_count_includes_duplicate_rows() -> None:
    repo = _Repo()
    repo._session.query.return_value.filter.return_value.scalar.return_value = 2
    assert repo.active_claim_count("person", "p1", "title") == 2
