"""Deterministic query_facts: active registry-backed claims, viewer-scoped."""

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
        description="Registry predicate key; omit to return all active predicates for the subject.",
    )
    as_of: datetime | None = Field(
        default=None,
        description="Reserved for temporal validity; currently filters by claim updated_at when set.",
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

    graph = GraphRepository(session)
    rows = graph.claims_for_viewer(
        body.subject_type.strip().lower(),
        body.subject_id.strip(),
        principal,
        statuses=["active"],
    )
    facts = []
    for claim, evidence_doc_ids in rows:
        if predicate is not None and claim.predicate != predicate:
            continue
        if not registry.is_known_predicate(claim.predicate):
            continue
        if body.as_of is not None and claim.updated_at > body.as_of:
            continue
        facts.append(
            {
                "fact_id": claim.claim_id,
                "subject_type": claim.subject_type,
                "subject_id": claim.subject_id,
                "predicate": claim.predicate,
                "object": claim.object_text,
                "confidence": claim.confidence,
                "status": claim.status,
                "evidence_doc_ids": evidence_doc_ids,
                "updated_at": claim.updated_at.isoformat(),
                "platform_binding": _platform_binding(registry, claim.predicate),
            }
        )
        if len(facts) >= body.limit:
            break

    return {
        "subject_type": body.subject_type.strip().lower(),
        "subject_id": body.subject_id.strip(),
        "predicate": predicate,
        "as_of": body.as_of.isoformat() if body.as_of else None,
        "facts": facts,
        "total": len(facts),
    }
