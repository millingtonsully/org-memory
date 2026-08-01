"""Eager exclusive-slot supersession after an active fact is applied."""

from __future__ import annotations

import structlog

from org_memory.db.orm import Claim, Relationship
from org_memory.db.repositories import GraphRepository
from org_memory.domain.fact_lifecycle import (
    ConflictCandidate,
    FactStatus,
    rank_conflict_candidates,
)
from org_memory.taxonomy_registry import get_taxonomy_registry

logger = structlog.get_logger(__name__)


def eager_close_claim_slot(graph: GraphRepository, winner: Claim) -> int:
    """Supersede other active values in a registry-exclusive claim slot.

    Returns the number of rivals superseded. No-op when the predicate is not
    registry-exclusive or the winner is not active.
    """
    if winner.status != FactStatus.active.value:
        return 0
    if get_taxonomy_registry().predicate_mutually_exclusive(winner.predicate) is not True:
        return 0

    claims = graph.active_claims_for_slot_locked(
        winner.subject_type, winner.subject_id, winner.predicate
    )
    if len(claims) < 2:
        return 0

    candidates = [
        ConflictCandidate(
            claim_id=claim.claim_id,
            object_text=claim.object_text,
            confidence=claim.confidence,
            latest_evidence_at=graph.latest_evidence_time(claim.evidence_doc_ids),
            updated_at=claim.updated_at,
            created_by=claim.created_by or "",
            evidence_count=len(claim.evidence_doc_ids or []),
        )
        for claim in claims
    ]
    ranked = rank_conflict_candidates(candidates)
    keep_id = ranked[0].claim_id
    by_id = {claim.claim_id: claim for claim in claims}
    superseded = 0
    for candidate in ranked[1:]:
        if candidate.object_text == ranked[0].object_text:
            continue
        loser = by_id[candidate.claim_id]
        graph.supersede_claim(
            loser,
            keep_id,
            "automatic:eager_exclusive",
            valid_to=by_id[keep_id].valid_from,
        )
        superseded += 1
    if superseded:
        logger.info(
            "temporality.eager_claim_close",
            subject=f"{winner.subject_type}:{winner.subject_id}",
            predicate=winner.predicate,
            winner=ranked[0].object_text,
            superseded=superseded,
        )
    return superseded


def eager_close_relationship_slot(graph: GraphRepository, winner: Relationship) -> int:
    """Supersede other active targets for a registry-exclusive relationship type."""
    if winner.status != FactStatus.active.value:
        return 0
    if (
        get_taxonomy_registry().relationship_mutually_exclusive(winner.relationship_type)
        is not True
    ):
        return 0

    rows = (
        graph._session.query(Relationship)
        .filter(
            Relationship.workspace_id == graph._ws,
            Relationship.from_type == winner.from_type,
            Relationship.from_id == winner.from_id,
            Relationship.relationship_type == winner.relationship_type,
            Relationship.status == FactStatus.active.value,
        )
        .order_by(Relationship.relationship_id)
        .with_for_update()
        .all()
    )
    if len(rows) < 2:
        return 0

    candidates = [
        ConflictCandidate(
            claim_id=rel.relationship_id,
            object_text=f"{rel.to_type}:{rel.to_id}",
            confidence=rel.confidence,
            latest_evidence_at=graph.latest_evidence_time(rel.evidence_doc_ids),
            updated_at=rel.updated_at,
            created_by=rel.created_by or "",
            evidence_count=len(rel.evidence_doc_ids or []),
        )
        for rel in rows
    ]
    ranked = rank_conflict_candidates(candidates)
    keep_id = ranked[0].claim_id
    by_id = {rel.relationship_id: rel for rel in rows}
    superseded = 0
    for candidate in ranked[1:]:
        if candidate.object_text == ranked[0].object_text:
            continue
        graph.supersede_relationship(
            by_id[candidate.claim_id],
            keep_id,
            "automatic:eager_exclusive",
            valid_to=by_id[keep_id].valid_from,
        )
        superseded += 1
    if superseded:
        logger.info(
            "temporality.eager_relationship_close",
            from_node=f"{winner.from_type}:{winner.from_id}",
            relationship_type=winner.relationship_type,
            winner=ranked[0].object_text,
            superseded=superseded,
        )
    return superseded
