"""Unit tests for pure fact snapshot diff."""

from __future__ import annotations

from org_memory.services.temporality.diff import diff_fact_snapshots


def _fact(fid: str, predicate: str, obj: str) -> dict:
    return {"fact_id": fid, "predicate": predicate, "object": obj}


def test_unchanged_and_added_removed() -> None:
    a = [_fact("1", "title", "IC"), _fact("2", "member_of", "Platform")]
    b = [_fact("1", "title", "IC"), _fact("3", "member_of", "Infra")]
    out = diff_fact_snapshots(a, b, exclusive_predicates=frozenset({"title"}))
    assert out["counts"]["unchanged"] == 1
    assert [f["fact_id"] for f in out["removed"]] == ["2"]
    assert [f["fact_id"] for f in out["added"]] == ["3"]
    assert out["changed"] == []


def test_exclusive_predicate_becomes_changed() -> None:
    a = [_fact("1", "title", "IC")]
    b = [_fact("2", "title", "Manager")]
    out = diff_fact_snapshots(a, b, exclusive_predicates=frozenset({"title"}))
    assert out["counts"]["changed"] == 1
    assert out["changed"][0]["from"]["object"] == "IC"
    assert out["changed"][0]["to"]["object"] == "Manager"
    assert out["added"] == []
    assert out["removed"] == []


def test_non_exclusive_stays_add_remove() -> None:
    a = [_fact("1", "member_of", "Platform")]
    b = [_fact("2", "member_of", "Infra")]
    out = diff_fact_snapshots(a, b, exclusive_predicates=frozenset({"title"}))
    assert out["counts"]["changed"] == 0
    assert out["counts"]["removed"] == 1
    assert out["counts"]["added"] == 1
