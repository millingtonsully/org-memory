"""Deterministic query_facts: registry-backed claims, viewer-scoped, temporal as_of."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from org_memory.api.deps import bind_principal, get_session, require_api_key
from org_memory.db.repositories import GraphRepository
from org_memory.domain.models import Principal
from org_memory.taxonomy_registry import get_taxonomy_registry

router = APIRouter(dependencies=[Depends(require_api_key)])


def _platform_binding(registry, predicate: str) -> dict | None:
    binding = registry.predicates[predicate].platform_binding
    return binding.model_dump() if binding is not None else None


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
    limit: int = Field(default=50, ge=1, le=200)


@router.post("/tools/query_facts")
def query_facts(
    body: QueryFactsRequest,
    principal: Principal = Depends(bind_principal),
    session: Session = Depends(get_session),
) -> dict:
    registry = get_taxonomy_registry()
    if body.predicate is not None:
        predicate = body.predicate.strip().lower()
        if not registry.is_known_predicate(predicate):
            raise HTTPException(
                status_code=422,
                detail=f"Unknown predicate {predicate!r}; not in taxonomy_registry.",
            )
    else:
        predicate = None

    statuses = ["active", "superseded"] if body.as_of is not None else ["active"]
    graph = GraphRepository(session)
    rows = graph.claims_for_viewer(
        body.subject_type.strip().lower(),
        body.subject_id.strip(),
        principal,
        statuses=statuses,
        as_of=body.as_of,
    )
    facts = []
    truncated = False
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
        facts.append(
            {
                "fact_id": claim.claim_id,
                "subject_type": claim.subject_type,
                "subject_id": claim.subject_id,
                "predicate": claim.predicate,
                "object": claim.object_text,
                "confidence": claim.confidence,
                "status": claim.status,
                "valid_from": claim.valid_from.isoformat() if claim.valid_from else None,
                "valid_to": claim.valid_to.isoformat() if claim.valid_to else None,
                "evidence_doc_ids": evidence_doc_ids,
                "evidence_quotes": visible_quotes,
                "updated_at": claim.updated_at.isoformat(),
                "platform_binding": _platform_binding(registry, claim.predicate),
            }
        )
        if len(facts) >= body.limit:
            truncated = True
            break

    return {
        "subject_type": body.subject_type.strip().lower(),
        "subject_id": body.subject_id.strip(),
        "predicate": predicate,
        "as_of": body.as_of.isoformat() if body.as_of else None,
        "facts": facts,
        "returned": len(facts),
        "truncated": truncated,
    }
