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


def handle_push_taxonomy_proposal_webhook(session: Session, payload: dict) -> bool:
    """Deliver pending proposals; return True if another push pass is needed.

    Always loads ``list_pending`` (payload ``proposal_ids`` are not the source
    of truth). After a successful push, any pending rows that were not in this
    batch — typically created while the singleton push job was running — require
    a follow-up job. The worker enqueues that follow-up only after ``mark_done``
    so singleton dedupe does not collapse it into this lease.
    """
    from org_memory.services.proposal_webhook import push_proposals_if_configured

    if not (get_settings().taxonomy_proposal_webhook_url or "").strip():
        return False
    repo = TaxonomyProposalRepository(session)
    pending = repo.list_pending(limit=200)
    if not pending:
        return False
    pushed_ids = {row.proposal_id for row in pending}
    # Failures raise so the job queue retries or dead-letters.
    push_proposals_if_configured(repo, pending, raise_on_error=True)
    # See proposals committed by other sessions while we were pushing.
    session.expire_all()
    leftovers = [
        row
        for row in repo.list_pending(limit=200)
        if row.proposal_id not in pushed_ids
    ]
    if leftovers:
        logger.info(
            "worker.taxonomy_webhook_needs_another_pass",
            leftover=len(leftovers),
        )
        return True
    return False
