"""Temporal truth helpers: grounding, intent plans, eager exclusive close.

Public entry points used by extraction apply and retrieve_context.
"""

from __future__ import annotations

from org_memory.services.temporality.diff import diff_fact_snapshots
from org_memory.services.temporality.grounding import ground_fact_times
from org_memory.services.temporality.intent import plan_temporal_query
from org_memory.services.temporality.types import (
    GroundedInterval,
    TemporalAxis,
    TemporalQueryPlan,
    TimeGrain,
)

__all__ = [
    "GroundedInterval",
    "TemporalAxis",
    "TemporalQueryPlan",
    "TimeGrain",
    "diff_fact_snapshots",
    "ground_fact_times",
    "plan_temporal_query",
]
