"""Audit read API for retrievals, synthesis traces, and document versions.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from org_memory.api.deps import bind_admin, get_session, require_api_key
from org_memory.core.errors import NotFoundError
from org_memory.db.repositories import (
    AuditRepository,
    DocumentRepository,
    DocumentVersionRepository,
    PersonMergeDecisionRepository,
    SynthesisTraceRepository,
)
from org_memory.domain.models import Principal

router = APIRouter(prefix="/v1/audit", dependencies=[Depends(require_api_key)])


@router.get("/person-merge-decisions")
def list_person_merge_decisions(
    limit: int = Query(default=100, le=500),
    principal: Principal = Depends(bind_admin),
    session: Session = Depends(get_session),
) -> dict:
    """Machine-readable audit; no approval queue or manual merge action."""
    decisions = PersonMergeDecisionRepository(session).recent(limit)
    return {
        "decisions": [
            {
                "decision_id": decision.decision_id,
                "person_a": decision.a_id,
                "person_b": decision.b_id,
                "verdict": decision.verdict,
                "confidence": decision.confidence,
                "signals": decision.signals,
                "reason": decision.reason,
                "status": decision.status,
                "decided_by": decision.decided_by,
                "decided_at": (decision.decided_at.isoformat() if decision.decided_at else None),
                "reversed_at": (decision.reversed_at.isoformat() if decision.reversed_at else None),
                "reversal_reason": decision.reversal_reason,
            }
            for decision in decisions
        ]
    }


@router.get("/retrievals")
def list_retrievals(
    principal_id: str = Query(default="", description="Filter to one viewer."),
    limit: int = Query(default=50, le=500),
    principal: Principal = Depends(bind_admin),
    session: Session = Depends(get_session),
) -> dict:
    audits = AuditRepository(session).recent(principal_id or None, limit)
    return {
        "retrievals": [
            {
                "audit_id": a.audit_id,
                "principal_id": a.principal_id,
                "tool": a.tool,
                "query": a.query,
                "params": a.params,
                "result_chunk_ids": a.result_chunk_ids,
                "result_fact_ids": a.result_fact_ids,
                "result_memory_ids": a.result_memory_ids,
                "created_at": a.created_at.isoformat(),
            }
            for a in audits
        ]
    }


@router.get("/synthesis-traces")
def list_synthesis_traces(
    tool: str = Query(default="worldbuilder_lookup"),
    subject: str = Query(description="Subject key, e.g. a person canonical_id."),
    limit: int = Query(default=20, le=100),
    principal: Principal = Depends(bind_admin),
    session: Session = Depends(get_session),
) -> dict:
    traces = SynthesisTraceRepository(session).for_subject(tool, subject, limit)
    return {
        "traces": [
            {
                "trace_id": t.trace_id,
                "principal_id": t.principal_id,
                "tool": t.tool,
                "subject": t.subject,
                "model": t.model,
                "input_doc_ids": t.input_doc_ids,
                "output_text": t.output_text,
                "tokens": t.tokens,
                "created_at": t.created_at.isoformat(),
            }
            for t in traces
        ]
    }


@router.get("/documents/{doc_id:path}/versions")
def document_versions(
    doc_id: str,
    principal: Principal = Depends(bind_admin),
    session: Session = Depends(get_session),
) -> dict:
    if DocumentRepository(session).get(doc_id) is None:
        raise NotFoundError(f"unknown document: {doc_id}")
    versions = DocumentVersionRepository(session).history(doc_id)
    return {
        "doc_id": doc_id,
        "versions": [
            {
                "version_id": v.version_id,
                "change_kind": v.change_kind,
                "event_time": v.event_time.isoformat(),
                "blob_key": v.blob_key,
                "payload_hash": v.payload_hash,
                "ingested_at": v.created_at.isoformat(),
            }
            for v in versions
        ],
    }
