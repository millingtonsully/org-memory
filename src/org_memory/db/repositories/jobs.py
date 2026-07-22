"""Job queue repository: leases, retries, and idempotent enqueue."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text as sql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from org_memory.core.settings import get_settings
from org_memory.db.orm import Job, utcnow
from org_memory.domain.jobs import JobType


class JobRepository:
    """Postgres job queue with leases, retries, and dead-lettering."""

    LEASE_SECONDS = 300
    BACKOFF_BASE_SECONDS = 30
    BACKOFF_CAP_SECONDS = 3600

    def __init__(self, session: Session):
        self._session = session

    def enqueue(self, job_type: JobType | str, payload: dict) -> str:
        job_type = JobType(job_type).value
        existing = self._find_open_duplicate(job_type, payload)
        if existing is not None:
            return self._refresh_open_job(existing, payload)
        job = Job(
            workspace_id=get_settings().workspace_id,
            job_type=job_type,
            payload=payload,
        )
        try:
            with self._session.begin_nested():
                self._session.add(job)
                self._session.flush()  # expose job_id before commit
            return job.job_id
        except IntegrityError:
            raced = self._find_open_duplicate(job_type, payload)
            if raced is None:
                raise
            return self._refresh_open_job(raced, payload)

    def _refresh_open_job(self, job: Job, payload: dict) -> str:
        job.payload = payload
        job.run_after = utcnow()
        job.updated_at = utcnow()
        if job.status == "running" and (job.locked_until is None or job.locked_until < utcnow()):
            job.status = "pending"
            job.locked_until = None
        return job.job_id

    def _find_open_duplicate(self, job_type: str, payload: dict) -> Job | None:
        ws = get_settings().workspace_id
        match_sql: str | None = None
        params: dict = {"ws": ws}
        if job_type in (
            JobType.extract_graph.value,
            JobType.embed_chunks.value,
        ):
            doc_id = payload.get("doc_id")
            if not doc_id:
                return None
            match_sql = "payload->>'doc_id' = :doc_id"
            params["doc_id"] = doc_id
        elif job_type == JobType.adjudicate_persons.value:
            person_a = payload.get("person_a")
            person_b = payload.get("person_b")
            if not person_a or not person_b:
                return None
            # The pair is unordered: a<->b is the same decision as b<->a.
            low, high = sorted([person_a, person_b])
            match_sql = (
                "least(payload->>'person_a', payload->>'person_b') = :low "
                "AND greatest(payload->>'person_a', payload->>'person_b') = :high"
            )
            params.update({"low": low, "high": high})
        elif job_type == JobType.resolve_claim_conflict.value:
            subject_type = payload.get("subject_type")
            subject_id = payload.get("subject_id")
            predicate = payload.get("predicate")
            if not subject_type or not subject_id or not predicate:
                return None
            match_sql = (
                "payload->>'subject_type' = :subject_type "
                "AND payload->>'subject_id' = :subject_id "
                "AND payload->>'predicate' = :predicate"
            )
            params.update(
                {
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "predicate": predicate,
                }
            )
        elif job_type in (
            JobType.generate_taxonomy_proposals.value,
            JobType.aggregate_collaboration_edges.value,
            JobType.push_taxonomy_proposal_webhook.value,
        ):
            match_sql = "true"
        elif job_type == JobType.refresh_identity_embedding.value:
            person_id = payload.get("person_id")
            if not person_id:
                return None
            match_sql = "payload->>'person_id' = :person_id"
            params["person_id"] = person_id
        if match_sql is None:
            return None
        row = self._session.execute(
            sql(f"""
                SELECT job_id FROM jobs
                WHERE workspace_id = :ws
                  AND job_type = :job_type
                  AND status IN ('pending', 'running')
                  AND {match_sql}
                ORDER BY created_at DESC
                LIMIT 1
                FOR UPDATE
            """),
            {**params, "job_type": job_type},
        ).fetchone()
        if row is None:
            return None
        job = self._session.get(Job, row.job_id)
        assert job is not None
        return job

    def heartbeat(self, job: Job, lease_seconds: int | None = None) -> None:
        from sqlalchemy.orm.attributes import instance_state, set_committed_value
        from sqlalchemy.orm.exc import UnmappedInstanceError

        from org_memory.db.engine import get_engine

        until = utcnow() + timedelta(seconds=lease_seconds or self.LEASE_SECONDS)
        now = utcnow()
        with get_engine().connect() as conn:
            conn.execute(
                sql("""
                    UPDATE jobs
                    SET locked_until = :until, updated_at = :now
                    WHERE job_id = :job_id
                      AND status = 'running'
                """),
                {"until": until, "now": now, "job_id": job.job_id},
            )
            conn.commit()
        try:
            instance_state(job)
        except (UnmappedInstanceError, AttributeError):
            job.locked_until = until
            job.updated_at = now
        else:
            set_committed_value(job, "locked_until", until)
            set_committed_value(job, "updated_at", now)

    def claim_next(self, job_types: list[str]) -> Job | None:
        ws = get_settings().workspace_id
        row = self._session.execute(
            sql("""
                SELECT job_id FROM jobs
                WHERE workspace_id = :workspace_id
                  AND job_type = ANY(:job_types)
                  AND (
                        (status = 'pending' AND run_after <= now())
                     OR (status = 'running' AND locked_until IS NOT NULL
                                            AND locked_until < now())
                  )
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            """),
            {"job_types": job_types, "workspace_id": ws},
        ).fetchone()
        if row is None:
            return None
        job = self._session.get(Job, row.job_id)
        assert job is not None  # row locked by FOR UPDATE
        job.status = "running"
        job.attempts += 1
        job.locked_until = utcnow() + timedelta(seconds=self.LEASE_SECONDS)
        job.updated_at = utcnow()
        return job

    def mark_done(self, job: Job) -> None:
        job.status = "done"
        job.locked_until = None
        job.updated_at = utcnow()

    def mark_failed(self, job: Job, error: str, raw_error: str = "") -> None:
        # Retry with backoff, then dead-letter
        job.last_error = error[:4000]
        job.raw_error = raw_error
        job.locked_until = None
        if job.attempts >= job.max_attempts:
            job.status = "dead"
        else:
            backoff = min(
                self.BACKOFF_BASE_SECONDS * (2 ** (job.attempts - 1)),
                self.BACKOFF_CAP_SECONDS,
            )
            job.status = "pending"
            job.run_after = utcnow() + timedelta(seconds=backoff)
        job.updated_at = utcnow()

    def counts_by_status(self) -> dict[str, int]:
        rows = self._session.execute(sql("SELECT status, count(*) AS n FROM jobs GROUP BY status")).fetchall()
        return {r.status: int(r.n) for r in rows}

    def counts_by_status_and_type(self) -> list[tuple[str, str, int]]:
        rows = self._session.execute(
            sql("SELECT status, job_type, count(*) AS n FROM jobs GROUP BY status, job_type")
        ).fetchall()
        return [(r.status, r.job_type, int(r.n)) for r in rows]

    def worker_lag_snapshot(self) -> dict:
        """Ops view of queue freshness derived from jobs rows (API-scrapable)."""
        ws = get_settings().workspace_id
        row = self._session.execute(
            sql("""
                SELECT
                  (SELECT count(*) FROM jobs
                    WHERE workspace_id = :ws AND status = 'pending') AS pending,
                  (SELECT count(*) FROM jobs
                    WHERE workspace_id = :ws AND status = 'running') AS running,
                  (SELECT count(*) FROM jobs
                    WHERE workspace_id = :ws AND status = 'dead') AS dead,
                  (SELECT min(created_at) FROM jobs
                    WHERE workspace_id = :ws AND status = 'pending') AS oldest_pending_at,
                  (SELECT max(updated_at) FROM jobs
                    WHERE workspace_id = :ws
                      AND status IN ('running', 'done', 'dead', 'cancelled')) AS last_job_activity_at
            """),
            {"ws": ws},
        ).fetchone()
        assert row is not None
        return {
            "pending": int(row.pending or 0),
            "running": int(row.running or 0),
            "dead": int(row.dead or 0),
            "oldest_pending_at": (
                row.oldest_pending_at.isoformat() if row.oldest_pending_at else None
            ),
            "last_job_activity_at": (
                row.last_job_activity_at.isoformat() if row.last_job_activity_at else None
            ),
        }

    def cancel(self, job: Job) -> None:
        """Terminal cancel for pending or running jobs."""
        if job.status not in ("pending", "running"):
            raise ValueError(f"cannot cancel job in status {job.status!r}")
        job.status = "cancelled"
        job.locked_until = None
        job.updated_at = utcnow()


