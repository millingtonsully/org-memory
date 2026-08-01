"""Unit tests for extraction time grounding."""

from __future__ import annotations

from datetime import UTC, datetime

from org_memory.services.temporality import ground_fact_times

_T_REF = datetime(2026, 6, 15, tzinfo=UTC)


def test_default_uses_t_ref() -> None:
    grounded = ground_fact_times({}, t_ref=_T_REF)
    assert grounded is not None
    assert grounded.valid_from == _T_REF
    assert grounded.valid_to is None
    assert grounded.time_grain == "day"


def test_relative_weeks_ago() -> None:
    grounded = ground_fact_times(
        {"time_expression": "2 weeks ago"}, t_ref=_T_REF
    )
    assert grounded is not None
    assert grounded.valid_from == datetime(2026, 6, 1, tzinfo=UTC)
    assert grounded.time_grain == "day"


def test_explicit_iso_and_grain() -> None:
    grounded = ground_fact_times(
        {
            "valid_from": "2026-03-01",
            "valid_to": "2026-07-01",
            "time_grain": "month",
        },
        t_ref=_T_REF,
    )
    assert grounded is not None
    assert grounded.valid_from == datetime(2026, 3, 1, tzinfo=UTC)
    assert grounded.valid_to == datetime(2026, 7, 1, tzinfo=UTC)
    assert grounded.time_grain == "month"


def test_contradictory_window_rejected() -> None:
    grounded = ground_fact_times(
        {"valid_from": "2026-08-01", "valid_to": "2026-01-01"},
        t_ref=_T_REF,
    )
    assert grounded is None
