"""Reconcile temporal fields when re-evidencing an existing fact row."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from org_memory.services.temporality.grain import normalize_grain
from org_memory.services.temporality.types import TimeGrain

# Finer grain ranks higher (prefer day over year when filling unknown).
_GRAIN_SPECIFICITY: dict[str, int] = {
    "unknown": 0,
    "year": 1,
    "quarter": 2,
    "month": 3,
    "day": 4,
}


class _TemporalFields(Protocol):
    valid_from: datetime | None
    valid_to: datetime | None
    time_grain: str


def merge_temporal_fields(existing: _TemporalFields, incoming: _TemporalFields) -> None:
    """Fill open ends and upgrade unknown grain; do not invent or overwrite windows.

    - ``valid_from`` / ``valid_to``: copy from incoming only when existing is None.
    - ``time_grain``: take incoming when existing is unknown, or when incoming is
      strictly finer (more specific). Never invent intervals.
    """
    if existing.valid_from is None and incoming.valid_from is not None:
        existing.valid_from = incoming.valid_from
    if existing.valid_to is None and incoming.valid_to is not None:
        existing.valid_to = incoming.valid_to

    existing_grain = normalize_grain(existing.time_grain)
    incoming_grain = normalize_grain(getattr(incoming, "time_grain", None))
    if _GRAIN_SPECIFICITY[incoming_grain] > _GRAIN_SPECIFICITY[existing_grain]:
        existing.time_grain = incoming_grain


def prefer_grain(a: TimeGrain | str | None, b: TimeGrain | str | None) -> TimeGrain:
    """Return the finer of two grains (for callers that need a pure helper)."""
    ga = normalize_grain(a)
    gb = normalize_grain(b)
    return ga if _GRAIN_SPECIFICITY[ga] >= _GRAIN_SPECIFICITY[gb] else gb
