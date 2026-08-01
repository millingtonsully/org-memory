"""Shared subject-fact reads used by query_facts and retrieve_context.

Keeps registry filtering, temporal status rules, freshness ranking, and
truncation identical across the HTTP primitive and the composed tool.
"""

from __future__ import annotations

from datetime import datetime

from org_memory.core.settings import get_settings
from org_memory.db.orm import Claim
from org_memory.db.repositories import GraphRepository
from org_memory.domain.models import Principal
from org_memory.services.ranking import fact_freshness_score
from org_memory.taxonomy_registry import get_taxonomy_registry


def _platform_binding(registry, predicate: str) -> dict | None:
    binding = registry.predicates[predicate].platform_binding
    return binding.model_dump() if binding is not None else None


def _predicate_half_life(registry, predicate: str, default: float) -> float:
    pred = registry.predicates.get(predicate)
    if pred is None or pred.freshness_half_life_days is None:
        return default
    return float(pred.freshness_half_life_days)


def query_subject_facts(
    graph: GraphRepository,
    *,
    subject_type: str,
    subject_id: str,
    principal: Principal,
    predicate: str | None = None,
    as_of: datetime | None = None,
    believed_as_of: datetime | None = None,
    limit: int = 50,
) -> dict:
    """Return the same payload shape as POST /tools/query_facts for one subject.

    Raises ValueError when ``predicate`` is set and unknown to the registry.
    """
    registry = get_taxonomy_registry()
    settings = get_settings()
    subject_type = subject_type.strip().lower()
    subject_id = subject_id.strip()
    if predicate is not None:
        predicate = predicate.strip().lower()
        if not registry.is_known_predicate(predicate):
            raise ValueError(
                f"Unknown predicate {predicate!r}; not in taxonomy_registry."
            )

    statuses = (
        ["active", "superseded"]
        if as_of is not None or believed_as_of is not None
        else ["active"]
    )
    rows = graph.claims_for_viewer(
        subject_type,
        subject_id,
        principal,
        statuses=statuses,
        as_of=as_of,
        believed_as_of=believed_as_of,
    )
    facts: list[dict] = []
    for claim, evidence_doc_ids in rows:
        if predicate is not None and claim.predicate != predicate:
            continue
        if not registry.is_known_predicate(claim.predicate):
            continue
        if as_of is None and believed_as_of is None and claim.status != "active":
            continue
        facts.append(_shape_claim(claim, evidence_doc_ids, registry, settings))

    facts.sort(
        key=lambda f: (
            float(f["freshness_score"] or 0.0),
            float(f["confidence"] or 0.0),
            str(f["fact_id"]),
        ),
        reverse=True,
    )
    truncated = len(facts) > limit
    facts = facts[:limit]
    return {
        "subject_type": subject_type,
        "subject_id": subject_id,
        "predicate": predicate,
        "as_of": as_of.isoformat() if as_of else None,
        "believed_as_of": believed_as_of.isoformat() if believed_as_of else None,
        "facts": facts,
        "returned": len(facts),
        "truncated": truncated,
    }


def _shape_claim(
    claim: Claim,
    evidence_doc_ids: list[str],
    registry,
    settings,
) -> dict:
    visible_quotes = [
        quote
        for quote in (claim.evidence_quotes or [])
        if quote.get("doc_id") in set(evidence_doc_ids)
    ]
    half = _predicate_half_life(
        registry, claim.predicate, settings.fact_freshness_half_life_days
    )
    as_of_time = claim.valid_from or claim.recorded_at
    freshness = fact_freshness_score(
        confidence=claim.confidence,
        as_of_time=as_of_time,
        half_life_days=half,
        min_decay=settings.fact_freshness_min_decay,
    )
    return {
        "fact_id": claim.claim_id,
        "subject_type": claim.subject_type,
        "subject_id": claim.subject_id,
        "predicate": claim.predicate,
        "object": claim.object_text,
        "confidence": claim.confidence,
        "freshness_score": freshness,
        "freshness_half_life_days": half,
        "status": claim.status,
        "valid_from": claim.valid_from.isoformat() if claim.valid_from else None,
        "valid_to": claim.valid_to.isoformat() if claim.valid_to else None,
        "recorded_at": claim.recorded_at.isoformat() if claim.recorded_at else None,
        "invalidated_at": (
            claim.invalidated_at.isoformat() if claim.invalidated_at else None
        ),
        "evidence_doc_ids": evidence_doc_ids,
        "evidence_quotes": visible_quotes,
        "updated_at": claim.updated_at.isoformat(),
        "platform_binding": _platform_binding(registry, claim.predicate),
    }
