"""POST /tools/query_facts and /tools/query_paths — structured graph primitives."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from org_memory.api.deps import bind_principal, get_session, require_api_key
from org_memory.db.repositories import GraphRepository
from org_memory.domain.models import Principal
from org_memory.services.facts_diff import diff_subject_facts
from org_memory.services.facts_query import query_subject_facts

router = APIRouter(dependencies=[Depends(require_api_key)])


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
    try:
        return query_subject_facts(
            GraphRepository(session),
            subject_type=body.subject_type,
            subject_id=body.subject_id,
            principal=principal,
            predicate=body.predicate,
            as_of=body.as_of,
            believed_as_of=body.believed_as_of,
            limit=body.limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class DiffFactsRequest(BaseModel):
    subject_type: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    predicate: str | None = Field(
        default=None,
        description="Registry predicate key; omit to diff all predicates for the subject.",
    )
    as_of_from: datetime | None = Field(
        default=None,
        description="World-time start of the snapshot pair (exclusive with belief pair).",
    )
    as_of_to: datetime | None = Field(
        default=None,
        description="World-time end of the snapshot pair; must be after as_of_from.",
    )
    believed_as_of_from: datetime | None = Field(
        default=None,
        description="Belief-time start of the snapshot pair (exclusive with world pair).",
    )
    believed_as_of_to: datetime | None = Field(
        default=None,
        description=(
            "Belief-time end of the snapshot pair; must be after believed_as_of_from."
        ),
    )
    limit: int = Field(default=50, ge=1, le=200)


@router.post("/tools/diff_facts")
def diff_facts(
    body: DiffFactsRequest,
    principal: Principal = Depends(bind_principal),
    session: Session = Depends(get_session),
) -> dict:
    """Compare two temporal snapshots of claims for one subject."""
    try:
        return diff_subject_facts(
            GraphRepository(session),
            subject_type=body.subject_type,
            subject_id=body.subject_id,
            principal=principal,
            predicate=body.predicate,
            as_of_from=body.as_of_from,
            as_of_to=body.as_of_to,
            believed_as_of_from=body.believed_as_of_from,
            believed_as_of_to=body.believed_as_of_to,
            limit=body.limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
