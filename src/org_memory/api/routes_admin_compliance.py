"""Admin spend, legal holds, and retention under /v1/admin."""

from __future__ import annotations

from typing import Literal

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from org_memory.api.deps import bind_admin, get_session
from org_memory.core.wiring import get_object_store
from org_memory.db.repositories import (
    AuditRepository,
    LegalHoldRepository,
    SpendRepository,
)
from org_memory.domain.models import Principal
from org_memory.services.retention import RetentionService

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get("/spend")
def spend_report(
    principal: Principal = Depends(bind_admin),
    session: Session = Depends(get_session),
) -> dict:
    by_class = SpendRepository(session).totals_by_class_this_month()
    return {
        "month_to_date_tokens_by_class": by_class,
        "tokens_used_this_month": sum(by_class.values()),
    }


class PlaceHoldRequest(BaseModel):
    scope_type: Literal["doc", "source_system", "person"] = Field(
        description=(
            "doc: document doc_id; source_system: connector system id; "
            "person: canonical person id (not email or source external id)."
        )
    )
    scope_value: str
    reason: str


@router.post("/legal-holds")
def place_legal_hold(
    body: PlaceHoldRequest,
    principal: Principal = Depends(bind_admin),
    session: Session = Depends(get_session),
) -> dict:
    hold = LegalHoldRepository(session).place(
        body.scope_type, body.scope_value, body.reason, principal.principal_id
    )
    AuditRepository(session).record_admin(
        principal,
        "legal_hold.place",
        {
            "hold_id": hold.hold_id,
            "scope_type": body.scope_type,
            "scope_value": body.scope_value,
            "reason": body.reason,
        },
    )
    return {"hold_id": hold.hold_id, "status": "placed"}


@router.get("/legal-holds")
def list_legal_holds(
    principal: Principal = Depends(bind_admin),
    session: Session = Depends(get_session),
) -> dict:
    holds = LegalHoldRepository(session).active_holds()
    return {
        "holds": [
            {
                "hold_id": h.hold_id,
                "scope_type": h.scope_type,
                "scope_value": h.scope_value,
                "reason": h.reason,
                "placed_by": h.placed_by,
                "placed_at": h.placed_at.isoformat(),
            }
            for h in holds
        ]
    }


@router.post("/legal-holds/{hold_id}/release")
def release_legal_hold(
    hold_id: str,
    principal: Principal = Depends(bind_admin),
    session: Session = Depends(get_session),
) -> dict:
    LegalHoldRepository(session).release(hold_id, principal.principal_id)
    AuditRepository(session).record_admin(
        principal, "legal_hold.release", {"hold_id": hold_id}
    )
    return {"status": "released", "hold_id": hold_id}


@router.post("/retention/purge")
def run_retention_purge(
    principal: Principal = Depends(bind_admin),
    session: Session = Depends(get_session),
) -> dict:
    """Run one retention purge batch and return what was deleted."""
    result = RetentionService(session, get_object_store()).purge_expired()
    AuditRepository(session).record_admin(principal, "retention.purge", result)
    logger.info("admin.retention_purge", by=principal.principal_id, **result)
    return result
