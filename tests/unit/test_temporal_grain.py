"""Unit tests for grain-aware world-time matching."""

from __future__ import annotations

from datetime import UTC, datetime

from org_memory.services.temporality.grain import (
    expand_valid_from,
    fact_matches_as_of,
    resolve_validity_query_point,
)


def test_month_grain_fact_matches_early_in_month_point() -> None:
    """valid_from mid-month with grain=month still matches as_of=month start."""
    assert fact_matches_as_of(
        valid_from=datetime(2026, 3, 15, tzinfo=UTC),
        valid_to=None,
        fact_grain="month",
        as_of=datetime(2026, 3, 1, tzinfo=UTC),
        query_grain="unknown",
    )


def test_day_grain_fact_rejects_before_valid_from() -> None:
    assert not fact_matches_as_of(
        valid_from=datetime(2026, 3, 15, tzinfo=UTC),
        valid_to=None,
        fact_grain="day",
        as_of=datetime(2026, 3, 1, tzinfo=UTC),
        query_grain="unknown",
    )


def test_query_month_bucket_overlaps_short_day_window() -> None:
    assert fact_matches_as_of(
        valid_from=datetime(2026, 3, 20, tzinfo=UTC),
        valid_to=datetime(2026, 3, 25, tzinfo=UTC),
        fact_grain="day",
        as_of=datetime(2026, 3, 15, tzinfo=UTC),
        query_grain="month",
    )


def test_expand_valid_from_quarter() -> None:
    assert expand_valid_from(datetime(2026, 5, 20, tzinfo=UTC), "quarter") == datetime(
        2026, 4, 1, tzinfo=UTC
    )


def test_resolve_belief_only_uses_belief_instant_and_day_grain() -> None:
    believed = datetime(2026, 3, 15, tzinfo=UTC)
    now = datetime(2026, 8, 1, tzinfo=UTC)
    point, grain = resolve_validity_query_point(
        as_of=None,
        believed_as_of=believed,
        as_of_grain=None,
        now=now,
    )
    assert point == believed
    assert grain == "day"


def test_resolve_host_as_of_wins_over_belief() -> None:
    as_of = datetime(2026, 1, 1, tzinfo=UTC)
    believed = datetime(2026, 3, 15, tzinfo=UTC)
    point, grain = resolve_validity_query_point(
        as_of=as_of,
        believed_as_of=believed,
        as_of_grain="month",
        now=datetime(2026, 8, 1, tzinfo=UTC),
    )
    assert point == as_of
    assert grain == "month"


def test_resolve_current_defaults_to_now_day() -> None:
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    point, grain = resolve_validity_query_point(
        as_of=None,
        believed_as_of=None,
        as_of_grain=None,
        now=now,
    )
    assert point == now
    assert grain == "day"
