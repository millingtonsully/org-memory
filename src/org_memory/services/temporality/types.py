"""Temporal query plans and grounded validity intervals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

TimeGrain = Literal["day", "month", "quarter", "year", "unknown"]
TemporalAxis = Literal["current", "world", "belief"]
PlanStatus = Literal["ok", "ambiguous"]


@dataclass(frozen=True)
class GroundedInterval:
    """World-time window produced by grounding against document event_time."""

    valid_from: datetime | None
    valid_to: datetime | None
    time_grain: TimeGrain
    time_expression: str = ""


@dataclass(frozen=True)
class TemporalQueryPlan:
    """How retrieve_context should filter structured time.

    Explicit client ``as_of`` / ``believed_as_of`` override this plan.
    """

    axis: TemporalAxis
    as_of: datetime | None = None
    believed_as_of: datetime | None = None
    range_end: datetime | None = None
    grain: TimeGrain = "unknown"
    confidence: float = 1.0
    status: PlanStatus = "ok"
    rationale: str = ""

    def to_diagnostics(self) -> dict:
        return {
            "axis": self.axis,
            "status": self.status,
            "grain": self.grain,
            "confidence": self.confidence,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "believed_as_of": (
                self.believed_as_of.isoformat() if self.believed_as_of else None
            ),
            "range_end": self.range_end.isoformat() if self.range_end else None,
            "rationale": self.rationale,
        }
