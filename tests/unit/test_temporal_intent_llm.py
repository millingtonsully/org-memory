"""Unit tests for LLM temporal-intent payload parsing."""

from __future__ import annotations

from datetime import UTC, datetime

from org_memory.services.temporality.intent_llm import plan_from_llm_payload

_NOW = datetime(2026, 8, 1, tzinfo=UTC)


def test_ok_world_plan() -> None:
    plan = plan_from_llm_payload(
        {
            "axis": "world",
            "as_of": "2026-03-15T00:00:00+00:00",
            "believed_as_of": None,
            "range_end": None,
            "grain": "month",
            "confidence": 0.8,
            "status": "ok",
            "rationale": "llm_world",
        },
        clock=_NOW,
    )
    assert plan is not None
    assert plan.status == "ok"
    assert plan.axis == "world"
    assert plan.as_of == datetime(2026, 3, 15, tzinfo=UTC)
    assert plan.believed_as_of is None


def test_world_without_point_downgrades_to_ambiguous() -> None:
    plan = plan_from_llm_payload(
        {
            "axis": "world",
            "as_of": None,
            "status": "ok",
            "confidence": 0.9,
            "grain": "unknown",
        },
        clock=_NOW,
    )
    assert plan is not None
    assert plan.status == "ambiguous"
    assert plan.rationale == "llm_world_without_point"


def test_invalid_axis_rejected() -> None:
    assert (
        plan_from_llm_payload({"axis": "transaction", "status": "ok"}, clock=_NOW)
        is None
    )
