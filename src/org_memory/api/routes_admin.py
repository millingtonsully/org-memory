"""Admin and ops API: health, jobs, spend, legal holds, retention.

Routes under /v1/admin. Deep health covers DB, embed backlog, jobs, connectors,
and spend. /healthz stays a cheap liveness probe. Administrator role required.
"""

from __future__ import annotations

from typing import Literal

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from org_memory.api.deps import (
    bind_admin,
    get_session,
    require_api_key,
)
from org_memory.core.errors import NotFoundError
from org_memory.core.settings import get_settings
from org_memory.core.wiring import get_object_store
from org_memory.db.orm import Job, utcnow
from org_memory.db.repositories import (
    AuditRepository,
    ConnectorStatusRepository,
    JobRepository,
    LegalHoldRepository,
    SpendRepository,
)
from org_memory.domain.models import Principal
from org_memory.services.retention import RetentionService

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/admin", dependencies=[Depends(require_api_key)])


@router.get("/health")
def deep_health(
    principal: Principal = Depends(bind_admin),
    session: Session = Depends(get_session),
) -> dict:
    settings = get_settings()

    embed_backlog = session.execute(
        sql(
            "SELECT count(*) AS n FROM chunks "
            "WHERE embedding IS NULL AND deleted = false AND chunk_role = 'child'"
        )
    ).fetchone()
    assert embed_backlog is not None
    job_repo = JobRepository(session)
    job_counts = job_repo.counts_by_status()
    worker_lag = job_repo.worker_lag_snapshot()
    by_class = SpendRepository(session).totals_by_class_this_month()
    tokens_used = sum(by_class.values())
    spend_alert = tokens_used >= settings.spend_alert_tokens_monthly
    spend_hard_limit_hit = tokens_used >= settings.spend_hard_limit_tokens_monthly
    if spend_alert:
        logger.error(
            "spend.alert_threshold_exceeded",
            tokens_used_this_month=tokens_used,
            threshold=settings.spend_alert_tokens_monthly,
        )
    if spend_hard_limit_hit:
        logger.error(
            "spend.hard_limit_reached",
            tokens_used_this_month=tokens_used,
            hard_limit=settings.spend_hard_limit_tokens_monthly,
        )
    retention_warning = settings.retention_days == 0

    connectors = [_connector_status_payload(c) for c in ConnectorStatusRepository(session).all_statuses()]

    return {
        "status": "ok",
        "workspace_id": settings.workspace_id,
        "database": "reachable",
        "embed_backlog_chunks": int(embed_backlog.n),
        "jobs": job_counts,
        "dead_jobs": job_counts.get("dead", 0),
        "worker_lag": worker_lag,
        "connectors": connectors,
        "spend": {
            "tokens_used_this_month": tokens_used,
            "tokens_by_class_this_month": by_class,
            "alert_threshold": settings.spend_alert_tokens_monthly,
            "alert": spend_alert,
            "hard_limit": settings.spend_hard_limit_tokens_monthly,
            "hard_limit_hit": spend_hard_limit_hit,
        },
        "config": {
            "object_store_backend": settings.object_store_backend,
            "retention_days": settings.retention_days,
            "retention_unset_warning": retention_warning,
            "embedding_model": settings.embedding_model,
            "rerank_model": settings.rerank_model,
        },
    }


def _connector_status_payload(c) -> dict:
    return {
        "source_system": c.source_system,
        "last_envelope_at": c.last_envelope_at.isoformat() if c.last_envelope_at else None,
        "last_event_time": c.last_event_time.isoformat() if c.last_event_time else None,
        "envelopes_total": c.envelopes_total,
        "failures_total": c.failures_total,
        "last_error": c.last_error,
        "last_failure_at": c.last_failure_at.isoformat() if c.last_failure_at else None,
        "recent_errors": list(c.recent_errors or []),
    }


@router.get("/connectors")
def connector_statuses(
    principal: Principal = Depends(bind_admin),
    session: Session = Depends(get_session),
) -> dict:
    """Ingest freshness + failure samples for sync ops."""
    rows = ConnectorStatusRepository(session).all_statuses()
    return {
        "connectors": [_connector_status_payload(c) for c in rows],
        "failing": [
            _connector_status_payload(c)
            for c in rows
            if c.failures_total > 0 and c.last_error
        ],
    }


@router.get("/jobs")
def job_queue(
    principal: Principal = Depends(bind_admin),
    session: Session = Depends(get_session),
) -> dict:
    counts = JobRepository(session).counts_by_status()
    problem_jobs = (
        session.query(Job)
        .filter(Job.status.in_(["dead", "pending"]), Job.last_error != "")
        .order_by(Job.updated_at.desc())
        .limit(50)
        .all()
    )
    return {
        "counts": counts,
        "problem_jobs": [
            {
                "job_id": j.job_id,
                "job_type": j.job_type,
                "status": j.status,
                "attempts": j.attempts,
                "max_attempts": j.max_attempts,
                "last_error": j.last_error,
                "raw_error": j.raw_error,
                "updated_at": j.updated_at.isoformat(),
            }
            for j in problem_jobs
        ],
    }


@router.post("/jobs/{job_id}/retry")
def retry_job(
    job_id: str,
    principal: Principal = Depends(bind_admin),
    session: Session = Depends(get_session),
) -> dict:
    job = session.get(Job, job_id)
    if job is None or job.status != "dead":
        raise NotFoundError(f"no dead job to retry: {job_id}")
    job.status = "pending"
    job.attempts = 0
    job.run_after = utcnow()
    job.updated_at = utcnow()
    AuditRepository(session).record_admin(
        principal, "job.retry", {"job_id": job_id, "job_type": job.job_type}
    )
    logger.info("admin.job_requeued", job_id=job_id, by=principal.principal_id)
    return {"status": "requeued", "job_id": job_id}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: str,
    principal: Principal = Depends(bind_admin),
    session: Session = Depends(get_session),
) -> dict:
    job = session.get(Job, job_id)
    if job is None:
        raise NotFoundError(f"no job to cancel: {job_id}")
    try:
        JobRepository(session).cancel(job)
    except ValueError as exc:
        raise NotFoundError(str(exc)) from exc
    AuditRepository(session).record_admin(
        principal, "job.cancel", {"job_id": job_id, "job_type": job.job_type}
    )
    logger.info("admin.job_cancelled", job_id=job_id, by=principal.principal_id)
    return {"status": "cancelled", "job_id": job_id}


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
