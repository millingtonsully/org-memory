"""Admin health and connector status under /v1/admin."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from org_memory.api.deps import bind_admin, get_session
from org_memory.core.settings import get_settings
from org_memory.db.repositories import (
    ConnectorStatusRepository,
    JobRepository,
    SpendRepository,
)
from org_memory.domain.models import Principal

logger = structlog.get_logger(__name__)

router = APIRouter()


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

    connectors = [
        _connector_status_payload(c)
        for c in ConnectorStatusRepository(session).all_statuses()
    ]

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
