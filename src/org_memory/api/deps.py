"""FastAPI dependencies for auth, viewer identity, and service wiring.

Every request checks X-Api-Key (caller allowed) and X-Principal-Id (viewer
to scope retrieval). Missing principal returns 400.
"""

from __future__ import annotations

import hmac
from collections.abc import Iterator

from fastapi import Depends, Header, HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from org_memory.core.settings import get_settings
from org_memory.core.wiring import (
    get_embedder,
    get_object_store,
    get_reranker,
    get_synthesizer,
)
from org_memory.db.engine import session_scope
from org_memory.db.repositories import (
    AuditRepository,
    ChunkSearchRepository,
    GraphRepository,
    PersonRepository,
)
from org_memory.domain.models import Principal
from org_memory.services.entity_resolution import EntityResolutionService
from org_memory.services.ingest import IngestService
from org_memory.services.procedural_memory import ProceduralMemoryService
from org_memory.services.retrieval import RetrievalService
from org_memory.services.worldbuilder import WorldbuilderService

__all__ = [
    "get_embedder",
    "get_reranker",
    "get_synthesizer",
    "get_object_store",
    "require_api_key",
    "bind_principal",
    "bind_admin",
    "get_session",
    "get_retrieval_service",
    "get_ingest_service",
    "get_worldbuilder_service",
    "get_procedural_memory_service",
]


def require_api_key(x_api_key: str = Header(default="")) -> None:
    # Constant-time compare avoids leaking key prefixes via timing.
    if not hmac.compare_digest(x_api_key, get_settings().service_api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Api-Key header.")


def bind_principal(
    x_principal_id: str = Header(default=""),
    x_principal_groups: str = Header(default=""),
) -> Principal:
    """Require a viewer principal on retrieval paths.

    Trust boundary: X-Api-Key authenticates the calling service. That service
    must set truthful X-Principal-Id / X-Principal-Groups for the human viewer. Values must
    be platform-form principals: user:<uuid> / group:<uuid>.
    """
    if not x_principal_id.strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "X-Principal-Id header is required: every retrieval must be "
                "scoped to the human viewer it acts for."
            ),
        )
    groups = [g.strip() for g in x_principal_groups.split(",") if g.strip()]
    try:
        return Principal(principal_id=x_principal_id.strip(), groups=groups)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc.errors()[0]["msg"])) from exc


def bind_admin(
    principal: Principal = Depends(bind_principal),
    x_principal_roles: str = Header(default=""),
) -> Principal:
    """Require the admin role for explicit governance operations."""
    roles = {r.strip().lower() for r in x_principal_roles.split(",") if r.strip()}
    if "admin" not in roles:
        raise HTTPException(
            status_code=403,
            detail=(
                "This endpoint requires the 'admin' role: pass "
                "X-Principal-Roles: admin for principals allowed to manage "
                "overrides, jobs, audits, retention, and legal holds."
            ),
        )
    return principal


def get_session() -> Iterator[Session]:
    with session_scope() as session:
        yield session


def get_retrieval_service(session: Session = Depends(get_session)) -> RetrievalService:
    return RetrievalService(
        search_repo=ChunkSearchRepository(session),
        audit_repo=AuditRepository(session),
        embedder=get_embedder(),
        reranker=get_reranker(),
        graph_repo=GraphRepository(session),
        person_repo=PersonRepository(session),
    )


def get_ingest_service(session: Session = Depends(get_session)) -> IngestService:
    return IngestService(
        session=session,
        object_store=get_object_store(),
        entity_resolution=EntityResolutionService(session, get_embedder()),
    )


def get_worldbuilder_service(
    session: Session = Depends(get_session),
) -> WorldbuilderService:
    retrieval = RetrievalService(
        search_repo=ChunkSearchRepository(session),
        audit_repo=AuditRepository(session),
        embedder=get_embedder(),
        reranker=get_reranker(),
        graph_repo=GraphRepository(session),
        person_repo=PersonRepository(session),
    )
    return WorldbuilderService(
        session=session,
        retrieval=retrieval,
        synthesizer=get_synthesizer(),
    )


def get_procedural_memory_service(
    session: Session = Depends(get_session),
) -> ProceduralMemoryService:
    return ProceduralMemoryService(
        session=session,
        synthesizer=get_synthesizer(),
        embedder=get_embedder(),
        reranker=get_reranker(),
    )
