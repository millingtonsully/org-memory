"""FastAPI entry point.

    uvicorn org_memory.main:app --reload

Settings are validated at import time. A missing key stops the process
before any request is served.
"""

from __future__ import annotations

import hmac

import structlog
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response

from org_memory.api import (
    routes_admin,
    routes_audit,
    routes_collaboration,
    routes_facts,
    routes_graph,
    routes_ingress,
    routes_procedural,
    routes_promotions,
    routes_proposals,
    routes_search,
    routes_worldbuilder,
)
from org_memory.core.errors import (
    ConfigurationError,
    NotFoundError,
    SpendLimitError,
    VendorAPIError,
)
from org_memory.core.logging import configure_logging
from org_memory.core.metrics import (
    EMBED_BACKLOG,
    JOBS_BY_STATUS,
    RETENTION_UNSET,
    SPEND_ALERT,
    SPEND_HARD_LIMIT_HIT,
    SPEND_TOKENS_MONTH,
    VENDOR_ERRORS,
    metrics_payload,
)
from org_memory.core.settings import get_settings
from org_memory.db.orm import EMBEDDING_DIM
from org_memory.taxonomy_registry import get_taxonomy_registry

configure_logging()
settings = get_settings()
logger = structlog.get_logger(__name__)

# Closed taxonomy schema — fail before serving if YAML is missing/invalid.
get_taxonomy_registry()

if settings.embedding_dimensions != EMBEDDING_DIM:
    raise ConfigurationError(
        f"EMBEDDING_DIMENSIONS={settings.embedding_dimensions} does not match the "
        f"database schema's vector({EMBEDDING_DIM}) column. Changing dimensions "
        "needs a re-embed migration, not just a config change."
    )

app = FastAPI()

# Agent tools and the write door for connectors.
app.include_router(routes_search.router)
app.include_router(routes_worldbuilder.router)
app.include_router(routes_facts.router)
app.include_router(routes_proposals.router)
app.include_router(routes_ingress.router)
app.include_router(routes_procedural.router)
app.include_router(routes_promotions.router)
# Governance and ops.
app.include_router(routes_graph.router)
app.include_router(routes_collaboration.router)
app.include_router(routes_audit.router)
app.include_router(routes_admin.router)


@app.exception_handler(VendorAPIError)
def vendor_error_handler(request: Request, exc: VendorAPIError) -> JSONResponse:
    # An outside API failed. Fail loudly server-side (full cause in the logs),
    # but never echo an untrusted vendor body back to the caller: it can leak
    # upstream URLs, keys in error strings, or internal detail. The client gets
    # a stable, generic 502; operators get everything in the log line.
    VENDOR_ERRORS.labels(vendor=exc.vendor).inc()
    logger.error(
        "vendor_api.failed",
        vendor=exc.vendor,
        status_code=exc.status_code,
        detail=exc.detail,
        raw_response=exc.raw_response,
        path=request.url.path,
    )
    return JSONResponse(
        status_code=502,
        content={"detail": "An upstream dependency failed. The error has been logged."},
    )


@app.exception_handler(NotFoundError)
def not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(SpendLimitError)
def spend_limit_handler(request: Request, exc: SpendLimitError) -> JSONResponse:
    logger.error(
        "spend.hard_limit_reached",
        tokens_used=exc.tokens_used,
        hard_limit=exc.hard_limit,
        path=request.url.path,
    )
    return JSONResponse(
        status_code=429,
        content={
            "detail": "Monthly spend hard limit reached. New spend-incurring work is refused.",
            "tokens_used": exc.tokens_used,
            "hard_limit": exc.hard_limit,
        },
    )


@app.get("/healthz")
def healthz() -> dict:
    """Liveness probe: is this process responsive?
    """
    return {"status": "ok", "workspace_id": settings.workspace_id}


@app.get("/readyz")
def readyz() -> JSONResponse:
    """Readiness probe: can this instance serve a request right now?
    """
    from sqlalchemy import text as sql

    from org_memory.core.wiring import get_object_store
    from org_memory.db.engine import get_engine

    try:
        with get_engine().connect() as connection:
            connection.execute(sql("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        logger.error("readyz.database_unreachable", error=str(exc))
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "database": "unreachable"},
        )
    try:
        get_object_store().ping()
    except Exception as exc:  # noqa: BLE001
        logger.error("readyz.object_store_unreachable", error=str(exc))
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "object_store": "unreachable"},
        )
    return JSONResponse(
        status_code=200,
        content={
            "status": "ready",
            "database": "reachable",
            "object_store": "reachable",
        },
    )


@app.get("/metrics")
def metrics(x_api_key: str = Header(default="")) -> Response:
    """Prometheus text exposition. Same trust as admin (SERVICE_API_KEY)."""
    from sqlalchemy import text as sql

    from org_memory.db.engine import session_scope
    from org_memory.db.repositories import JobRepository, SpendRepository

    if not hmac.compare_digest(x_api_key, settings.service_api_key):
        return JSONResponse(status_code=401, content={"detail": "Invalid or missing X-Api-Key header."})

    with session_scope() as session:
        embed_backlog = session.execute(
            sql(
                "SELECT count(*) AS n FROM chunks "
                "WHERE embedding IS NULL AND deleted = false AND chunk_role = 'child'"
            )
        ).fetchone()
        EMBED_BACKLOG.set(int(embed_backlog.n) if embed_backlog else 0)
        for status, job_type, n in JobRepository(session).counts_by_status_and_type():
            JOBS_BY_STATUS.labels(status=status, job_type=job_type).set(n)
        tokens = sum(SpendRepository(session).totals_by_class_this_month().values())
        SPEND_TOKENS_MONTH.set(tokens)
        over = 1.0 if tokens >= settings.spend_alert_tokens_monthly else 0.0
        SPEND_ALERT.set(over)
        hard = 1.0 if tokens >= settings.spend_hard_limit_tokens_monthly else 0.0
        SPEND_HARD_LIMIT_HIT.set(hard)
        if over:
            logger.error(
                "spend.alert_threshold_exceeded",
                tokens_used_this_month=tokens,
                threshold=settings.spend_alert_tokens_monthly,
            )
        if hard:
            logger.error(
                "spend.hard_limit_reached",
                tokens_used_this_month=tokens,
                hard_limit=settings.spend_hard_limit_tokens_monthly,
            )
        RETENTION_UNSET.set(1.0 if settings.retention_days == 0 else 0.0)

    body, content_type = metrics_payload()
    return Response(content=body, media_type=content_type)
