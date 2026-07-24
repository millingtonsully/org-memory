"""Background job handlers for embed, extract, and person adjudication."""

from __future__ import annotations

import hashlib
import json

import structlog
from sqlalchemy.orm import Session

from org_memory.core.errors import VendorAPIError
from org_memory.core.settings import get_settings
from org_memory.db.engine import session_scope
from org_memory.db.orm import Chunk, Document, Person, utcnow
from org_memory.db.repositories import (
    GraphRepository,
    JobRepository,
    PersonMergeDecisionRepository,
    PersonRepository,
    SpendRepository,
    TaxonomyProposalRepository,
)
from org_memory.domain.fact_lifecycle import ConflictCandidate, rank_conflict_candidates
from org_memory.domain.jobs import JobType
from org_memory.ports.embedder import Embedder
from org_memory.services.extraction import ExtractionService
from org_memory.services.identity_merge import (
    corroborating_signals,
    hard_identity_conflicts,
    has_sufficient_corroboration,
    identity_fingerprint,
    merge_people,
)

logger = structlog.get_logger(__name__)


def handle_embed_chunks(session: Session, payload: dict, embedder: Embedder, heartbeat=None) -> None:
    doc_id = payload["doc_id"]
    doc = session.get(Document, doc_id)
    if doc is None or doc.deleted:
        return
    expected_hash = payload.get("content_hash")
    current_hash = hashlib.sha256(doc.rendered_text.encode("utf-8")).hexdigest()
    if expected_hash and expected_hash != current_hash:
        raise RuntimeError("embed_chunks stale content_hash")
    chunks = (
        session.query(Chunk)
        .filter(
            Chunk.doc_id == doc_id,
            Chunk.chunk_role == "child",
            Chunk.embedding.is_(None),
            Chunk.deleted == False,  # noqa: E712
        )
        .order_by(Chunk.chunk_index)
        .all()
    )
    if not chunks:
        return
    SpendRepository(session).assert_under_hard_limit()
    if heartbeat is not None:
        heartbeat()
    texts = [c.text for c in chunks]
    chunk_ids = [c.chunk_id for c in chunks]
    vectors, tokens = embedder.embed_texts(texts)
    if heartbeat is not None:
        heartbeat()
    doc = session.get(Document, doc_id)
    if doc is None or doc.deleted:
        return
    current_hash = hashlib.sha256(doc.rendered_text.encode("utf-8")).hexdigest()
    if expected_hash and expected_hash != current_hash:
        raise RuntimeError("embed_chunks stale content_hash")
    live_chunks = {
        c.chunk_id: c
        for c in session.query(Chunk)
        .filter(Chunk.chunk_id.in_(chunk_ids), Chunk.deleted == False)  # noqa: E712
        .all()
    }
    for chunk_id, text, vector in zip(chunk_ids, texts, vectors, strict=True):
        chunk = live_chunks.get(chunk_id)
        if chunk is None or chunk.text != text:
            raise RuntimeError("embed_chunks chunk text changed during embed")
        chunk.embedding = vector
        chunk.embedding_model = embedder.model_name
        chunk.updated_at = utcnow()
    with session_scope() as spend_session:
        SpendRepository(spend_session).record("embed", "embedding", embedder.model_name, tokens)
    logger.info("worker.embedded", doc_id=doc_id, chunks=len(chunks), tokens=tokens)


