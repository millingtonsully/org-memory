"""Generate taxonomy_proposals (field-value write-backs) from active registry-bound claims."""

from __future__ import annotations

import structlog
from sqlalchemy.orm import Session

from org_memory.core.settings import get_settings
from org_memory.db.orm import Claim, Document, TaxonomyProposal
from org_memory.db.repositories import GraphRepository
from org_memory.db.repositories.proposals import TaxonomyProposalRepository
from org_memory.domain.proposals import (
    ProposalCandidate,
    pick_slot_winners,
    precedence_class_name,
)
from org_memory.taxonomy_registry import get_taxonomy_registry

logger = structlog.get_logger(__name__)


class TaxonomyProposalService:
    def __init__(self, session: Session):
        self._session = session
        self._graph = GraphRepository(session)
        self._proposals = TaxonomyProposalRepository(session)

    def generate_from_active_claims(self, *, min_confidence: float | None = None) -> dict:
        """Scan active claims, gate, pick winners, upsert pending proposals."""
        settings = get_settings()
        threshold = (
            min_confidence
            if min_confidence is not None
            else settings.fact_activation_confidence
        )
        registry = get_taxonomy_registry()
        claims = (
            self._session.query(Claim)
            .filter(
                Claim.workspace_id == settings.workspace_id,
                Claim.status == "active",
            )
            .all()
        )

        candidates: list[ProposalCandidate] = []
        skipped = {
            "no_binding": 0,
            "low_confidence": 0,
            "no_evidence": 0,
            "evidence_missing": 0,
            "untyped": 0,
        }
        for claim in claims:
            if claim.created_by == "extraction:untyped":
                skipped["untyped"] += 1
                continue
            pred = registry.predicates.get(claim.predicate)
            if pred is None or pred.platform_binding is None:
                skipped["no_binding"] += 1
                continue
            if claim.confidence < threshold and not claim.created_by.startswith("structured_field"):
                skipped["low_confidence"] += 1
                continue
            evidence = list(claim.evidence_doc_ids or [])
            if not evidence:
                skipped["no_evidence"] += 1
                continue
            if not self._evidence_docs_exist(evidence):
                skipped["evidence_missing"] += 1
                continue
            candidates.append(
                ProposalCandidate(
                    claim_id=claim.claim_id,
                    subject_type=claim.subject_type,
                    subject_id=claim.subject_id,
                    predicate=claim.predicate,
                    value_text=claim.object_text,
                    confidence=claim.confidence,
                    evidence_doc_ids=evidence,
                    created_by=claim.created_by,
                    latest_evidence_at=self._graph.latest_evidence_time(evidence),
                    taxonomy_key=pred.platform_binding.taxonomy_key,
                    field_key=pred.platform_binding.field_key,
                )
            )

        winners = pick_slot_winners(candidates)
        created = 0
        refreshed = 0
        proposal_ids: list[str] = []
        for winner in winners:
            before = self._proposals.pending_for_slot(
                winner.subject_type,
                winner.subject_id,
                winner.taxonomy_key,
                winner.field_key,
            )
            stored = self._proposals.upsert_pending(
                TaxonomyProposal(
                    subject_type=winner.subject_type,
                    subject_id=winner.subject_id,
                    taxonomy_key=winner.taxonomy_key,
                    field_key=winner.field_key,
                    predicate=winner.predicate,
                    value_text=winner.value_text,
                    confidence=winner.confidence,
                    evidence_doc_ids=sorted(set(winner.evidence_doc_ids)),
                    source_claim_id=winner.claim_id,
                    precedence_class=precedence_class_name(winner.rank),
                    status="pending",
                )
            )
            proposal_ids.append(stored.proposal_id)
            if before is None:
                created += 1
            elif before.proposal_id == stored.proposal_id:
                refreshed += 1
            else:
                created += 1

        summary = {
            "candidates": len(candidates),
            "winners": len(winners),
            "created_or_replaced": created,
            "refreshed": refreshed,
            "skipped": skipped,
            "proposal_ids": proposal_ids,
        }
        log_fields = {k: v for k, v in summary.items() if k != "proposal_ids"}
        logger.info("taxonomy_proposals.generated", **log_fields)
        return summary

    def _evidence_docs_exist(self, doc_ids: list[str]) -> bool:
        """Existence gate: every evidence doc must still exist and not be tombstoned.

        This is not viewer ACL. Taxonomy proposal list/pull is a service-key ops
        surface; the host applies under its own auth.
        """
        rows = (
            self._session.query(Document.doc_id)
            .filter(
                Document.doc_id.in_(doc_ids),
                Document.deleted == False,  # noqa: E712
            )
            .all()
        )
        return len({r.doc_id for r in rows}) == len(set(doc_ids))
