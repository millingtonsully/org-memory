"""Viewer-scoped collaboration graph queries."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from org_memory.api.deps import bind_principal, get_session, require_api_key
from org_memory.domain.models import Principal
from org_memory.services.collaboration import CollaborationService

router = APIRouter(prefix="/v1/collaboration", dependencies=[Depends(require_api_key)])


@router.get("/persons/{person_id}/collaborators")
def top_collaborators(
    person_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(bind_principal),
    session: Session = Depends(get_session),
) -> dict:
    rows = CollaborationService(session).top_collaborators(
        person_id, principal, limit=limit
    )
    return {"person_id": person_id, "collaborators": rows, "total": len(rows)}


@router.get("/pair")
def pair_strength(
    person_a: str = Query(min_length=1),
    person_b: str = Query(min_length=1),
    principal: Principal = Depends(bind_principal),
    session: Session = Depends(get_session),
) -> dict:
    edge = CollaborationService(session).pair_strength(person_a, person_b, principal)
    return {"pair": edge}
