"""Unit tests for grain-aware world-time matching."""

from __future__ import annotations

from datetime import UTC, datetime

from org_memory.services.temporality.grain import (
    expand_valid_from,
    fact_matches_as_of,
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
