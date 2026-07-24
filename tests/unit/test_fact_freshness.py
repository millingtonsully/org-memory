"""Claim/edge freshness ranking helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from org_memory.services.ranking import fact_freshness_score, recency_multiplier


def test_recency_multiplier_halves_at_half_life() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    older = now - timedelta(days=90)
    score = recency_multiplier(older, now=now, half_life_days=90.0, min_decay=0.0)
    assert abs(score - 0.5) < 1e-9


def test_fact_freshness_score_uses_confidence_and_age() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    older = now - timedelta(days=180)
    score = fact_freshness_score(
        confidence=1.0,
        as_of_time=older,
        half_life_days=180.0,
        min_decay=0.1,
        now=now,
    )
    assert abs(score - 0.5) < 1e-9


def test_fact_freshness_missing_time_uses_min_decay() -> None:
    score = fact_freshness_score(
        confidence=0.8,
        as_of_time=None,
        half_life_days=180.0,
        min_decay=0.25,
    )
    assert abs(score - 0.2) < 1e-9
