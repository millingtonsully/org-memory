"""Unit tests for rule-based temporal intent."""

from __future__ import annotations

from datetime import UTC, datetime

from org_memory.services.temporality import plan_temporal_query

_NOW = datetime(2026, 8, 1, tzinfo=UTC)


def test_default_and_current_cues() -> None:
    plan = plan_temporal_query("Who is Alice's manager?", now=_NOW)
    assert plan.status == "ok"
    assert plan.axis == "current"
    assert plan.as_of is None
    assert plan.believed_as_of is None


def test_world_month_resolves_as_of() -> None:
    plan = plan_temporal_query("What was Alice's title in March 2026?", now=_NOW)
    assert plan.status == "ok"
    assert plan.axis == "world"
    assert plan.as_of == datetime(2026, 3, 15, tzinfo=UTC)
    assert plan.grain == "month"
    assert plan.believed_as_of is None


def test_belief_with_month() -> None:
    plan = plan_temporal_query(
        "What did we report as Alice's title in March 2026?", now=_NOW
    )
    assert plan.status == "ok"
    assert plan.axis == "belief"
    assert plan.believed_as_of == datetime(2026, 3, 15, tzinfo=UTC)
    assert plan.as_of is None


def test_belief_without_point_is_ambiguous() -> None:
    plan = plan_temporal_query(
        "What did we think Alice's title was before the correction?", now=_NOW
    )
    assert plan.status == "ambiguous"
    assert plan.axis == "belief"


def test_world_soft_cue_without_date_is_ambiguous() -> None:
    plan = plan_temporal_query("Who was Alice's manager as of the reorg?", now=_NOW)
    assert plan.status == "ambiguous"
    assert plan.axis == "world"


def test_quarter_grain() -> None:
    plan = plan_temporal_query("What was the project status in Q1 2026?", now=_NOW)
    assert plan.status == "ok"
    assert plan.axis == "world"
    assert plan.grain == "quarter"
    assert plan.as_of == datetime(2026, 2, 15, tzinfo=UTC)
