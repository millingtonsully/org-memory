"""Collaboration aggregation job: rebuild who-works-with-whom edges."""

from __future__ import annotations

import structlog
from sqlalchemy.orm import Session

logger = structlog.get_logger(__name__)


def handle_aggregate_collaboration_edges(session: Session, payload: dict) -> None:
    from org_memory.services.collaboration import CollaborationService

    summary = CollaborationService(session).rebuild_edges()
    logger.info("worker.collaboration_aggregated", **summary)