def handle_extract_graph(
    session: Session,
    payload: dict,
    synthesizer,
    embedder: Embedder,
    heartbeat=None,
) -> None:
    doc = session.get(Document, payload["doc_id"])
    if doc is None or doc.deleted:
        return
    expected_hash = payload.get("content_hash")
    current_hash = hashlib.sha256(doc.rendered_text.encode("utf-8")).hexdigest()
    if expected_hash and expected_hash != current_hash:
        logger.info(
            "worker.stale_extraction_skipped",
            doc_id=doc.doc_id,
            expected_hash=expected_hash,
            current_hash=current_hash,
        )
        return
    SpendRepository(session).assert_under_hard_limit()
    service = ExtractionService(session, synthesizer, embedder)
    service.extract_for_document(doc, heartbeat=heartbeat)

    # A same-predicate collision is only a contradiction for a mutually
    # exclusive predicate. Queue an adjudication per affected slot that now
    # holds more than one active value; the resolver decides exclusivity.
    graph = GraphRepository(session)
    jobs = JobRepository(session)
    for subject_type, subject_id, predicate in service.active_claim_slots:
        if len(graph.active_object_texts(subject_type, subject_id, predicate)) > 1:
            jobs.enqueue(
                JobType.resolve_claim_conflict,
                {
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "predicate": predicate,
                },
            )
    # Refresh taxonomy write-back candidates after new active facts land.
    jobs.enqueue(JobType.generate_taxonomy_proposals, {})
    jobs.enqueue(JobType.aggregate_collaboration_edges, {})

    from org_memory.db.orm import Relationship
    from org_memory.taxonomy_registry import get_taxonomy_registry

    registry = get_taxonomy_registry()
    for rel_type, rel_def in registry.relationship_types.items():
        if not rel_def.mutually_exclusive:
            continue
        touched = (
            session.query(
                Relationship.from_type,
                Relationship.from_id,
                Relationship.relationship_type,
            )
            .filter(
                Relationship.workspace_id == doc.workspace_id,
                Relationship.relationship_type == rel_type,
                Relationship.status == "active",
                Relationship.evidence_doc_ids.contains([doc.doc_id]),
            )
            .distinct()
            .all()
        )
        for from_type, from_id, relationship_type in touched:
            distinct_targets = (
                session.query(Relationship.to_id)
                .filter(
                    Relationship.workspace_id == doc.workspace_id,
                    Relationship.from_type == from_type,
                    Relationship.from_id == from_id,
                    Relationship.relationship_type == relationship_type,
                    Relationship.status == "active",
                )
                .distinct()
                .count()
            )
            if distinct_targets > 1:
                jobs.enqueue(
                    JobType.resolve_relationship_conflict,
                    {
                        "from_type": from_type,
                        "from_id": from_id,
                        "relationship_type": relationship_type,
                    },
                )


_ADJUDICATION_SYSTEM_PROMPT = """You are an identity-resolution adjudicator.
Given two source-derived person records, decide if they are the SAME real
person, DIFFERENT people, or if the evidence is UNSURE.
Return ONLY this JSON object:
{"verdict": "same" | "different" | "unsure", "confidence": 0.0-1.0, "reason": "..."}
Do not treat a similar name alone as identity proof. Different source systems
use unrelated id namespaces. Explain the concrete evidence behind the verdict."""


