"""Admin job queue routes under /v1/admin."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from org_memory.api.deps import bind_admin, get_session
from org_memory.core.errors import NotFoundError
from org_memory.db.orm import Job, utcnow
from org_memory.db.repositories import AuditRepository, JobRepository
from org_memory.domain.models import Principal

logger = structlog.get_logger(__name__)

router = APIRouter()


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
                "raw_error": (j.raw_error or "")[:2000],
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
