"""Agent-authored procedural memory creation and viewer-scoped recall."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from org_memory.api.deps import (
    bind_principal,
    get_procedural_memory_service,
    require_api_key,
)
from org_memory.domain.models import Principal
from org_memory.domain.principals import require_principal
from org_memory.services.procedural_memory import ProceduralMemoryService

router = APIRouter(dependencies=[Depends(require_api_key)])


class ProcedureEvent(BaseModel):
    actor: str = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=20_000)
    result: str = Field(min_length=1, max_length=40_000)
    metadata: dict = Field(default_factory=dict)


class CreateProceduralMemoryRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=500)
    run_id: str = Field(min_length=1, max_length=500)
    procedure_key: str = Field(min_length=1, max_length=500)
    objective: str = Field(min_length=1, max_length=10_000)
    events: list[ProcedureEvent] = Field(min_length=1, max_length=200)
    evidence_doc_ids: list[str] = Field(default_factory=list, max_length=500)
    org_visible: bool = False
    allowed_principals: list[str] = Field(default_factory=list, max_length=500)

    @field_validator("allowed_principals")
    @classmethod
    def _validate_principals(cls, values: list[str]) -> list[str]:
        return [require_principal(v, field="allowed_principals") for v in values]


@router.post("/v1/procedural-memories")
def create_procedural_memory(
    body: CreateProceduralMemoryRequest,
    principal: Principal = Depends(bind_principal),
    service: ProceduralMemoryService = Depends(get_procedural_memory_service),
) -> dict:
    try:
        memory = service.create(
            principal=principal,
            agent_id=body.agent_id,
            run_id=body.run_id,
            procedure_key=body.procedure_key,
            objective=body.objective,
            events=[event.model_dump() for event in body.events],
            evidence_doc_ids=body.evidence_doc_ids,
            org_visible=body.org_visible,
            allowed_principals=body.allowed_principals,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "memory_id": memory.memory_id,
        "status": memory.status,
        "procedure_key": memory.procedure_key,
    }


class SearchProceduralMemoryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=10_000)
    agent_id: str | None = Field(default=None, max_length=500)
    limit: int = Field(default=10, ge=1, le=50)


@router.post("/tools/search_procedural_memory")
def search_procedural_memory(
    body: SearchProceduralMemoryRequest,
    principal: Principal = Depends(bind_principal),
    service: ProceduralMemoryService = Depends(get_procedural_memory_service),
) -> dict:
    return service.search(
        principal=principal,
        query=body.query,
        agent_id=body.agent_id,
        limit=body.limit,
    )