def handle_adjudicate_persons(session: Session, payload: dict, synthesizer, heartbeat=None) -> None:
    person_ids = sorted({payload["person_a"], payload["person_b"]})
    if len(person_ids) != 2:
        return
    SpendRepository(session).assert_under_hard_limit()
    if heartbeat is not None:
        heartbeat()
    locked_people = (
        session.query(Person)
        .filter(
            Person.canonical_id.in_(person_ids),
            Person.workspace_id == get_settings().workspace_id,
        )
        .order_by(Person.canonical_id)
        .with_for_update()
        .all()
    )
    if len(locked_people) != 2:
        return
    by_id = {person.canonical_id: person for person in locked_people}
    person_a = by_id[payload["person_a"]]
    person_b = by_id[payload["person_b"]]
    if person_a.merged_into_id or person_b.merged_into_id:
        return

    persons = PersonRepository(session)
    decisions = PersonMergeDecisionRepository(session)
    aliases_a = persons.aliases_for(person_a.canonical_id)
    aliases_b = persons.aliases_for(person_b.canonical_id)
    fingerprint = identity_fingerprint(person_a, aliases_a, person_b, aliases_b)
    if decisions.find_by_fingerprint(fingerprint) is not None:
        logger.info(
            "worker.person_adjudication_cached",
            person_a=person_a.canonical_id,
            person_b=person_b.canonical_id,
        )
        return

    conflicts = hard_identity_conflicts(aliases_a, aliases_b)
    if conflicts:
        decisions.add(
            "person",
            person_a.canonical_id,
            person_b.canonical_id,
            verdict="different",
            confidence=1.0,
            reason="Deterministic source identifiers conflict.",
            status="blocked_conflict",
            signals=conflicts,
            evidence_fingerprint=fingerprint,
            decided_by="automatic:conflict_detector",
        )
        return

    signals = corroborating_signals(
        aliases_a,
        aliases_b,
        person_a,
        person_b,
        float(payload.get("candidate_similarity", 0.0)),
    )
    if not has_sufficient_corroboration(signals):
        decisions.add(
            "person",
            person_a.canonical_id,
            person_b.canonical_id,
            verdict="unsure",
            confidence=0.0,
            reason="Insufficient structured corroboration to justify an LLM call.",
            status="unsure",
            signals=signals,
            evidence_fingerprint=fingerprint,
            decided_by="automatic:corroboration_gate",
        )
        return

    def _describe(person: Person, aliases) -> str:
        alias_lines = "\n".join(
            f"  - source={alias.source_system!r}; external_id={alias.external_id!r}; "
            f"name={alias.display_name!r}; email={alias.email!r}; "
            f"email_verified={alias.email_verified}"
            for alias in aliases
        )
        return f"display_name={person.display_name!r} email={person.primary_email!r}\n{alias_lines}"

    raw, tokens = synthesizer.complete(
        _ADJUDICATION_SYSTEM_PROMPT,
        f"STRUCTURED CORROBORATION: {signals}\n\n"
        f"RECORD A:\n{_describe(person_a, aliases_a)}\n\n"
        f"RECORD B:\n{_describe(person_b, aliases_b)}",
    )
    with session_scope() as spend_session:
        SpendRepository(spend_session).record(
            "adjudication",
            "synthesis",
            synthesizer.model_name,
            tokens,
        )

    try:
        verdict = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
    except json.JSONDecodeError as exc:
        raise VendorAPIError(
            "identity-adjudication",
            200,
            "adjudicator returned non-JSON output",
            raw_response=raw,
        ) from exc

    verdict_kind = verdict.get("verdict")
    if verdict_kind not in {"same", "different", "unsure"}:
        raise VendorAPIError(
            "identity-adjudication",
            200,
            f"invalid verdict: {verdict_kind!r}",
            raw_response=raw,
        )
    confidence_value = verdict.get("confidence")
    if isinstance(confidence_value, bool):
        raise VendorAPIError(
            "identity-adjudication",
            200,
            "confidence must be a number between 0 and 1",
            raw_response=raw,
        )
    try:
        confidence = float(confidence_value)
    except (TypeError, ValueError) as exc:
        raise VendorAPIError(
            "identity-adjudication",
            200,
            "confidence must be a number between 0 and 1",
            raw_response=raw,
        ) from exc
    if not 0.0 <= confidence <= 1.0:
        raise VendorAPIError(
            "identity-adjudication",
            200,
            "confidence must be a number between 0 and 1",
            raw_response=raw,
        )
    reason = str(verdict.get("reason", "")).strip()
    if not reason:
        raise VendorAPIError(
            "identity-adjudication",
            200,
            "reason must be non-empty",
            raw_response=raw,
        )

    auto_merge = (
        verdict_kind == "same"
        and confidence >= get_settings().identity_merge_confidence
        and has_sufficient_corroboration(signals)
    )
    if auto_merge:
        keep, merge = sorted(
            [person_a, person_b],
            key=lambda person: (person.created_at, person.canonical_id),
        )
        merge_people(session, keep, merge)
        status = "auto_merged"
    else:
        status = verdict_kind

    decisions.add(
        "person",
        person_a.canonical_id,
        person_b.canonical_id,
        verdict_kind,
        confidence,
        reason,
        status=status,
        signals=signals,
        evidence_fingerprint=fingerprint,
        decided_by=f"automatic:llm:{synthesizer.model_name}",
    )
    logger.info(
        "worker.person_adjudicated",
        person_a=person_a.canonical_id,
        person_b=person_b.canonical_id,
        verdict=verdict_kind,
        confidence=confidence,
        status=status,
        signals=signals,
    )
    person_a.updated_at = utcnow()
    person_b.updated_at = utcnow()


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
    """Retire stale values when several active claims fill one exclusive slot.

    A same-predicate collision is only a contradiction when the predicate is
    mutually exclusive (the model judges that). When it is, the current value is
    chosen deterministically by evidence recency and the others are superseded
    (retained for audit, not deleted). Multi-valued predicates are left intact.
    """
    subject_type = payload["subject_type"]
    subject_id = payload["subject_id"]
    predicate = payload["predicate"]
    SpendRepository(session).assert_under_hard_limit()
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
    for _object_text, group in by_object.items():
        if len(group) < 2:
            continue
        ranked_dupes = rank_conflict_candidates(
            [
                ConflictCandidate(
                    claim_id=claim.claim_id,
                    object_text=claim.object_text,
                    confidence=claim.confidence,
                    latest_evidence_at=graph.latest_evidence_time(claim.evidence_doc_ids),
                    updated_at=claim.updated_at,
                    created_by=claim.created_by or "",
                    evidence_count=len(claim.evidence_doc_ids or []),
                )
                for claim in group
            ]
        )
        keep = ranked_dupes[0]
        keep_row = next(c for c in group if c.claim_id == keep.claim_id)
        for rival in group:
            if rival.claim_id == keep.claim_id:
                continue
            # Merge evidence into keeper then supersede the duplicate row.
            merged = set(keep_row.evidence_doc_ids or []) | set(rival.evidence_doc_ids or [])
            keep_row.evidence_doc_ids = sorted(merged)
            quotes = {
                (str(item.get("doc_id", "")), str(item.get("quote", ""))): item
                for item in [*(keep_row.evidence_quotes or []), *(rival.evidence_quotes or [])]
            }
            keep_row.evidence_quotes = list(quotes.values())
            keep_row.confidence = max(keep_row.confidence, rival.confidence)
            graph.supersede_claim(rival, keep.claim_id, "automatic:duplicate_collapse")

    claims = graph.active_claims_for_slot_locked(subject_type, subject_id, predicate)
    by_object = {}
    for claim in claims:
        by_object.setdefault(claim.object_text, []).append(claim)
    if len(by_object) < 2:
        return

    def _describe(object_text: str, group: list) -> str:
        quotes = [
            str(quote.get("quote", ""))
            for claim in group
            for quote in (claim.evidence_quotes or [])[:2]
        ]
        joined = " | ".join(quote for quote in quotes if quote)
        return f"- value={object_text!r}; evidence={joined!r}"

    from org_memory.taxonomy_registry import get_taxonomy_registry

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
        # Registry unset for this predicate — LLM exclusivity judgement only.
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

        try:
            verdict = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
        except json.JSONDecodeError as exc:
            raise VendorAPIError(
                "claim-conflict",
                200,
                "conflict adjudicator returned non-JSON output",
                raw_response=raw,
            ) from exc
        exclusive = verdict.get("mutually_exclusive")
        if not isinstance(exclusive, bool):
            raise VendorAPIError(
                "claim-conflict",
                200,
                "mutually_exclusive must be a boolean",
                raw_response=raw,
            )
        decided_by = f"automatic:contradiction:{synthesizer.model_name}"

    if not exclusive:
        logger.info(
            "worker.claim_conflict_coexists",
            subject=f"{subject_type}:{subject_id}",
            predicate=predicate,
            values=len(by_object),
        )
        return

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
    winner = ranked[0]
    by_id = {claim.claim_id: claim for claim in claims}
    superseded = 0
    for candidate in ranked[1:]:
        if candidate.object_text == winner.object_text:
            continue  # same value stored on a different row, not a conflict
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
    """Close losing exclusive relationships by evidence recency / precedence."""
    from_type = payload["from_type"]
    from_id = payload["from_id"]
    relationship_type = payload["relationship_type"]
    if heartbeat is not None:
        heartbeat()
    from org_memory.db.orm import Relationship
    from org_memory.taxonomy_registry import get_taxonomy_registry

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


