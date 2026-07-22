"""Ranking helpers: RRF fuse and recency decay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from org_memory.services.ranking import recency_multiplier, rrf_fuse


def test_rrf_fuse_prefers_shared_high_ranks() -> None:
    fused = rrf_fuse([{"a": 1, "b": 2}, {"b": 1, "c": 2}], k=60)
    assert max(fused, key=fused.get) == "b"  # type: ignore[arg-type]


def test_rrf_fuse_includes_fact_channel() -> None:
    fused = rrf_fuse(
        [
            {"chunk:a": 1},
            {"chunk:a": 2},
            {"fact:f1": 1},
        ],
        k=60,
    )
    assert "fact:f1" in fused
    assert "chunk:a" in fused
    assert fused["chunk:a"] > fused["fact:f1"]


def test_recency_multiplier_decays() -> None:
    now = datetime(2026, 7, 1, tzinfo=UTC)
    recent = recency_multiplier(now - timedelta(days=1), now=now, half_life_days=30, min_decay=0.1)
    old = recency_multiplier(now - timedelta(days=365), now=now, half_life_days=30, min_decay=0.1)
    assert recent > old
    assert old >= 0.1
