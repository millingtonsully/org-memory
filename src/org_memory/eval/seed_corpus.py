"""Seed a workspace with the retrieval gold corpus for live evaluation."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from org_memory.db.orm import Chunk, Claim, Document, DocumentParticipant, Entity, Person
from org_memory.eval.fixture_embedder import EvalFixtureEmbedder, unit_vector
from org_memory.eval.harness import GoldCase

_EVENT = datetime(2026, 3, 1, tzinfo=UTC)
_JAN = datetime(2026, 1, 1, tzinfo=UTC)
_MAR_START = datetime(2026, 3, 1, tzinfo=UTC)
_MAR = datetime(2026, 3, 15, tzinfo=UTC)
_JUN = datetime(2026, 6, 1, tzinfo=UTC)
_JUL = datetime(2026, 7, 1, tzinfo=UTC)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _add_doc_with_chunk(
    session: Session,
    *,
    workspace_id: str,
    doc_id: str,
    text: str,
    embedder: EvalFixtureEmbedder,
    vector: list[float],
) -> None:
    # Stamp clocks in the past so belief/world as_of filters in gold cases
    # still surface these passages (updated_at / event_time <= query point).
    session.add(
        Document(
            doc_id=doc_id,
            workspace_id=workspace_id,
            source_system=doc_id.split(":", 1)[0],
            external_id=doc_id.split(":", 1)[-1],
            source_type="eval",
            title=doc_id,
            rendered_text=text,
            event_time=_JAN,
            ingested_at=_JAN,
            updated_at=_JAN,
            org_visible=True,
            allowed_principals=[],
            acl_event_time=_JAN,
        )
    )
    session.flush()
    embedder.plant(text, vector)
    session.add(
        Chunk(
            chunk_id=f"{doc_id}#0",
            doc_id=doc_id,
            workspace_id=workspace_id,
            chunk_index=0,
            chunk_role="child",
            parent_chunk_id=None,
            text=text,
            content_hash=_hash(text),
            embedding=list(vector),
            embedding_model=embedder.model_name,
            source_type="eval",
            title=doc_id,
            author_display_name="",
            event_time=_JAN,
            updated_at=_JAN,
            deep_link="",
            org_visible=True,
            allowed_principals=[],
            deleted=False,
        )
    )


def seed_gold_corpus(
    session: Session,
    *,
    workspace_id: str,
    cases: list[GoldCase],
    embedder: EvalFixtureEmbedder,
) -> dict[str, Any]:
    """Insert documents, chunks, people/entities, and claims for the gold set.

    Returns a small manifest (subject ids, etc.) for callers.
    """
    # Stable subjects referenced by the shipped gold set.
    alice_id = "eval-person-alice"
    carol_id = "eval-person-carol"
    carepod_id = "eval-glossary-carepod"

    reserved_doc_ids = {doc_id for case in cases for doc_id in case.expected_doc_ids}
    reserved_doc_ids.add("eval:distractor-unrelated")
    reserved_claim_ids = {
        "claim:alice-title-engineer",
        "claim:alice-title-manager",
        "claim:alice-title-belief-wrong",
        "claim:alice-member-platform",
        "claim:alice-member-infra",
        "claim:carepod-definition",
        "claim:carol-reports-to-dan",
    }
    # Gold primary keys are shared across eval runs; clear leftovers first.
    if reserved_doc_ids:
        session.query(DocumentParticipant).filter(
            DocumentParticipant.doc_id.in_(reserved_doc_ids)
        ).delete(synchronize_session=False)
        session.query(Chunk).filter(Chunk.doc_id.in_(reserved_doc_ids)).delete(
            synchronize_session=False
        )
        session.query(Document).filter(Document.doc_id.in_(reserved_doc_ids)).delete(
            synchronize_session=False
        )
    session.query(Claim).filter(Claim.claim_id.in_(reserved_claim_ids)).delete(
        synchronize_session=False
    )
    session.query(Person).filter(
        Person.canonical_id.in_([alice_id, carol_id])
    ).delete(synchronize_session=False)
    session.query(Entity).filter(Entity.entity_id == carepod_id).delete(
        synchronize_session=False
    )
    session.flush()

    session.add(
        Person(
            canonical_id=alice_id,
            workspace_id=workspace_id,
            display_name="Alice Example",
            resolution_status="resolved",
        )
    )
    session.add(
        Person(
            canonical_id=carol_id,
            workspace_id=workspace_id,
            display_name="Carol Example",
            resolution_status="resolved",
        )
    )
    session.add(
        Entity(
            entity_id=carepod_id,
            workspace_id=workspace_id,
            entity_type="glossary",
            name="CarePod",
            normalized_name="carepod",
            description="Clinical care unit",
            evidence_doc_ids=["notion:glossary-carepod"],
            resolution_status="resolved",
        )
    )
    session.flush()

    seen_docs: set[str] = set()
    seen_participants: set[tuple[str, str]] = set()
    for index, case in enumerate(cases):
        vector = unit_vector(index, dimensions=embedder.dimensions)
        embedder.plant(case.query, vector)
        for doc_id in case.expected_doc_ids:
            if doc_id in seen_docs:
                continue
            seen_docs.add(doc_id)
            text = (
                f"{case.query} Supporting passage for evaluation document {doc_id}. "
                f"{case.notes}".strip()
            )
            _add_doc_with_chunk(
                session,
                workspace_id=workspace_id,
                doc_id=doc_id,
                text=text,
                embedder=embedder,
                vector=vector,
            )
        # joint/graph modes scope passage search to person participants when
        # person subjects are present — link evidence docs accordingly.
        for subject_type, subject_id in case.subjects:
            if subject_type != "person":
                continue
            display = (
                "Alice Example"
                if subject_id == alice_id
                else "Carol Example"
                if subject_id == carol_id
                else subject_id
            )
            for doc_id in case.expected_doc_ids:
                key = (doc_id, subject_id)
                if key in seen_participants:
                    continue
                seen_participants.add(key)
                session.add(
                    DocumentParticipant(
                        doc_id=doc_id,
                        workspace_id=workspace_id,
                        role="participant",
                        identity_kind="person",
                        source_system="eval",
                        external_id=subject_id,
                        display_name=display,
                        person_id=subject_id,
                        observed_person_id=subject_id,
                    )
                )
    session.flush()

    # Claims with ids required by the gold set.
    # Belief: Intern recorded Jan, invalidated Mar. World: Intern ends before
    # March so month-grain March snapshots only see Engineer (not Intern+Engineer).
    # Engineer Jan–Jul; Manager from Jul.
    session.add(
        Claim(
            claim_id="claim:alice-title-belief-wrong",
            workspace_id=workspace_id,
            subject_type="person",
            subject_id=alice_id,
            predicate="title",
            object_text="Intern",
            valid_from=_JAN,
            valid_to=_MAR_START,
            recorded_at=_JAN,
            invalidated_at=_MAR,
            confidence=0.7,
            status="superseded",
            evidence_doc_ids=["hr:alice-title-correction"],
            created_by="eval",
            time_grain="day",
        )
    )
    session.add(
        Claim(
            claim_id="claim:alice-title-engineer",
            workspace_id=workspace_id,
            subject_type="person",
            subject_id=alice_id,
            predicate="title",
            object_text="Engineer",
            valid_from=_JAN,
            valid_to=_JUL,
            recorded_at=_MAR,
            invalidated_at=_JUL,
            confidence=0.95,
            status="superseded",
            evidence_doc_ids=["slack:promo-thread-2026-06", "hr:offer-letter-alice"],
            created_by="eval",
            time_grain="month",
        )
    )
    session.add(
        Claim(
            claim_id="claim:alice-title-manager",
            workspace_id=workspace_id,
            subject_type="person",
            subject_id=alice_id,
            predicate="title",
            object_text="Manager",
            valid_from=_JUL,
            recorded_at=_JUL,
            confidence=0.95,
            status="active",
            evidence_doc_ids=["slack:promo-thread-2026-06"],
            created_by="eval",
            time_grain="day",
        )
    )
    session.add(
        Claim(
            claim_id="claim:alice-member-platform",
            workspace_id=workspace_id,
            subject_type="person",
            subject_id=alice_id,
            predicate="team",
            object_text="Platform",
            recorded_at=_EVENT,
            confidence=0.9,
            status="active",
            evidence_doc_ids=["notion:alice-teams"],
            created_by="eval",
        )
    )
    session.add(
        Claim(
            claim_id="claim:alice-member-infra",
            workspace_id=workspace_id,
            subject_type="person",
            subject_id=alice_id,
            predicate="team",
            object_text="Infra",
            recorded_at=_EVENT,
            confidence=0.9,
            status="active",
            evidence_doc_ids=["notion:alice-teams"],
            created_by="eval",
        )
    )
    session.add(
        Claim(
            claim_id="claim:carepod-definition",
            workspace_id=workspace_id,
            subject_type="glossary",
            subject_id=carepod_id,
            predicate="definition",
            object_text="A cross-functional clinical unit for a patient panel.",
            recorded_at=_EVENT,
            confidence=0.99,
            status="active",
            evidence_doc_ids=["notion:glossary-carepod"],
            created_by="eval",
        )
    )
    session.add(
        Claim(
            claim_id="claim:carol-reports-to-dan",
            workspace_id=workspace_id,
            subject_type="person",
            subject_id=carol_id,
            predicate="reports_to",
            object_text="Dan Example",
            recorded_at=_EVENT,
            confidence=0.9,
            status="active",
            evidence_doc_ids=["hr:carol-manager-note"],
            created_by="eval",
        )
    )
    session.flush()

    # Distractor doc with a different vector so ranking has something to beat.
    distractor = "eval:distractor-unrelated"
    noise = unit_vector(99, dimensions=embedder.dimensions)
    _add_doc_with_chunk(
        session,
        workspace_id=workspace_id,
        doc_id=distractor,
        text="Unrelated cafeteria menu and parking policy.",
        embedder=embedder,
        vector=noise,
    )
    embedder.plant("Unrelated cafeteria menu and parking policy.", noise)

    return {
        "alice_id": alice_id,
        "carol_id": carol_id,
        "carepod_id": carepod_id,
        "as_of_pre_promo": _JUN.isoformat(),
    }
