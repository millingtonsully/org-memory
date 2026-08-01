"""Score ranked retrieval predictions against a gold question set.

Gold labels are evaluation targets only. They are never written into the
production graph or used as silent fallbacks for live answers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from org_memory.eval.metrics import (
    hit_at_k,
    mean_reciprocal_rank,
    precision_at_k,
    recall_at_k,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_GOLD_PATH = _REPO_ROOT / "evals" / "retrieval" / "gold_set.json"


@dataclass(frozen=True)
class GoldCase:
    case_id: str
    query: str
    expected_doc_ids: tuple[str, ...]
    expected_claim_ids: tuple[str, ...] = ()
    k: int = 10
    notes: str = ""
    mode: str = "vector_first"
    subjects: tuple[tuple[str, str], ...] = ()
    about: str | None = None
    as_of: str | None = None
    believed_as_of: str | None = None


@dataclass(frozen=True)
class CasePrediction:
    """Ranked ids produced by a system under test for one gold case."""

    doc_ids: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CaseScore:
    case_id: str
    k: int
    doc_hit_at_k: float | None
    doc_recall_at_k: float | None
    doc_precision_at_k: float | None
    doc_mrr: float | None
    claim_hit_at_k: float | None
    claim_recall_at_k: float | None
    claim_mrr: float | None


@dataclass
class EvalReport:
    cases: list[CaseScore] = field(default_factory=list)
    missing_predictions: list[str] = field(default_factory=list)

    def averages(self) -> dict[str, float]:
        """Mean of each metric across cases where that metric was defined."""
        buckets: dict[str, list[float]] = {}
        fields = (
            "doc_hit_at_k",
            "doc_recall_at_k",
            "doc_precision_at_k",
            "doc_mrr",
            "claim_hit_at_k",
            "claim_recall_at_k",
            "claim_mrr",
        )
        for case in self.cases:
            for name in fields:
                value = getattr(case, name)
                if value is None:
                    continue
                buckets.setdefault(name, []).append(float(value))
        return {
            name: sum(values) / len(values)
            for name, values in buckets.items()
            if values
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_count": len(self.cases),
            "missing_predictions": list(self.missing_predictions),
            "averages": self.averages(),
            "cases": [case.__dict__ for case in self.cases],
        }


def default_gold_path() -> Path:
    candidates = (
        _REPO_ROOT / "evals" / "retrieval" / "gold_set.json",
        Path.cwd() / "evals" / "retrieval" / "gold_set.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def load_gold_set(path: Path | None = None) -> list[GoldCase]:
    gold_path = path or default_gold_path()
    raw = json.loads(gold_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "cases" not in raw:
        raise ValueError(f"gold set must be an object with a 'cases' array: {gold_path}")
    cases: list[GoldCase] = []
    for entry in raw["cases"]:
        if not isinstance(entry, dict):
            raise ValueError("each gold case must be an object")
        case_id = str(entry["case_id"]).strip()
        query = str(entry["query"]).strip()
        if not case_id or not query:
            raise ValueError("case_id and query are required on every gold case")
        docs = tuple(str(x) for x in entry.get("expected_doc_ids") or [])
        claims = tuple(str(x) for x in entry.get("expected_claim_ids") or [])
        if not docs and not claims:
            raise ValueError(f"case {case_id!r} needs expected_doc_ids and/or expected_claim_ids")
        k = int(entry.get("k", 10))
        if k < 1:
            raise ValueError(f"case {case_id!r} k must be >= 1")
        subjects_raw = entry.get("subjects") or []
        subjects: list[tuple[str, str]] = []
        for subject in subjects_raw:
            if not isinstance(subject, dict):
                raise ValueError(f"case {case_id!r} subjects entries must be objects")
            subjects.append((str(subject["type"]).strip(), str(subject["id"]).strip()))
        mode = str(entry.get("mode") or "vector_first").strip()
        about = entry.get("about")
        as_of = entry.get("as_of")
        believed_as_of = entry.get("believed_as_of")
        cases.append(
            GoldCase(
                case_id=case_id,
                query=query,
                expected_doc_ids=docs,
                expected_claim_ids=claims,
                k=k,
                notes=str(entry.get("notes") or ""),
                mode=mode,
                subjects=tuple(subjects),
                about=str(about).strip() if about else None,
                as_of=str(as_of).strip() if as_of else None,
                believed_as_of=str(believed_as_of).strip() if believed_as_of else None,
            )
        )
    if not cases:
        raise ValueError(f"gold set has no cases: {gold_path}")
    return cases


def score_case(case: GoldCase, prediction: CasePrediction) -> CaseScore:
    doc_relevant = set(case.expected_doc_ids)
    claim_relevant = set(case.expected_claim_ids)
    return CaseScore(
        case_id=case.case_id,
        k=case.k,
        doc_hit_at_k=(
            hit_at_k(prediction.doc_ids, doc_relevant, case.k) if doc_relevant else None
        ),
        doc_recall_at_k=(
            recall_at_k(prediction.doc_ids, doc_relevant, case.k) if doc_relevant else None
        ),
        doc_precision_at_k=(
            precision_at_k(prediction.doc_ids, doc_relevant, case.k) if doc_relevant else None
        ),
        doc_mrr=(
            mean_reciprocal_rank(prediction.doc_ids, doc_relevant) if doc_relevant else None
        ),
        claim_hit_at_k=(
            hit_at_k(prediction.claim_ids, claim_relevant, case.k) if claim_relevant else None
        ),
        claim_recall_at_k=(
            recall_at_k(prediction.claim_ids, claim_relevant, case.k) if claim_relevant else None
        ),
        claim_mrr=(
            mean_reciprocal_rank(prediction.claim_ids, claim_relevant) if claim_relevant else None
        ),
    )


def score_predictions(
    cases: list[GoldCase],
    predictions: dict[str, CasePrediction],
) -> EvalReport:
    report = EvalReport()
    for case in cases:
        prediction = predictions.get(case.case_id)
        if prediction is None:
            report.missing_predictions.append(case.case_id)
            continue
        report.cases.append(score_case(case, prediction))
    return report


def predictions_from_mapping(raw: dict[str, Any]) -> dict[str, CasePrediction]:
    """Parse ``{case_id: {doc_ids: [...], claim_ids: [...]}}`` payloads."""
    out: dict[str, CasePrediction] = {}
    for case_id, payload in raw.items():
        if not isinstance(payload, dict):
            raise ValueError(f"prediction for {case_id!r} must be an object")
        out[str(case_id)] = CasePrediction(
            doc_ids=tuple(str(x) for x in payload.get("doc_ids") or []),
            claim_ids=tuple(str(x) for x in payload.get("claim_ids") or []),
        )
    return out
