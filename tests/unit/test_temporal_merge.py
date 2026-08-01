"""Unit tests for temporal field merge on re-evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from org_memory.services.temporality.merge import merge_temporal_fields, prefer_grain


def test_merge_fills_open_ends_and_upgrades_grain() -> None:
    existing = SimpleNamespace(
        valid_from=None,
        valid_to=None,
        time_grain="unknown",
    )
    incoming = SimpleNamespace(
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_to=datetime(2026, 7, 1, tzinfo=UTC),
        time_grain="month",
    )
    merge_temporal_fields(existing, incoming)
    assert existing.valid_from == incoming.valid_from
    assert existing.valid_to == incoming.valid_to
    assert existing.time_grain == "month"


def test_merge_does_not_overwrite_existing_window() -> None:
    existing = SimpleNamespace(
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_to=datetime(2026, 3, 1, tzinfo=UTC),
        time_grain="day",
    )
    incoming = SimpleNamespace(
        valid_from=datetime(2025, 1, 1, tzinfo=UTC),
        valid_to=datetime(2027, 1, 1, tzinfo=UTC),
        time_grain="year",
    )
    merge_temporal_fields(existing, incoming)
    assert existing.valid_from == datetime(2026, 1, 1, tzinfo=UTC)
    assert existing.valid_to == datetime(2026, 3, 1, tzinfo=UTC)
    assert existing.time_grain == "day"


def test_prefer_grain_finer_wins() -> None:
    assert prefer_grain("year", "month") == "month"
    assert prefer_grain("unknown", "day") == "day"
