"""Temporal truth helpers: grounding, intent plans, eager exclusive close.

Import leaf modules directly when wiring graph repositories (avoid cycles
through ``intent_llm`` → spend → graph).
"""

from __future__ import annotations

from org_memory.services.temporality.diff import diff_fact_snapshots
from org_memory.services.temporality.grain import (
    fact_matches_as_of,
    normalize_grain,
    validity_as_of_sql,
)
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
    "fact_matches_as_of",
    "ground_fact_times",
    "normalize_grain",
    "plan_temporal_query",
    "validity_as_of_sql",
]
