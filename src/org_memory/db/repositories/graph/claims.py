"""Claim reads, writes, and supersession for the graph repository."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func

from org_memory.db.orm import Claim, Document, utcnow
from org_memory.db.repositories.graph.base import GraphRepositoryBase
from org_memory.domain.fact_lifecycle import FactStatus, transition_fact
from org_memory.domain.proposals import precedence_rank


class GraphClaimsMixin(GraphRepositoryBase):
    """Claim lifecycle and viewer-scoped claim reads."""

    def add_claim(self, claim: Claim) -> Claim:
        """Dedupe facts and advance a proposal when stronger evidence arrives."""
        existing = (
            self._session.query(Claim)
            .filter(
                Claim.workspace_id == self._ws,
                Claim.subject_type == claim.subject_type,
                Claim.subject_id == claim.subject_id,
                Claim.predicate == claim.predicate,
                Claim.object_text == claim.object_text,
                Claim.status.in_(["proposed", "active", "retracted"]),
            )
            .first()
        )
        if existing is not None:
            merged = set(existing.evidence_doc_ids) | set(claim.evidence_doc_ids)
            existing.evidence_doc_ids = sorted(merged)
            quotes = {
                (str(item.get("doc_id", "")), str(item.get("quote", ""))): item
                for item in [*(existing.evidence_quotes or []), *(claim.evidence_quotes or [])]
            }
            existing.evidence_quotes = list(quotes.values())
            existing.confidence = max(existing.confidence, claim.confidence)
            if existing.valid_from is None and claim.valid_from is not None:
                existing.valid_from = claim.valid_from
            merged_count = len(existing.evidence_doc_ids or [])
            incoming_rank = precedence_rank(
                created_by=claim.created_by or "",
                evidence_count=merged_count,
            )
            existing_rank = precedence_rank(
                created_by=existing.created_by or "",
                evidence_count=merged_count,
            )
            if incoming_rank > existing_rank:
                existing.created_by = claim.created_by
                if claim.decided_by:
                    existing.decided_by = claim.decided_by
            if claim.status == FactStatus.active.value and existing.status != FactStatus.active.value:
                transition_fact(
                    existing,
                    FactStatus.active,
                    claim.decided_by or "automatic:confidence_gate",
                )
            elif existing.status == FactStatus.retracted.value and claim.status == FactStatus.proposed.value:
                transition_fact(existing, FactStatus.proposed, "")
            existing.updated_at = utcnow()
            return existing
        claim.workspace_id = self._ws
        self._session.add(claim)
        self._session.flush()
        return claim

    def active_object_texts(self, subject_type: str, subject_id: str, predicate: str) -> list[str]:
        """Distinct object values currently active for one (subject, predicate)."""
        rows = (
            self._session.query(Claim.object_text)
            .filter(
                Claim.workspace_id == self._ws,
                Claim.subject_type == subject_type,
                Claim.subject_id == subject_id,
                Claim.predicate == predicate,
                Claim.status == FactStatus.active.value,
            )
            .distinct()
            .all()
        )
        return [row.object_text for row in rows]

    def active_claims_for_slot_locked(
        self, subject_type: str, subject_id: str, predicate: str
    ) -> list[Claim]:
        """Lock and return active claims for one slot for conflict resolution."""
        return (
            self._session.query(Claim)
            .filter(
                Claim.workspace_id == self._ws,
                Claim.subject_type == subject_type,
                Claim.subject_id == subject_id,
                Claim.predicate == predicate,
                Claim.status == FactStatus.active.value,
            )
            .order_by(Claim.claim_id)
            .with_for_update()
            .all()
        )

    def latest_evidence_time(self, evidence_doc_ids: list[str]) -> datetime | None:
        """Newest event_time among a claim's still-live evidence documents."""
        if not evidence_doc_ids:
            return None
        row = (
            self._session.query(func.max(Document.event_time))
            .filter(
                Document.workspace_id == self._ws,
                Document.doc_id.in_(evidence_doc_ids),
                Document.deleted == False,  # noqa: E712
            )
            .first()
        )
        return row[0] if row is not None else None

    def supersede_claim(
        self,
        claim: Claim,
        superseded_by_claim_id: str,
        decided_by: str,
        *,
        valid_to: datetime | None = None,
    ) -> None:
        """Retire a losing claim in a mutually-exclusive slot. The row stays for audit."""
        winner = self._session.get(Claim, superseded_by_claim_id)
        close_at = valid_to
        if close_at is None and winner is not None:
            close_at = winner.valid_from
        if close_at is None:
            close_at = utcnow()
        claim.valid_to = close_at
        claim.invalidated_at = utcnow()
        transition_fact(claim, FactStatus.superseded, decided_by)
        claim.superseded_by_claim_id = superseded_by_claim_id

    def supersede_slot_rivals(self, winner: Claim, decided_by: str) -> list[Claim]:
        """Supersede only lower-precedence rivals; return equal/higher left active."""
        winner_rank = precedence_rank(
            created_by=winner.created_by or "",
            evidence_count=len(winner.evidence_doc_ids or []),
        )
        rivals = self.active_claims_for_slot_locked(
            winner.subject_type, winner.subject_id, winner.predicate
        )
        leftover: list[Claim] = []
        for rival in rivals:
            if rival.claim_id == winner.claim_id:
                continue
            if rival.object_text == winner.object_text:
                continue
            rival_rank = precedence_rank(
                created_by=rival.created_by or "",
                evidence_count=len(rival.evidence_doc_ids or []),
            )
            if rival_rank >= winner_rank:
                leftover.append(rival)
                continue
            self.supersede_claim(rival, winner.claim_id, decided_by)
        return leftover

