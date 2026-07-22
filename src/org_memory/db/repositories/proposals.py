"""taxonomy_proposals persistence."""

from __future__ import annotations

from sqlalchemy.orm import Session

from org_memory.core.errors import NotFoundError
from org_memory.core.settings import get_settings
from org_memory.db.orm import TaxonomyProposal, utcnow


class TaxonomyProposalRepository:
    def __init__(self, session: Session):
        self._session = session
        self._ws = get_settings().workspace_id

    def get(self, proposal_id: str) -> TaxonomyProposal | None:
        row = self._session.get(TaxonomyProposal, proposal_id)
        if row is not None and row.workspace_id != self._ws:
            return None
        return row

    def pending_for_slot(
        self,
        subject_type: str,
        subject_id: str,
        taxonomy_key: str,
        field_key: str,
    ) -> TaxonomyProposal | None:
        return (
            self._session.query(TaxonomyProposal)
            .filter(
                TaxonomyProposal.workspace_id == self._ws,
                TaxonomyProposal.subject_type == subject_type,
                TaxonomyProposal.subject_id == subject_id,
                TaxonomyProposal.taxonomy_key == taxonomy_key,
                TaxonomyProposal.field_key == field_key,
                TaxonomyProposal.status == "pending",
            )
            .one_or_none()
        )

    def list_pending(self, *, limit: int = 100) -> list[TaxonomyProposal]:
        return (
            self._session.query(TaxonomyProposal)
            .filter(
                TaxonomyProposal.workspace_id == self._ws,
                TaxonomyProposal.status == "pending",
            )
            .order_by(TaxonomyProposal.created_at.asc())
            .limit(limit)
            .all()
        )

    def upsert_pending(self, proposal: TaxonomyProposal) -> TaxonomyProposal:
        """Insert or refresh the open pending row for this binding slot."""
        existing = self.pending_for_slot(
            proposal.subject_type,
            proposal.subject_id,
            proposal.taxonomy_key,
            proposal.field_key,
        )
        if existing is None:
            proposal.workspace_id = self._ws
            self._session.add(proposal)
            self._session.flush()
            return proposal
        if (
            existing.value_text == proposal.value_text
            and existing.source_claim_id == proposal.source_claim_id
            and set(existing.evidence_doc_ids or []) == set(proposal.evidence_doc_ids or [])
        ):
            existing.confidence = max(existing.confidence, proposal.confidence)
            existing.updated_at = utcnow()
            return existing
        # Different value/evidence wins the slot: supersede the old pending row.
        existing.status = "superseded"
        existing.superseded_by_id = ""  # filled after insert
        existing.updated_at = utcnow()
        proposal.workspace_id = self._ws
        self._session.add(proposal)
        self._session.flush()
        existing.superseded_by_id = proposal.proposal_id
        return proposal

    def mark_applied(self, proposal_id: str, decided_by: str) -> TaxonomyProposal:
        row = self.get(proposal_id)
        if row is None or row.status != "pending":
            raise NotFoundError(f"no pending proposal: {proposal_id}")
        row.status = "applied"
        row.decided_by = decided_by
        row.decided_at = utcnow()
        row.updated_at = utcnow()
        return row

    def mark_rejected(self, proposal_id: str, decided_by: str, reason: str = "") -> TaxonomyProposal:
        row = self.get(proposal_id)
        if row is None or row.status != "pending":
            raise NotFoundError(f"no pending proposal: {proposal_id}")
        row.status = "rejected"
        row.decided_by = decided_by
        row.decided_at = utcnow()
        if reason:
            row.last_push_error = reason[:4000]
        row.updated_at = utcnow()
        return row

    def record_push_error(self, proposal_id: str, error: str) -> None:
        row = self.get(proposal_id)
        if row is None:
            return
        row.last_push_error = error[:4000]
        row.updated_at = utcnow()
