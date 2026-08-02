"""Eager exclusive-slot supersession after an active fact is applied."""

from __future__ import annotations

import structlog

from org_memory.db.orm import Claim, Relationship
from org_memory.db.repositories import GraphRepository, JobRepository
from org_memory.domain.fact_lifecycle import (
    ConflictCandidate,
    FactStatus,
    rank_conflict_candidates,
)
from org_memory.domain.jobs import JobType
from org_memory.taxonomy_registry import get_taxonomy_registry

logger = structlog.get_logger(__name__)


def eager_close_claim_slot_and_enqueue_conflict(
    graph: GraphRepository,
    jobs: JobRepository,
    winner: Claim,
) -> int:
    """Eager-close a registry-exclusive claim slot; enqueue conflict if rivals remain.

    Shared by structured writers and promotions so concurrent races still get the
    async safety net after the in-transaction close.
    """
    superseded = eager_close_claim_slot(graph, winner)
    if get_taxonomy_registry().predicate_mutually_exclusive(winner.predicate) is not True:
        return superseded
    # Row count (not distinct objects) so same-object twins still enqueue.
    if graph.active_claim_count(
        winner.subject_type, winner.subject_id, winner.predicate
    ) > 1:
        jobs.enqueue(
            JobType.resolve_claim_conflict,
            {
                "subject_type": winner.subject_type,
                "subject_id": winner.subject_id,
                "predicate": winner.predicate,
            },
        )
    return superseded


def eager_close_relationship_slot_and_enqueue_conflict(
    graph: GraphRepository,
    jobs: JobRepository,
    winner: Relationship,
) -> int:
    """Eager-close a registry-exclusive relationship slot; enqueue if rivals remain."""
    superseded = eager_close_relationship_slot(graph, winner)
    if (
        get_taxonomy_registry().relationship_mutually_exclusive(winner.relationship_type)
        is not True
    ):
        return superseded
    distinct_targets = (
        graph._session.query(Relationship.to_id)
        .filter(
            Relationship.workspace_id == graph._ws,
            Relationship.from_type == winner.from_type,
            Relationship.from_id == winner.from_id,
            Relationship.relationship_type == winner.relationship_type,
            Relationship.status == FactStatus.active.value,
        )
        .distinct()
        .count()
    )
    if distinct_targets > 1:
        jobs.enqueue(
            JobType.resolve_relationship_conflict,
            {
                "from_type": winner.from_type,
                "from_id": winner.from_id,
                "relationship_type": winner.relationship_type,
            },
        )
    return superseded


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

    # Same-object active twins collapse first (merge evidence, supersede extras).
    by_object: dict[str, list[Claim]] = {}
    for claim in claims:
        by_object.setdefault(claim.object_text, []).append(claim)
    collapsed: list[Claim] = []
    duplicate_closed = 0
    for group in by_object.values():
        if len(group) == 1:
            collapsed.append(group[0])
            continue
        keeper = graph.collapse_live_claims_for_object(group)
        collapsed.append(keeper)
        duplicate_closed += len(group) - 1

    if len(collapsed) < 2:
        if duplicate_closed:
            logger.info(
                "temporality.eager_claim_duplicate_collapse",
                subject=f"{winner.subject_type}:{winner.subject_id}",
                predicate=winner.predicate,
                superseded=duplicate_closed,
            )
        return duplicate_closed

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
        for claim in collapsed
    ]
    ranked = rank_conflict_candidates(candidates)
    keep_id = ranked[0].claim_id
    by_id = {claim.claim_id: claim for claim in collapsed}
    superseded = duplicate_closed
    for candidate in ranked[1:]:
        loser = by_id[candidate.claim_id]
        graph.supersede_claim(
            loser,
            keep_id,
            "automatic:eager_exclusive",
            valid_to=by_id[keep_id].valid_from,
        )
        superseded += 1
    if superseded:
        graph._session.flush()
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
        graph._session.flush()
        logger.info(
            "temporality.eager_relationship_close",
            from_node=f"{winner.from_type}:{winner.from_id}",
            relationship_type=winner.relationship_type,
            winner=ranked[0].object_text,
            superseded=superseded,
        )
    return superseded
