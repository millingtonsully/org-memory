"""Spend-gated LLM assist when rule-based temporal intent is ambiguous."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from org_memory.core.errors import VendorAPIError
from org_memory.db.engine import session_scope
from org_memory.db.repositories import SpendRepository
from org_memory.services.temporality.grain import normalize_grain
from org_memory.services.temporality.types import TemporalAxis, TemporalQueryPlan

_SYSTEM = """\
You map organizational memory questions to a bi-temporal query plan.
Return a single JSON object with keys:
  axis: "current" | "world" | "belief"
  as_of: ISO-8601 timestamptz or null (world axis point)
  believed_as_of: ISO-8601 timestamptz or null (belief axis point)
  range_end: ISO-8601 timestamptz or null (second bound for snapshot diff)
  grain: "day" | "month" | "quarter" | "year" | "unknown"
  confidence: number from 0 to 1
  status: "ok" | "ambiguous"
  rationale: short machine-readable string

axis meanings:
- current: ask what is true now; leave as_of and believed_as_of null
- world: ask what was true in the world at a time; set as_of
- belief: ask what this system believed/recorded then; set believed_as_of

If the question mixes axes or lacks a resolvable time, set status=ambiguous.
For month-only cues use mid-month ISO and grain=month. Do not invent day precision.
"""


def assist_temporal_query(
    query: str,
    rule_plan: TemporalQueryPlan,
    *,
    synthesizer: Any,
    now: datetime | None = None,
) -> TemporalQueryPlan:
    """Call the synthesis vendor only after rules abstained.

    Raises ``SpendLimitError`` / ``VendorAPIError`` on hard failures (fail closed).
    Uncertain but well-shaped model output may still be ``status=ambiguous``.
    """
    clock = now or datetime.now(UTC)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=UTC)

    with session_scope() as spend_session:
        SpendRepository(spend_session).assert_under_hard_limit()

    user = (
        f"NOW: {clock.isoformat()}\n"
        f"RULE_PLAN: {json.dumps(rule_plan.to_diagnostics())}\n"
        f"QUERY: {query.strip()}"
    )
    raw, tokens = synthesizer.complete(_SYSTEM, user, json_object=True)
    with session_scope() as spend_session:
        SpendRepository(spend_session).record(
            "temporal_intent", "synthesis", synthesizer.model_name, tokens
        )

    payload = _parse_json_object(raw)
    plan = plan_from_llm_payload(payload, clock=clock)
    if plan is None:
        raise VendorAPIError(
            "temporal-intent",
            200,
            "model returned an invalid temporal plan shape",
            raw_response=raw,
        )
    return plan


def plan_from_llm_payload(
    payload: dict[str, Any],
    *,
    clock: datetime,
) -> TemporalQueryPlan | None:
    """Validate model JSON into a TemporalQueryPlan, or None if unusable."""
    del clock  # reserved for future relative resolution against assist clock
    axis_raw = str(payload.get("axis") or "").strip().lower()
    if axis_raw not in ("current", "world", "belief"):
        return None
    axis: TemporalAxis = axis_raw  # type: ignore[assignment]
    status_raw = str(payload.get("status") or "ambiguous").strip().lower()
    if status_raw not in ("ok", "ambiguous"):
        return None
    grain = normalize_grain(payload.get("grain"))
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None
    confidence = max(0.0, min(1.0, confidence))
    rationale = str(payload.get("rationale") or "llm_assist").strip()[:200]
    as_of = _parse_optional_instant(payload.get("as_of"))
    believed = _parse_optional_instant(payload.get("believed_as_of"))
    range_end = _parse_optional_instant(payload.get("range_end"))

    if status_raw == "ok":
        if axis == "world" and as_of is None and range_end is None:
            status_raw = "ambiguous"
            rationale = "llm_world_without_point"
            confidence = min(confidence, 0.3)
        elif axis == "belief" and believed is None and range_end is None:
            status_raw = "ambiguous"
            rationale = "llm_belief_without_point"
            confidence = min(confidence, 0.3)

    return TemporalQueryPlan(
        axis=axis,
        as_of=as_of if axis == "world" else None,
        believed_as_of=believed if axis == "belief" else None,
        range_end=range_end,
        grain=grain,
        confidence=confidence,
        status=status_raw,  # type: ignore[arg-type]
        rationale=rationale or "llm_assist",
    )


def _parse_json_object(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(
            raw.strip().removeprefix("```json").removesuffix("```").strip()
        )
    except json.JSONDecodeError as exc:
        raise VendorAPIError(
            "temporal-intent",
            200,
            "model returned non-JSON output",
            raw_response=raw,
        ) from exc
    if not isinstance(parsed, dict):
        raise VendorAPIError(
            "temporal-intent",
            200,
            "model returned JSON that is not an object",
            raw_response=raw,
        )
    return parsed


def _parse_optional_instant(raw: Any) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
