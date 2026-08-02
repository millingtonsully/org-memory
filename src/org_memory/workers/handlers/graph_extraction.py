"""Graph extraction job: turn one document's text into claims and relationships.

Extraction itself lives in ``services.extraction``. This handler adds the
follow-up work that only makes sense after new facts land: queueing conflict
adjudication for slots that now hold competing values, and refreshing
downstream aggregates (taxonomy proposals, collaboration edges).
"""

from __future__ import annotations

import hashlib

import structlog
from sqlalchemy.orm import Session

from org_memory.db.orm import Document, Relationship
from org_memory.db.repositories import GraphRepository, JobRepository
from org_memory.domain.jobs import JobType
from org_memory.ports.embedder import Embedder
from org_memory.services.extraction import ExtractionService
from org_memory.taxonomy_registry import get_taxonomy_registry
from org_memory.workers.handlers._shared import assert_spend_under_hard_limit

logger = structlog.get_logger(__name__)


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
    assert_spend_under_hard_limit()
    service = ExtractionService(session, synthesizer, embedder)
    service.extract_for_document(doc, heartbeat=heartbeat)

    # A same-predicate collision is only a contradiction for a mutually
    # exclusive predicate. Queue an adjudication per affected slot that now
    # holds more than one active value; the resolver decides exclusivity.
    graph = GraphRepository(session)
    jobs = JobRepository(session)
    for subject_type, subject_id, predicate in service.active_claim_slots:
        if graph.active_claim_count(subject_type, subject_id, predicate) > 1:
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

    _enqueue_exclusive_relationship_conflicts(session, doc, jobs)


def _enqueue_exclusive_relationship_conflicts(
    session: Session, doc: Document, jobs: JobRepository
) -> None:
    """Queue conflict jobs for exclusive relationship types this document touched."""
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
