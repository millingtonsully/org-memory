"""Ranking helpers: RRF fusion and recency decay.

Pure functions with no side effects, used by RetrievalService.
"""

from __future__ import annotations

from datetime import UTC, datetime


def rrf_fuse(rank_lists: list[dict[str, int]], k: int = 60) -> dict[str, float]:
    """Merge rank lists with reciprocal rank fusion."""
    fused: dict[str, float] = {}
    for ranks in rank_lists:
        for item_id, rank in ranks.items():
            fused[item_id] = fused.get(item_id, 0.0) + 1.0 / (k + rank)
    return fused


def recency_multiplier(
    event_time: datetime,
    now: datetime | None = None,
    half_life_days: float = 90.0,
    min_decay: float = 0.3,
) -> float:
    """Exponential decay by document age, floored at min_decay."""
    now = now or datetime.now(UTC)
    age_days = max(0.0, (now - event_time).total_seconds() / 86400.0)
    return max(min_decay, 0.5 ** (age_days / half_life_days))