def handle_generate_taxonomy_proposals(session: Session, payload: dict) -> None:
    """Materialize pending taxonomy write-back rows from active bound claims."""
    from org_memory.services.taxonomy_proposals import TaxonomyProposalService

    summary = TaxonomyProposalService(session).generate_from_active_claims()
    JobRepository(session).enqueue(JobType.push_taxonomy_proposal_webhook, {})
    log_fields = {k: v for k, v in summary.items() if k != "proposal_ids"}
    logger.info("worker.taxonomy_proposals_generated", **log_fields)


def handle_push_taxonomy_proposal_webhook(session: Session, payload: dict) -> None:
    """Deliver pending proposals to TAXONOMY_PROPOSAL_WEBHOOK_URL with job retries."""
    from org_memory.core.settings import get_settings
    from org_memory.services.proposal_webhook import push_proposals_if_configured

    if not (get_settings().taxonomy_proposal_webhook_url or "").strip():
        return
    repo = TaxonomyProposalRepository(session)
    pending = repo.list_pending(limit=200)
    if not pending:
        return
    # Failures raise so the job queue retries / dead-letters.
    push_proposals_if_configured(repo, pending, raise_on_error=True)


def handle_aggregate_collaboration_edges(session: Session, payload: dict) -> None:
    from org_memory.services.collaboration import CollaborationService

    summary = CollaborationService(session).rebuild_edges()
    logger.info("worker.collaboration_aggregated", **summary)


def handle_refresh_identity_embedding(
    session: Session, payload: dict, embedder: Embedder, heartbeat=None
) -> None:
    person_id = payload.get("person_id")
    if not person_id:
        return
    SpendRepository(session).assert_under_hard_limit()
    if heartbeat is not None:
        heartbeat()
    person = session.get(Person, person_id)
    if person is None or person.merged_into_id:
        return
    if person.workspace_id != get_settings().workspace_id:
        return
    from org_memory.services.entity_resolution import EntityResolutionService

    EntityResolutionService(session, embedder).refresh_identity_embedding(person)
    if heartbeat is not None:
        heartbeat()
    person.updated_at = utcnow()
    logger.info("worker.identity_embedding_refreshed", person_id=person.canonical_id)
