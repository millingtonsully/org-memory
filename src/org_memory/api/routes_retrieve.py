"""POST /tools/retrieve_context — composed search + facts + paths for agents."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from org_memory.api.deps import bind_principal, get_retrieve_context_service, require_api_key
from org_memory.domain.models import Principal
from org_memory.services.retrieve_context import RetrieveContextService, SubjectRef

router = APIRouter(dependencies=[Depends(require_api_key)])


class SubjectIn(BaseModel):
    type: str = Field(min_length=1)
    id: str = Field(min_length=1)


class RetrieveContextRequest(BaseModel):
    query: str = Field(min_length=1)
    mode: Literal["vector_first", "graph_first", "joint"] = "vector_first"
    limit: int = Field(default=20, ge=1, le=200)
    subjects: list[SubjectIn] = Field(default_factory=list)
    about: str | None = Field(
        default=None,
        description="Optional name to resolve into subject seeds (viewer-scoped).",
    )
    as_of: datetime | None = None
    believed_as_of: datetime | None = None
    path_max_depth: int = Field(default=2, ge=1, le=10)
    path_limit: int = Field(default=20, ge=1, le=500)
    relationship_types: list[str] = Field(default_factory=list)
    fact_limit_per_subject: int = Field(default=20, ge=1, le=200)
    max_tokens: int | None = Field(default=None, ge=1, le=200_000)
    source_type: str | None = None
    source_system: str | None = None
    author: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    updated_from: datetime | None = None
    updated_to: datetime | None = None
    doc_id: str | None = None
    half_life_days: float = Field(default=90.0, ge=1, le=365)
    min_decay: float = Field(default=0.3, ge=0, le=1)


@router.post("/tools/retrieve_context")
def retrieve_context(
    body: RetrieveContextRequest,
    principal: Principal = Depends(bind_principal),
    service: RetrieveContextService = Depends(get_retrieve_context_service),
) -> dict:
    try:
        return service.retrieve(
            principal=principal,
            query=body.query,
            mode=body.mode,
            limit=body.limit,
            subjects=[
                SubjectRef(type=s.type, id=s.id) for s in body.subjects
            ],
            about=body.about,
            as_of=body.as_of,
            believed_as_of=body.believed_as_of,
            path_max_depth=body.path_max_depth,
            path_limit=body.path_limit,
            relationship_types=body.relationship_types,
            fact_limit_per_subject=body.fact_limit_per_subject,
            max_tokens=body.max_tokens,
            source_type=body.source_type,
            source_system=body.source_system,
            author=body.author,
            date_from=body.date_from,
            date_to=body.date_to,
            updated_from=body.updated_from,
            updated_to=body.updated_to,
            doc_id=body.doc_id,
            half_life_days=body.half_life_days,
            min_decay=body.min_decay,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
