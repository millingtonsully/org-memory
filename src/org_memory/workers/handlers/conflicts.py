"""Conflict resolution jobs for claims and relationships.

A same-predicate collision is only a contradiction when the predicate is
mutually exclusive. Exclusivity comes from the taxonomy registry when the
predicate declares it; otherwise the model judges exclusivity only, never the
winner. The winning value is always chosen deterministically by precedence,
evidence recency, confidence, and stable id (see
``domain.fact_lifecycle.rank_conflict_candidates``). Losers are superseded and
retained for audit, never deleted.
"""

from __future__ import annotations

import structlog
from sqlalchemy.orm import Session

from org_memory.core.errors import VendorAPIError
from org_memory.db.engine import session_scope
from org_memory.db.orm import Relationship
from org_memory.db.repositories import GraphRepository, JobRepository, SpendRepository
from org_memory.domain.fact_lifecycle import ConflictCandidate, rank_conflict_candidates
from org_memory.domain.jobs import JobType
from org_memory.taxonomy_registry import get_taxonomy_registry
from org_memory.workers.handlers._shared import assert_spend_under_hard_limit, parse_llm_json

logger = structlog.get_logger(__name__)

_CONFLICT_SYSTEM_PROMPT = """You judge whether a predicate is MUTUALLY EXCLUSIVE
for a single subject: at one point in time, can only ONE value be true?
Exclusive examples: a person's single current job title; a project's single
current status; a document's single owner.
NOT exclusive: team memberships, skills, collaborators, tags -- a subject can
hold many of these at once.
You are given the subject, the predicate, and the competing values with their
supporting evidence. Return ONLY this JSON object:
{"mutually_exclusive": true | false, "reason": "..."}
Do not pick a winner; only judge exclusivity."""


def handle_resolve_claim_conflict(session: Session, payload: dict, synthesizer, heartbeat=None) -> None:
    """Retire stale values when several active claims fill one exclusive slot."""
    subject_type = payload["subject_type"]
    subject_id = payload["subject_id"]
    predicate = payload["predicate"]
    assert_spend_under_hard_limit()
    if heartbeat is not None:
        heartbeat()
    graph = GraphRepository(session)
    claims = graph.active_claims_for_slot_locked(subject_type, subject_id, predicate)

    by_object: dict[str, list] = {}
    for claim in claims:
        by_object.setdefault(claim.object_text, []).append(claim)
    if len(by_object) == 0:
        return
    if len(by_object) == 1 and all(len(group) == 1 for group in by_object.values()):
        return  # already resolved by an earlier run or re-extraction

    # Collapse duplicate same-object active rows into one winner first.
    for group in by_object.values():
        if len(group) >= 2:
            graph.collapse_live_claims_for_object(group)

    claims = graph.active_claims_for_slot_locked(subject_type, subject_id, predicate)
    by_object = {}
    for claim in claims:
        by_object.setdefault(claim.object_text, []).append(claim)
    if len(by_object) < 2:
        return

    registry_exclusive = get_taxonomy_registry().predicate_mutually_exclusive(predicate)
    if registry_exclusive is False:
        logger.info(
            "worker.claim_conflict_coexists",
            subject=f"{subject_type}:{subject_id}",
            predicate=predicate,
            values=len(by_object),
            source="taxonomy_registry",
        )
        return

    if registry_exclusive is True:
        exclusive = True
        decided_by = "automatic:contradiction:taxonomy_registry"
    else:
        # Registry unset for this predicate, so the model judges exclusivity only.
        exclusive, decided_by = _llm_exclusivity(
            synthesizer, subject_type, subject_id, predicate, by_object
        )

    if not exclusive:
        logger.info(
            "worker.claim_conflict_coexists",
            subject=f"{subject_type}:{subject_id}",
            predicate=predicate,
            values=len(by_object),
        )
        return

    candidates = [_candidate(graph, claim) for claim in claims]
    ranked = rank_conflict_candidates(candidates)
    winner = ranked[0]
    by_id = {claim.claim_id: claim for claim in claims}
    superseded = 0
    for candidate in ranked[1:]:
        if candidate.object_text == winner.object_text:
            continue  # same value stored on a different row; nothing to supersede
        graph.supersede_claim(by_id[candidate.claim_id], winner.claim_id, decided_by)
        superseded += 1
    logger.info(
        "worker.claim_conflict_resolved",
        subject=f"{subject_type}:{subject_id}",
        predicate=predicate,
        winner=winner.object_text,
        superseded=superseded,
        exclusivity_source=("taxonomy_registry" if registry_exclusive is True else "llm"),
    )
    JobRepository(session).enqueue(JobType.generate_taxonomy_proposals, {})


