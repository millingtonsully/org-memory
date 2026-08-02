"""Deterministic structured-field writers (connector ground truth → graph).

Ingest always persists structured_fields on doc_metadata. This module promotes
them to active claims only when taxonomy_registry marks the key as ground truth
via structured_field_keys.
"""

from __future__ import annotations

from typing import Protocol

import structlog
from sqlalchemy.orm import Session

from org_memory.db.orm import Claim, Document, utcnow
from org_memory.db.repositories import GraphRepository, JobRepository, PersonRepository
from org_memory.domain.fact_lifecycle import FactStatus, transition_fact
from org_memory.domain.models import StructuredField
from org_memory.services.temporality.eager_close import (
    eager_close_claim_slot_and_enqueue_conflict,
)
from org_memory.services.temporality.grounding import ground_fact_times
from org_memory.taxonomy_registry import TaxonomyRegistry, get_taxonomy_registry

logger = structlog.get_logger(__name__)


class StructuredFieldWriter(Protocol):
    def apply(
        self,
        session: Session,
        *,
        doc_id: str,
        fields: list[StructuredField],
    ) -> list[str]:
        """Write ground-truth facts. Returns claim ids created/updated."""
        ...


class RegistryBackedStructuredFieldWriter:
    """Write active claims for registry-bound structured_field_keys only."""

    def __init__(self, registry: TaxonomyRegistry | None = None):
        self._registry = registry

    def apply(
        self,
        session: Session,
        *,
        doc_id: str,
        fields: list[StructuredField],
    ) -> list[str]:
        if not fields:
            return []
        registry = self._registry or get_taxonomy_registry()
        doc = session.get(Document, doc_id)
        if doc is None:
            return []
        subject = _author_person_subject(session, doc)
        if subject is None:
            logger.info(
                "structured_writer.no_person_subject",
                doc_id=doc_id,
                fields=len(fields),
            )
            return []

        graph = GraphRepository(session)
        jobs = JobRepository(session)
        written: list[str] = []
        for field in fields:
            pred = registry.ground_truth_predicate_for_structured_key(field.key)
            if pred is None:
                continue
            if "person" not in pred.subject_types:
                continue
            object_text = _stringify_value(field.value)
            if not object_text:
                if pred.mutually_exclusive:
                    rivals = graph.active_claims_for_slot_locked(
                        subject[0], subject[1], pred.key
                    )
                    for rival in rivals:
                        if (rival.created_by or "").startswith("structured_field"):
                            transition_fact(
                                rival,
                                FactStatus.retracted,
                                "automatic:structured_field_cleared",
                            )
                            close_at = doc.event_time or utcnow()
                            if rival.valid_to is None:
                                rival.valid_to = close_at
                            rival.invalidated_at = utcnow()
                continue
            if doc.event_time is None:
                logger.info(
                    "structured_writer.missing_event_time",
                    doc_id=doc_id,
                    field=field.key,
                )
                continue
            grounded = ground_fact_times({}, t_ref=doc.event_time)
            if grounded is None:
                continue
            claim = graph.add_claim(
                Claim(
                    workspace_id=doc.workspace_id,
                    subject_type=subject[0],
                    subject_id=subject[1],
                    predicate=pred.key,
                    object_text=object_text,
                    confidence=1.0,
                    status=FactStatus.active.value,
                    evidence_doc_ids=[doc_id],
                    evidence_quotes=[
                        {
                            "doc_id": doc_id,
                            "quote": f"structured_field:{field.key}={object_text}",
                        }
                    ],
                    origin_subject_id=subject[1],
                    created_by="structured_field:ground_truth",
                    decided_by="automatic:taxonomy_registry",
                    valid_from=grounded.valid_from,
                    valid_to=grounded.valid_to,
                    time_grain=grounded.time_grain,
                )
            )
            written.append(claim.claim_id)
            if pred.mutually_exclusive:
                eager_close_claim_slot_and_enqueue_conflict(graph, jobs, claim)
        return written


def _author_person_subject(session: Session, doc: Document) -> tuple[str, str] | None:
    """Resolve the document author to a canonical person when possible."""
    persons = PersonRepository(session)
    if doc.author_external_id:
        person = persons.find_by_source_id(doc.source_system, doc.author_external_id)
        if person is not None:
            return ("person", person.canonical_id)
    if doc.author_email and bool((doc.doc_metadata or {}).get("author_email_verified")):
        person = persons.find_by_verified_email(doc.author_email)
        if person is not None:
            return ("person", person.canonical_id)
    return None


def _stringify_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()
