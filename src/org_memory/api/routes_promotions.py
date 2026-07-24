"""Agent promote → OM claim + taxonomy proposal for host apply."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from org_memory.api.deps import bind_principal, get_session, require_api_key
from org_memory.domain.models import Principal
from org_memory.services.promotions import PromotionService

router = APIRouter(dependencies=[Depends(require_api_key)])


class PromoteRequest(BaseModel):
    om_canonical_id: str = Field(min_length=1, max_length=500)
    subject_type: str = Field(default="person", min_length=1, max_length=100)
    host_entity_id: str = Field(default="", max_length=500)
    taxonomy_key: str = Field(min_length=1, max_length=200)
    field_key: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=10_000)
    evidence_doc_ids: list[str] = Field(min_length=1, max_length=200)
    source_kind: str = Field(default="", max_length=100)
    source_id: str = Field(default="", max_length=500)


@router.post("/v1/promotions")
def create_promotion(
    body: PromoteRequest,
    principal: Principal = Depends(bind_principal),
    session: Session = Depends(get_session),
) -> dict:
    try:
        return PromotionService(session).promote(
            principal=principal,
            om_canonical_id=body.om_canonical_id,
            subject_type=body.subject_type,
            taxonomy_key=body.taxonomy_key,
            field_key=body.field_key,
            value=body.value,
            evidence_doc_ids=body.evidence_doc_ids,
            host_entity_id=body.host_entity_id,
            source_kind=body.source_kind,
            source_id=body.source_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