def handle_resolve_relationship_conflict(session: Session, payload: dict, heartbeat=None) -> None:
    """Close losing exclusive relationships by evidence recency and precedence."""
    from_type = payload["from_type"]
    from_id = payload["from_id"]
    relationship_type = payload["relationship_type"]
    if heartbeat is not None:
        heartbeat()

    if get_taxonomy_registry().relationship_mutually_exclusive(relationship_type) is not True:
        return
    graph = GraphRepository(session)
    rows = (
        session.query(Relationship)
        .filter(
            Relationship.workspace_id == graph._ws,
            Relationship.from_type == from_type,
            Relationship.from_id == from_id,
            Relationship.relationship_type == relationship_type,
            Relationship.status == "active",
        )
        .order_by(Relationship.relationship_id)
        .with_for_update()
        .all()
    )
    by_target: dict[str, list] = {}
    for rel in rows:
        by_target.setdefault(rel.to_id, []).append(rel)
    if len(by_target) < 2:
        return
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
    winner_id = ranked[0].claim_id
    by_id = {rel.relationship_id: rel for rel in rows}
    superseded = 0
    for candidate in ranked[1:]:
        if candidate.object_text == ranked[0].object_text:
            continue
        graph.supersede_relationship(
            by_id[candidate.claim_id],
            winner_id,
            "automatic:contradiction:taxonomy_registry",
        )
        superseded += 1
    logger.info(
        "worker.relationship_conflict_resolved",
        from_node=f"{from_type}:{from_id}",
        relationship_type=relationship_type,
        winner=ranked[0].object_text,
        superseded=superseded,
    )


def _candidate(graph: GraphRepository, claim) -> ConflictCandidate:
    return ConflictCandidate(
        claim_id=claim.claim_id,
        object_text=claim.object_text,
        confidence=claim.confidence,
        latest_evidence_at=graph.latest_evidence_time(claim.evidence_doc_ids),
        updated_at=claim.updated_at,
        created_by=claim.created_by or "",
        evidence_count=len(claim.evidence_doc_ids or []),
    )


def _llm_exclusivity(
    synthesizer,
    subject_type: str,
    subject_id: str,
    predicate: str,
    by_object: dict[str, list],
) -> tuple[bool, str]:
    """Ask the model whether the predicate is mutually exclusive for one subject."""

    def _describe(object_text: str, group: list) -> str:
        quotes = [
            str(quote.get("quote", ""))
            for claim in group
            for quote in (claim.evidence_quotes or [])[:2]
        ]
        joined = " | ".join(quote for quote in quotes if quote)
        return f"- value={object_text!r}; evidence={joined!r}"

    values_block = "\n".join(_describe(obj, group) for obj, group in by_object.items())
    raw, tokens = synthesizer.complete(
        _CONFLICT_SYSTEM_PROMPT,
        f"SUBJECT: {subject_type}:{subject_id}\nPREDICATE: {predicate!r}\n"
        f"COMPETING VALUES:\n{values_block}",
    )
    with session_scope() as spend_session:
        SpendRepository(spend_session).record(
            "adjudication", "synthesis", synthesizer.model_name, tokens
        )

    verdict = parse_llm_json("claim-conflict", raw)
    exclusive = verdict.get("mutually_exclusive")
    if not isinstance(exclusive, bool):
        raise VendorAPIError(
            "claim-conflict",
            200,
            "mutually_exclusive must be a boolean",
            raw_response=raw,
        )
    return exclusive, f"automatic:contradiction:{synthesizer.model_name}"
