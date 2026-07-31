"""Taxonomy proposal jobs: materialize write-back rows and deliver the webhook."""

from __future__ import annotations

import structlog
from sqlalchemy.orm import Session

from org_memory.core.settings import get_settings
from org_memory.db.repositories import JobRepository, TaxonomyProposalRepository
from org_memory.domain.jobs import JobType

logger = structlog.get_logger(__name__)


def handle_generate_taxonomy_proposals(session: Session, payload: dict) -> None:
    """Materialize pending taxonomy write-back rows from active bound claims."""
    from org_memory.services.taxonomy_proposals import TaxonomyProposalService

    summary = TaxonomyProposalService(session).generate_from_active_claims()
    JobRepository(session).enqueue(JobType.push_taxonomy_proposal_webhook, {})
    log_fields = {k: v for k, v in summary.items() if k != "proposal_ids"}
    logger.info("worker.taxonomy_proposals_generated", **log_fields)


def handle_push_taxonomy_proposal_webhook(session: Session, payload: dict) -> None:
    """Deliver pending proposals to TAXONOMY_PROPOSAL_WEBHOOK_URL with job retries."""
    from org_memory.services.proposal_webhook import push_proposals_if_configured

    if not (get_settings().taxonomy_proposal_webhook_url or "").strip():
        return
    repo = TaxonomyProposalRepository(session)
    pending = repo.list_pending(limit=200)
    if not pending:
        return
    # Failures raise so the job queue retries or dead-letters.
    push_proposals_if_configured(repo, pending, raise_on_error=True)
