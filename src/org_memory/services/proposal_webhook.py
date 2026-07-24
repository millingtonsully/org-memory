from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import structlog

from org_memory.core.settings import get_settings
from org_memory.db.orm import TaxonomyProposal
from org_memory.db.repositories.proposals import TaxonomyProposalRepository

logger = structlog.get_logger(__name__)


def proposal_payload(row: TaxonomyProposal) -> dict:
    return {
        "proposal_id": row.proposal_id,
        "subject_type": row.subject_type,
        "subject_id": row.subject_id,
        "host_entity_id": getattr(row, "host_entity_id", "") or None,
        "taxonomy_key": row.taxonomy_key,
        "field_key": row.field_key,
        "predicate": row.predicate,
        "value": row.value_text,
        "confidence": row.confidence,
        "evidence_doc_ids": list(row.evidence_doc_ids or []),
        "source_claim_id": row.source_claim_id,
        "precedence_class": row.precedence_class,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _sign_body(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def push_proposals_if_configured(
    repo: TaxonomyProposalRepository,
    proposals: list[TaxonomyProposal],
    *,
    raise_on_error: bool = False,
) -> None:
    settings = get_settings()
    url = (settings.taxonomy_proposal_webhook_url or "").strip()
    if not url or not proposals:
        return
    body = {"proposals": [proposal_payload(p) for p in proposals if p.status == "pending"]}
    if not body["proposals"]:
        return
    payload = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    secret = (settings.taxonomy_proposal_webhook_secret or "").strip()
    if secret:
        headers["X-Org-Memory-Signature"] = _sign_body(payload, secret)
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(url, content=payload, headers=headers)
        if response.status_code >= 400:
            error = f"webhook HTTP {response.status_code}: {response.text[:500]}"
            logger.error("taxonomy_proposals.webhook_failed", error=error)
            for p in proposals:
                repo.record_push_error(p.proposal_id, error)
            if raise_on_error:
                raise RuntimeError(error)
            return
        logger.info("taxonomy_proposals.webhook_ok", count=len(body["proposals"]))
    except httpx.HTTPError as exc:
        error = f"webhook transport error: {exc}"
        logger.error("taxonomy_proposals.webhook_failed", error=error)
        for p in proposals:
            repo.record_push_error(p.proposal_id, error)
        if raise_on_error:
            raise RuntimeError(error) from exc
