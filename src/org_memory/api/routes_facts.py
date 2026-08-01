"""Deterministic query_facts: registry-backed claims, viewer-scoped, temporal as_of.

Results are ordered by freshness-weighted confidence (stale active facts rank lower).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from org_memory.api.deps import bind_principal, get_session, require_api_key
from org_memory.core.settings import get_settings
from org_memory.db.repositories import GraphRepository
from org_memory.domain.models import Principal
from org_memory.services.ranking import fact_freshness_score
from org_memory.taxonomy_registry import get_taxonomy_registry

router = APIRouter(dependencies=[Depends(require_api_key)])


def _platform_binding(registry, predicate: str) -> dict | None:
    binding = registry.predicates[predicate].platform_binding
    return binding.model_dump() if binding is not None else None


def _predicate_half_life(registry, predicate: str, default: float) -> float:
    pred = registry.predicates.get(predicate)
    if pred is None or pred.freshness_half_life_days is None:
        return default
    return float(pred.freshness_half_life_days)


class QueryFactsRequest(BaseModel):
    subject_type: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    predicate: str | None = Field(
        default=None,
        description="Registry predicate key; omit to return all matching predicates for the subject.",
    )
    as_of: datetime | None = Field(
        default=None,
        description=(
            "World-time point. When set, returns active∪superseded claims whose "
            "validity window contains as_of (half-open: valid_from <= as_of < valid_to). "
            "When omitted, returns currently active open-interval claims."
        ),
    )
    believed_as_of: datetime | None = Field(
        default=None,
        description=(
            "System-time point: what the service believed then "
            "(recorded_at <= believed_as_of < invalidated_at)."
        ),
    )
    limit: int = Field(default=50, ge=1, le=200)


@router.post("/tools/query_facts")
def query_facts(
    body: QueryFactsRequest,
    principal: Principal = Depends(bind_principal),
    session: Session = Depends(get_session),
) -> dict:
    registry = get_taxonomy_registry()
    settings = get_settings()
    if body.predicate is not None:
        predicate = body.predicate.strip().lower()
        if not registry.is_known_predicate(predicate):
            raise HTTPException(
                status_code=422,
                detail=f"Unknown predicate {predicate!r}; not in taxonomy_registry.",
            )
    else:
        predicate = None

    statuses = (
        ["active", "superseded"]
        if body.as_of is not None or body.believed_as_of is not None
        else ["active"]
    )
    graph = GraphRepository(session)
    rows = graph.claims_for_viewer(
        body.subject_type.strip().lower(),
        body.subject_id.strip(),
        principal,
        statuses=statuses,
        as_of=body.as_of,
        believed_as_of=body.believed_as_of,
    )
    facts = []
    for claim, evidence_doc_ids in rows:
        if predicate is not None and claim.predicate != predicate:
            continue
        if not registry.is_known_predicate(claim.predicate):
            continue
        if body.as_of is None and claim.status != "active":
            continue
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
        facts.append(
            {
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
        )

    def _sort_key(f: dict) -> tuple[float, float, str]:
        freshness = f["freshness_score"]
        confidence = f["confidence"]
        freshness_f = float(freshness) if isinstance(freshness, (int, float)) else 0.0
        confidence_f = float(confidence) if isinstance(confidence, (int, float)) else 0.0
        return (freshness_f, confidence_f, str(f["fact_id"]))

    facts.sort(key=_sort_key, reverse=True)
    truncated = len(facts) > body.limit
    facts = facts[: body.limit]

    return {
        "subject_type": body.subject_type.strip().lower(),
        "subject_id": body.subject_id.strip(),
        "predicate": predicate,
        "as_of": body.as_of.isoformat() if body.as_of else None,
        "believed_as_of": body.believed_as_of.isoformat() if body.believed_as_of else None,
        "facts": facts,
        "returned": len(facts),
        "truncated": truncated,
    }


class QueryPathsRequest(BaseModel):
    start_type: str = Field(min_length=1)
    start_id: str = Field(min_length=1)
    relationship_types: list[str] = Field(default_factory=list)
    max_depth: int = Field(
        default=2,
        ge=1,
        le=10,
        description="Requested walk depth; effective maximum is 3 (see capped).",
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Requested path cap; effective maximum is 200 (see capped).",
    )
    as_of: datetime | None = Field(
        default=None,
        description=(
            "World-time point. When set, walks edges whose validity window "
            "contains as_of (active and superseded)."
        ),
    )
    believed_as_of: datetime | None = Field(
        default=None,
        description=(
            "System-time point: edges the service believed then "
            "(recorded_at <= believed_as_of < invalidated_at)."
        ),
    )


@router.post("/tools/query_paths")
def query_paths(
    body: QueryPathsRequest,
    principal: Principal = Depends(bind_principal),
    session: Session = Depends(get_session),
) -> dict:
    """Deterministic multi-hop relationship walk (Postgres recursive CTE)."""
    result = GraphRepository(session).paths_from(
        start_type=body.start_type.strip().lower(),
        start_id=body.start_id.strip(),
        principal=principal,
        relationship_types=body.relationship_types,
        max_depth=body.max_depth,
        limit=body.limit,
        as_of=body.as_of,
        believed_as_of=body.believed_as_of,
    )
    return {
        "start": {
            "type": body.start_type.strip().lower(),
            "id": body.start_id.strip(),
        },
        "paths": result["paths"],
        "returned": result["returned"],
        "limit": result["limit"],
        "max_depth": result["max_depth"],
        "truncated": result["truncated"],
        "capped": result["capped"],
        "as_of": body.as_of.isoformat() if body.as_of else None,
        "believed_as_of": (
            body.believed_as_of.isoformat() if body.believed_as_of else None
        ),
    }
