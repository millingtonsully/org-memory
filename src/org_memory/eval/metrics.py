"""Retrieval evaluation metrics (gold labels only — never production truth)."""

from __future__ import annotations

from collections.abc import Sequence


def hit_at_k(ranked_ids: Sequence[str], relevant: set[str], k: int) -> float:
    """1.0 if any relevant id appears in the top-k ranked list, else 0.0."""
    if k < 1:
        raise ValueError("k must be >= 1")
    if not relevant:
        raise ValueError("relevant set must be non-empty")
    top = list(ranked_ids)[:k]
    return 1.0 if any(item in relevant for item in top) else 0.0


def recall_at_k(ranked_ids: Sequence[str], relevant: set[str], k: int) -> float:
    """Fraction of relevant ids recovered in the top-k ranked list."""
    if k < 1:
        raise ValueError("k must be >= 1")
    if not relevant:
        raise ValueError("relevant set must be non-empty")
    top = set(list(ranked_ids)[:k])
    return len(top & relevant) / len(relevant)


def precision_at_k(ranked_ids: Sequence[str], relevant: set[str], k: int) -> float:
    """Fraction of the top-k ranked list that is relevant."""
    if k < 1:
        raise ValueError("k must be >= 1")
    if not relevant:
        raise ValueError("relevant set must be non-empty")
    top = list(ranked_ids)[:k]
    if not top:
        return 0.0
    return sum(1 for item in top if item in relevant) / len(top)


def mean_reciprocal_rank(ranked_ids: Sequence[str], relevant: set[str]) -> float:
    """1/rank of the first relevant id, or 0.0 if none appear."""
    if not relevant:
        raise ValueError("relevant set must be non-empty")
    for index, item in enumerate(ranked_ids, start=1):
        if item in relevant:
            return 1.0 / index
    return 0.0
