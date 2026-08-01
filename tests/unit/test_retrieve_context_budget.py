"""Unit tests for retrieve_context packing and request guards."""

from __future__ import annotations

import pytest

from org_memory.services.retrieve_context import _apply_token_budget


def test_token_budget_trims_lowest_priority_first() -> None:
    payload = {
        "passages": [{"text": "passage " + ("x" * 200)} for _ in range(5)],
        "search_facts": [{"text": "fact " + ("y" * 200)} for _ in range(5)],
        "structured_facts": [{"facts": [{"object": "z" * 200}]} for _ in range(5)],
        "paths": [{"paths": [{"nodes": ["a", "b"]}]} for _ in range(5)],
        "truncated_tokens": False,
    }
    # Tiny budget forces trimming.
    out = _apply_token_budget(payload, max_tokens=50, mode="vector_first")
    assert out["truncated_tokens"] is True
    # vector_first trims paths before passages.
    assert len(out["paths"]) < 5 or len(out["structured_facts"]) < 5


def test_token_budget_noop_when_under_limit() -> None:
    payload = {
        "passages": [{"text": "hi"}],
        "search_facts": [],
        "structured_facts": [],
        "paths": [],
        "truncated_tokens": False,
    }
    out = _apply_token_budget(payload, max_tokens=10_000, mode="joint")
    assert out["truncated_tokens"] is False
    assert out["passages"] == payload["passages"]


def test_token_budget_rejects_non_positive() -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        _apply_token_budget({"passages": []}, max_tokens=0, mode="vector_first")
