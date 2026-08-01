"""Retrieval evaluation package: gold-set scoring and baseline metrics."""

from org_memory.eval.harness import (
    CasePrediction,
    CaseScore,
    EvalReport,
    GoldCase,
    default_gold_path,
    load_gold_set,
    score_case,
    score_predictions,
)
from org_memory.eval.metrics import (
    hit_at_k,
    mean_reciprocal_rank,
    precision_at_k,
    recall_at_k,
)
from org_memory.eval.predict import predictions_from_retrieve_payload

__all__ = [
    "CasePrediction",
    "CaseScore",
    "EvalReport",
    "GoldCase",
    "default_gold_path",
    "hit_at_k",
    "load_gold_set",
    "mean_reciprocal_rank",
    "precision_at_k",
    "predictions_from_retrieve_payload",
    "recall_at_k",
    "score_case",
    "score_predictions",
]
