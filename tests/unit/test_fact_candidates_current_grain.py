"""Hybrid fact_candidates current path binds grain-aware validity."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from org_memory.db.repositories.graph.search import GraphSearchMixin
from org_memory.domain.models import Principal


class _Repo(GraphSearchMixin):
    def __init__(self) -> None:
        self._ws = "ws-grain"
        self._session = MagicMock()
        self._session.execute.return_value.mappings.return_value = []


def test_fact_candidates_current_binds_now_and_day_grain() -> None:
    repo = _Repo()
    principal = Principal(
        principal_id="user:11111111-1111-1111-1111-111111111111",
        groups=[],
    )
    fixed_now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    with patch(
        "org_memory.db.repositories.graph.search.utcnow",
        return_value=fixed_now,
    ):
        repo.fact_candidates("title manager", principal, limit=5)

    params = repo._session.execute.call_args.args[1]
    assert params["as_of"] == fixed_now
    assert params["as_of_grain"] == "day"
    assert params["believed_as_of"] is None
    assert params["statuses"] == ["active"]


def test_fact_candidates_host_as_of_keeps_host_grain() -> None:
    repo = _Repo()
    principal = Principal(
        principal_id="user:11111111-1111-1111-1111-111111111111",
        groups=[],
    )
    as_of = datetime(2026, 3, 15, tzinfo=UTC)
    repo.fact_candidates(
        "title engineer",
        principal,
        limit=5,
        as_of=as_of,
        as_of_grain="month",
    )
    params = repo._session.execute.call_args.args[1]
    assert params["as_of"] == as_of
    assert params["as_of_grain"] == "month"
    assert params["statuses"] == ["active", "superseded"]


def test_fact_candidates_belief_only_binds_belief_as_world_point() -> None:
    repo = _Repo()
    principal = Principal(
        principal_id="user:11111111-1111-1111-1111-111111111111",
        groups=[],
    )
    believed = datetime(2026, 3, 15, tzinfo=UTC)
    repo.fact_candidates(
        "title engineer",
        principal,
        limit=5,
        believed_as_of=believed,
    )
    params = repo._session.execute.call_args.args[1]
    assert params["as_of"] == believed
    assert params["as_of_grain"] == "day"
    assert params["believed_as_of"] == believed
    assert params["statuses"] == ["active", "superseded"]
