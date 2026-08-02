"""Claim reads, writes, and supersession for the graph repository."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, text as sql
from sqlalchemy.exc import IntegrityError

from org_memory.db.orm import Claim, Document, utcnow
from org_memory.db.repositories.graph.base import (
    VISIBLE_DOCS_CTE,
    GraphRepositoryBase,
    all_visible_sql,
    evidence_lateral_sql,
)
from org_memory.domain.fact_lifecycle import (
    ConflictCandidate,
    FactStatus,
    rank_conflict_candidates,
    transition_fact,
)
from org_memory.domain.models import Principal
from org_memory.domain.proposals import precedence_rank
from org_memory.services.temporality.grain import (
    belief_as_of_sql,
    resolve_validity_query_point,
    validity_as_of_sql,
)
from org_memory.services.temporality.merge import merge_temporal_fields

_LIVE_CLAIM_STATUSES = (
    FactStatus.proposed.value,
    FactStatus.active.value,
    FactStatus.retracted.value,
)
_CLAIM_VALIDITY_AS_OF = validity_as_of_sql("c")
_CLAIM_BELIEF_AS_OF = belief_as_of_sql("c")
_CLAIM_ALL_VISIBLE_SQL = all_visible_sql("c")
_CLAIM_EVIDENCE_LATERAL = evidence_lateral_sql("c")


class GraphClaimsMixin(GraphRepositoryBase):
    """Claim lifecycle and viewer-scoped claim reads."""

    def add_claim(self, claim: Claim) -> Claim:
        """Dedupe facts and advance a proposal when stronger evidence arrives.

        Live rows sharing the same (subject, predicate, object_text) collapse into
        one keeper (evidence merged; extras superseded). Concurrent inserts that
        race the unique live-object index merge into the winner via IntegrityError.
        """
        existing = self._live_claims_for_object_locked(
            claim.subject_type,
            claim.subject_id,
            claim.predicate,
            claim.object_text,
        )
        if existing:
            keeper = self.collapse_live_claims_for_object(existing)
            return self._merge_incoming_into_claim(keeper, claim)

        claim.workspace_id = self._ws
        try:
            with self._session.begin_nested():
                self._session.add(claim)
                self._session.flush()
            return claim
        except IntegrityError:
            raced = self._live_claims_for_object_locked(
                claim.subject_type,
                claim.subject_id,
                claim.predicate,
                claim.object_text,
            )
            if not raced:
                raise
            keeper = self.collapse_live_claims_for_object(raced)
            return self._merge_incoming_into_claim(keeper, claim)

    def merge_claim_evidence(self, keeper: Claim, donor: Claim) -> None:
        """Union evidence, quotes, confidence, and temporal fields into keeper."""
        merged = set(keeper.evidence_doc_ids or []) | set(donor.evidence_doc_ids or [])
        keeper.evidence_doc_ids = sorted(merged)
        quotes = {
            (str(item.get("doc_id", "")), str(item.get("quote", ""))): item
            for item in [*(keeper.evidence_quotes or []), *(donor.evidence_quotes or [])]
        }
        keeper.evidence_quotes = list(quotes.values())
        keeper.confidence = max(keeper.confidence, donor.confidence)
        merge_temporal_fields(keeper, donor)

    def collapse_live_claims_for_object(self, rows: list[Claim]) -> Claim:
        """Keep one live row for a same-object group; supersede the rest."""
        if not rows:
            raise ValueError("collapse_live_claims_for_object requires at least one row")
        if len(rows) == 1:
            return rows[0]
        keeper = self._pick_claim_keeper(rows)
        for rival in rows:
            if rival.claim_id == keeper.claim_id:
                continue
            self.merge_claim_evidence(keeper, rival)
            if rival.status != FactStatus.superseded.value:
                self.supersede_claim(
                    rival, keeper.claim_id, "automatic:duplicate_collapse"
                )
        keeper.updated_at = utcnow()
        self._session.flush()
        return keeper

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

    def active_claim_count(self, subject_type: str, subject_id: str, predicate: str) -> int:
        """Count of active claim rows for one (subject, predicate), including duplicates."""
        return (
            self._session.query(func.count(Claim.claim_id))
            .filter(
                Claim.workspace_id == self._ws,
                Claim.subject_type == subject_type,
                Claim.subject_id == subject_id,
                Claim.predicate == predicate,
                Claim.status == FactStatus.active.value,
            )
            .scalar()
            or 0
        )

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

    def _live_claims_for_object_locked(
        self,
        subject_type: str,
        subject_id: str,
        predicate: str,
        object_text: str,
    ) -> list[Claim]:
        return (
            self._session.query(Claim)
            .filter(
                Claim.workspace_id == self._ws,
                Claim.subject_type == subject_type,
                Claim.subject_id == subject_id,
                Claim.predicate == predicate,
                Claim.object_text == object_text,
                Claim.status.in_(_LIVE_CLAIM_STATUSES),
            )
            .order_by(Claim.claim_id)
            .with_for_update()
            .all()
        )

    def _pick_claim_keeper(self, rows: list[Claim]) -> Claim:
        actives = [c for c in rows if c.status == FactStatus.active.value]
        if len(actives) >= 2:
            ranked = rank_conflict_candidates(
                [
                    ConflictCandidate(
                        claim_id=claim.claim_id,
                        object_text=claim.object_text,
                        confidence=claim.confidence,
                        latest_evidence_at=self.latest_evidence_time(
                            claim.evidence_doc_ids
                        ),
                        updated_at=claim.updated_at,
                        created_by=claim.created_by or "",
                        evidence_count=len(claim.evidence_doc_ids or []),
                    )
                    for claim in actives
                ]
            )
            return next(c for c in actives if c.claim_id == ranked[0].claim_id)
        status_pref = {
            FactStatus.active.value: 0,
            FactStatus.proposed.value: 1,
            FactStatus.retracted.value: 2,
        }
        return sorted(
            rows,
            key=lambda c: (status_pref.get(c.status, 9), c.claim_id),
        )[0]

    def _merge_incoming_into_claim(self, existing: Claim, claim: Claim) -> Claim:
        self.merge_claim_evidence(existing, claim)
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
        if (
            claim.status == FactStatus.active.value
            and existing.status != FactStatus.active.value
        ):
            transition_fact(
                existing,
                FactStatus.active,
                claim.decided_by or "automatic:confidence_gate",
            )
            existing.invalidated_at = None
        elif (
            existing.status == FactStatus.retracted.value
            and claim.status == FactStatus.proposed.value
        ):
            transition_fact(existing, FactStatus.proposed, "")
            existing.invalidated_at = None
        existing.updated_at = utcnow()
        return existing

    def claims_for_viewer(
        self,
        subject_type: str,
        subject_id: str,
        principal: Principal,
        statuses: list[str] | None = None,
        as_of: datetime | None = None,
        believed_as_of: datetime | None = None,
        as_of_grain: str | None = None,
    ) -> list[tuple[Claim, list[str]]]:
        """Return claims whose *entire* evidence set is visible to this viewer.

        All-visible ACL and grain-aware validity run in SQL so private claims never
        enter the result set (same predicates as hybrid ``fact_candidates``).
        """
        effective_as_of, effective_grain = resolve_validity_query_point(
            as_of=as_of,
            believed_as_of=believed_as_of,
            as_of_grain=as_of_grain,
            now=utcnow(),
        )
        status_clause = (
            "AND c.status = ANY(CAST(:statuses AS text[]))" if statuses else ""
        )
        rows = self._session.execute(
            sql(f"""
                WITH {VISIBLE_DOCS_CTE}
                SELECT c.claim_id, evidence.doc_ids AS evidence_doc_ids
                FROM claims c
                {_CLAIM_EVIDENCE_LATERAL}
                WHERE c.workspace_id = :workspace_id
                  AND c.subject_type = :subject_type
                  AND c.subject_id = :subject_id
                  {status_clause}
                  AND {_CLAIM_VALIDITY_AS_OF}
                  AND {_CLAIM_BELIEF_AS_OF}
                  AND {_CLAIM_ALL_VISIBLE_SQL}
                ORDER BY c.created_at DESC
            """),
            {
                "workspace_id": self._ws,
                "viewer_principals": principal.all_principals(),
                "subject_type": subject_type,
                "subject_id": subject_id,
                "statuses": statuses or [],
                "as_of": effective_as_of,
                "as_of_grain": effective_grain,
                "believed_as_of": believed_as_of,
            },
        ).mappings().all()
        if not rows:
            return []
        by_id = {
            claim.claim_id: claim
            for claim in self._session.query(Claim)
            .filter(
                Claim.workspace_id == self._ws,
                Claim.claim_id.in_([row["claim_id"] for row in rows]),
            )
            .all()
        }
        visible: list[tuple[Claim, list[str]]] = []
        for row in rows:
            claim = by_id.get(row["claim_id"])
            if claim is None:
                continue
            visible.append((claim, list(row["evidence_doc_ids"] or [])))
        return visible
