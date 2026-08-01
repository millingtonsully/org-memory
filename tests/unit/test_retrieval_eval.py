"""Unit tests for retrieval gold-set metrics and harness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from org_memory.eval.harness import (
    CasePrediction,
    default_gold_path,
    load_gold_set,
    predictions_from_mapping,
    score_case,
    score_predictions,
)
from org_memory.eval.metrics import (
    hit_at_k,
    mean_reciprocal_rank,
    precision_at_k,
    recall_at_k,
)
from org_memory.eval.score_retrieval import main as score_main


def test_hit_recall_precision_mrr() -> None:
    ranked = ["a", "b", "c", "d"]
    relevant = {"c", "z"}
    assert hit_at_k(ranked, relevant, k=2) == 0.0
    assert hit_at_k(ranked, relevant, k=3) == 1.0
    assert recall_at_k(ranked, relevant, k=3) == 0.5
    assert precision_at_k(ranked, relevant, k=3) == pytest.approx(1.0 / 3.0)
    assert mean_reciprocal_rank(ranked, relevant) == pytest.approx(1.0 / 3.0)


def test_metrics_reject_empty_relevant() -> None:
    with pytest.raises(ValueError):
        hit_at_k(["a"], set(), k=1)


def test_load_shipped_gold_set() -> None:
    cases = load_gold_set()
    assert default_gold_path().is_file()
    assert len(cases) >= 3
    assert all(
        c.query
        and (
            c.expected_doc_ids
            or c.expected_claim_ids
            or c.expected_diff_changed_pairs
        )
        for c in cases
    )


def test_score_predictions_averages_and_missing(tmp_path: Path) -> None:
    gold = {
        "cases": [
            {
                "case_id": "one",
                "query": "q1",
                "expected_doc_ids": ["doc:a", "doc:b"],
                "k": 2,
            },
            {
                "case_id": "two",
                "query": "q2",
                "expected_doc_ids": ["doc:x"],
                "expected_claim_ids": ["claim:1"],
                "k": 5,
            },
        ]
    }
    path = tmp_path / "gold.json"
    path.write_text(json.dumps(gold), encoding="utf-8")
    cases = load_gold_set(path)
    report = score_predictions(
        cases,
        {
            "one": CasePrediction(doc_ids=("doc:a", "doc:other")),
            # "two" missing on purpose
        },
    )
    assert report.missing_predictions == ["two"]
    assert len(report.cases) == 1
    assert report.cases[0].doc_hit_at_k == 1.0
    assert report.cases[0].doc_recall_at_k == 0.5
    assert report.averages()["doc_hit_at_k"] == 1.0


def test_score_case_claim_channel() -> None:
    cases = load_gold_set()
    glossary = next(c for c in cases if c.case_id == "glossary-definition")
    scored = score_case(
        glossary,
        CasePrediction(
            doc_ids=("notion:glossary-carepod",),
            claim_ids=("claim:carepod-definition",),
        ),
    )
    assert scored.doc_hit_at_k == 1.0
    assert scored.claim_hit_at_k == 1.0
    assert scored.claim_mrr == 1.0


def test_cli_missing_predictions_is_clear(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    with pytest.raises(SystemExit) as exc:
        score_main(["--predictions", str(missing)])
    assert "predictions file not found" in str(exc.value)


def test_cli_scores_predictions(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from org_memory.eval.harness import default_gold_path

    example = default_gold_path().parent / "example_predictions.json"
    assert score_main(["--predictions", str(example)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["case_count"] == len(load_gold_set())
    assert payload["averages"]["doc_hit_at_k"] == 1.0
    assert not payload["missing_predictions"]


def test_predictions_from_mapping_roundtrip() -> None:
    parsed = predictions_from_mapping({"c1": {"doc_ids": ["d1"], "claim_ids": ["c"]}})
    assert parsed["c1"].doc_ids == ("d1",)
    assert parsed["c1"].claim_ids == ("c",)


def test_predictions_from_retrieve_payload_orders_ids() -> None:
    from org_memory.eval.predict import predictions_from_retrieve_payload

    pred = predictions_from_retrieve_payload(
        {
            "passages": [
                {"doc_id": "doc:a"},
                {"doc_id": "doc:b"},
                {"doc_id": "doc:a"},
            ],
            "search_facts": [{"fact_id": "claim:1"}],
            "structured_facts": [{"facts": [{"fact_id": "claim:2"}, {"fact_id": "claim:1"}]}],
        }
    )
    assert pred.doc_ids == ("doc:a", "doc:b")
    assert pred.claim_ids == ("claim:1", "claim:2")
