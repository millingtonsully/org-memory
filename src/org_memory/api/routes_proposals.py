"""taxonomy_proposals APIs for caller pull + apply/reject callbacks.

"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from org_memory.api.deps import bind_admin, get_session, require_api_key
from org_memory.core.errors import NotFoundError
from org_memory.db.repositories.proposals import TaxonomyProposalRepository
from org_memory.domain.models import Principal
from org_memory.services.proposal_webhook import proposal_payload
from org_memory.services.taxonomy_proposals import TaxonomyProposalService

router = APIRouter(prefix="/v1/taxonomy-proposals", dependencies=[Depends(require_api_key)])


class ProposalDecision(BaseModel):
    decided_by: str = Field(min_length=1, max_length=500, description="caller actor or system id")
    reason: str = Field(default="", max_length=4000)


@router.get("")
def list_pending_proposals(
    limit: int = Query(default=100, ge=1, le=500),
    _admin: Principal = Depends(bind_admin),
    session: Session = Depends(get_session),
) -> dict:
    rows = TaxonomyProposalRepository(session).list_pending(limit=limit)
    return {
        "proposals": [proposal_payload(r) for r in rows],
        "total": len(rows),
    }


@router.post("/generate")
def generate_proposals(
    _admin: Principal = Depends(bind_admin),
    session: Session = Depends(get_session),
) -> dict:
    """Manual/ops trigger; workers also enqueue generate_taxonomy_proposals."""
    return TaxonomyProposalService(session).generate_from_active_claims()


@router.post("/{proposal_id}/applied")
def mark_applied(
    proposal_id: str,
    body: ProposalDecision,
    _admin: Principal = Depends(bind_admin),
    session: Session = Depends(get_session),
) -> dict:
    try:
        row = TaxonomyProposalRepository(session).mark_applied(proposal_id, body.decided_by)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return proposal_payload(row)


@router.post("/{proposal_id}/rejected")
def mark_rejected(
    proposal_id: str,
    body: ProposalDecision,
    _admin: Principal = Depends(bind_admin),
    session: Session = Depends(get_session),
) -> dict:
    try:
        row = TaxonomyProposalRepository(session).mark_rejected(
            proposal_id, body.decided_by, reason=body.reason
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return proposal_payload(row)
