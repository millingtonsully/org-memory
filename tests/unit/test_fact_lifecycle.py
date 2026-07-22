"""Fact lifecycle transition helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from org_memory.domain.fact_lifecycle import FactStatus, transition_fact


def test_transition_active_to_retracted() -> None:
    row = SimpleNamespace(
        status=FactStatus.active.value,
        decided_by="",
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    transition_fact(row, FactStatus.retracted, "test")
    assert row.status == FactStatus.retracted.value
    assert row.decided_by == "test"


def test_invalid_transition_raises() -> None:
    row = SimpleNamespace(
        status=FactStatus.superseded.value,
        decided_by="",
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(ValueError):
        transition_fact(row, FactStatus.active, "nope")
