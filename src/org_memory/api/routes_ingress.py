"""Envelope ingress: single write path into organizational memory.

POST /ingress/envelope accepts a Change Envelope. Downstream chunking, embedding,
and scoping run automatically. No principal header; visibility comes from the envelope.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from org_memory.api.deps import get_ingest_service, require_api_key
from org_memory.db.engine import session_scope
from org_memory.db.repositories import ConnectorStatusRepository
from org_memory.domain.models import ChangeEnvelope
from org_memory.services.ingest import IngestService

router = APIRouter(dependencies=[Depends(require_api_key)])


class IngressResponse(BaseModel):
    doc_id: str
    status: str


@router.post("/ingress/envelope", response_model=IngressResponse)
def ingest_envelope(
    envelope: ChangeEnvelope,
    ingest: IngestService = Depends(get_ingest_service),
) -> IngressResponse:
    from org_memory.core.metrics import INGEST_FAIL, INGEST_OK

    try:
        doc_id = ingest.ingest_envelope(
            envelope,
            raw_payload=envelope.model_dump_json().encode("utf-8"),
        )
    except Exception as exc:
        INGEST_FAIL.inc()
        # Record failure in a separate session after ingest rollback.
        with session_scope() as session:
            ConnectorStatusRepository(session).record_failure(
                envelope.source_system, f"{type(exc).__name__}: {exc}"
            )
        raise
    INGEST_OK.inc()
    return IngressResponse(doc_id=doc_id, status="accepted")
