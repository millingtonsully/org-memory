"""Worker entry point: poll jobs, dispatch handlers, run retention when configured.

Retention runs when RETENTION_DAYS > 0 (see settings).
Run with: python -m org_memory.workers.run
"""

from __future__ import annotations

import time
from collections.abc import Callable

import structlog
from sqlalchemy.orm import Session

from org_memory.core.errors import ConfigurationError
from org_memory.core.logging import configure_logging
from org_memory.core.settings import Settings, get_settings
from org_memory.core.wiring import get_embedder, get_synthesizer, make_object_store
from org_memory.db.engine import session_scope
from org_memory.db.orm import EMBEDDING_DIM, Job
from org_memory.db.repositories import JobRepository
from org_memory.domain.jobs import JobType
from org_memory.services.retention import RetentionService
from org_memory.taxonomy_registry import get_taxonomy_registry
from org_memory.workers.handlers import (
    handle_adjudicate_persons,
    handle_aggregate_collaboration_edges,
    handle_embed_chunks,
    handle_extract_graph,
    handle_generate_taxonomy_proposals,
    handle_push_taxonomy_proposal_webhook,
    handle_refresh_identity_embedding,
    handle_resolve_claim_conflict,
    handle_resolve_relationship_conflict,
)

logger = structlog.get_logger(__name__)

_POLL_SECONDS = 1.0
_RETENTION_INTERVAL_SECONDS = 24 * 3600
_JOB_TYPES = [job_type.value for job_type in JobType]

# A handler receives the session, the claimed job, and its repository (for
# heartbeats). Keying dispatch off JobType means adding a job type is a single
# registry entry, and an unregistered type fails loudly instead of silently.
JobHandler = Callable[[Session, Job, JobRepository], None]


class Worker:
    def __init__(self, settings: Settings):
        if settings.embedding_dimensions != EMBEDDING_DIM:
            raise ConfigurationError(
                f"EMBEDDING_DIMENSIONS={settings.embedding_dimensions} does not match "
                f"the schema's vector({EMBEDDING_DIM}) column. Refusing to embed "
                "into the wrong space."
            )
        self._settings = settings
        self._embedder = get_embedder()
        self._synthesizer = get_synthesizer()
        self._last_retention_run = 0.0
        self._handlers: dict[JobType, JobHandler] = {
            JobType.embed_chunks: lambda session, job, jobs: handle_embed_chunks(
                session, job.payload, self._embedder, heartbeat=lambda: jobs.heartbeat(job)
            ),
            JobType.extract_graph: lambda session, job, jobs: handle_extract_graph(
                session,
                job.payload,
                self._synthesizer,
                self._embedder,
                heartbeat=lambda: jobs.heartbeat(job),
            ),
            JobType.adjudicate_persons: lambda session, job, jobs: handle_adjudicate_persons(
                session, job.payload, self._synthesizer, heartbeat=lambda: jobs.heartbeat(job)
            ),
            JobType.resolve_claim_conflict: lambda session, job, jobs: handle_resolve_claim_conflict(
                session, job.payload, self._synthesizer, heartbeat=lambda: jobs.heartbeat(job)
            ),
            JobType.resolve_relationship_conflict: lambda session, job, jobs: (
                handle_resolve_relationship_conflict(
                    session, job.payload, heartbeat=lambda: jobs.heartbeat(job)
                )
            ),
            JobType.generate_taxonomy_proposals: lambda session, job, jobs: (
                handle_generate_taxonomy_proposals(session, job.payload)
            ),
            JobType.push_taxonomy_proposal_webhook: lambda session, job, jobs: (
                handle_push_taxonomy_proposal_webhook(session, job.payload)
            ),
            JobType.aggregate_collaboration_edges: lambda session, job, jobs: (
                handle_aggregate_collaboration_edges(session, job.payload)
            ),
            JobType.refresh_identity_embedding: lambda session, job, jobs: (
                handle_refresh_identity_embedding(
                    session, job.payload, self._embedder, heartbeat=lambda: jobs.heartbeat(job)
                )
            ),
        }

    def process_one(self) -> bool:
        with session_scope() as session:
            jobs = JobRepository(session)
            job = jobs.claim_next(_JOB_TYPES)
            if job is None:
                return False
            try:
                handler = self._handlers[JobType(job.job_type)]
                handler(session, job, jobs)
                jobs.mark_done(job)
            except Exception as exc:  # noqa: BLE001
                jobs.mark_failed(
                    job,
                    f"{type(exc).__name__}: {exc}",
                    raw_error=str(getattr(exc, "raw_response", ""))[:2000],
                )
                logger.error(
                    "worker.job_failed",
                    job_id=job.job_id,
                    job_type=job.job_type,
                    attempt=job.attempts,
                    status=job.status,
                    error=str(exc),
                )
            return True

    def maybe_run_retention(self) -> None:
        if self._settings.retention_days <= 0:
            return
        now = time.monotonic()
        if now - self._last_retention_run < _RETENTION_INTERVAL_SECONDS:
            return
        self._last_retention_run = now
        try:
            with session_scope() as session:
                RetentionService(session, make_object_store()).purge_expired()
        except Exception as exc:  # noqa: BLE001
            logger.error("worker.retention_failed", error=str(exc))

    def run_forever(self) -> None:
        from org_memory.core.metrics import WORKER_HEARTBEAT

        logger.info(
            "worker.started",
            job_types=_JOB_TYPES,
            retention_days=self._settings.retention_days,
        )
        while True:
            WORKER_HEARTBEAT.set_to_current_time()
            self.maybe_run_retention()
            if not self.process_one():
                time.sleep(_POLL_SECONDS)


def main() -> None:
    configure_logging()
    get_taxonomy_registry()
    Worker(get_settings()).run_forever()


if __name__ == "__main__":
    main()
