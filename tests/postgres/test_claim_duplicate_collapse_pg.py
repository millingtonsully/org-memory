"""Postgres: same-object claim collapse and live unique index."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text as sql
from sqlalchemy.exc import IntegrityError
from tests.postgres.helpers import make_doc

pytestmark = pytest.mark.postgres

_JAN = datetime(2026, 1, 1, tzinfo=UTC)


def test_add_claim_merges_same_object_into_one_live_row(hermetic_workspace) -> None:
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Claim
    from org_memory.db.repositories import GraphRepository

    person_id = str(uuid.uuid4())
    doc_a = f"test:dup-a-{hermetic_workspace}"
    doc_b = f"test:dup-b-{hermetic_workspace}"

    with session_scope() as session:
        session.add(
            make_doc(
                doc_id=doc_a,
                workspace_id=hermetic_workspace,
                org_visible=True,
                allowed_principals=[],
                event_time=_JAN,
            )
        )
        session.add(
            make_doc(
                doc_id=doc_b,
                workspace_id=hermetic_workspace,
                org_visible=True,
                allowed_principals=[],
                event_time=_JAN,
            )
        )
        graph = GraphRepository(session)
        first = graph.add_claim(
            Claim(
                workspace_id=hermetic_workspace,
                subject_type="person",
                subject_id=person_id,
                predicate="title",
                object_text="Engineer",
                confidence=0.8,
                status="active",
                evidence_doc_ids=[doc_a],
                created_by="extraction",
                decided_by="automatic:confidence_gate",
                valid_from=_JAN,
            )
        )
        second = graph.add_claim(
            Claim(
                workspace_id=hermetic_workspace,
                subject_type="person",
                subject_id=person_id,
                predicate="title",
                object_text="Engineer",
                confidence=0.95,
                status="active",
                evidence_doc_ids=[doc_b],
                created_by="extraction",
                decided_by="automatic:confidence_gate",
                valid_from=_JAN,
            )
        )
        assert second.claim_id == first.claim_id
        assert set(second.evidence_doc_ids) == {doc_a, doc_b}
        assert second.confidence == 0.95
        assert graph.active_claim_count("person", person_id, "title") == 1


def test_uq_claims_live_object_rejects_raw_duplicate_insert(hermetic_workspace) -> None:
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Claim

    person_id = str(uuid.uuid4())
    doc_id = f"test:dup-idx-{hermetic_workspace}"

    with session_scope() as session:
        session.add(
            make_doc(
                doc_id=doc_id,
                workspace_id=hermetic_workspace,
                org_visible=True,
                allowed_principals=[],
                event_time=_JAN,
            )
        )
        session.add(
            Claim(
                claim_id=f"claim-a-{uuid.uuid4().hex[:8]}",
                workspace_id=hermetic_workspace,
                subject_type="person",
                subject_id=person_id,
                predicate="title",
                object_text="Engineer",
                confidence=0.9,
                status="active",
                evidence_doc_ids=[doc_id],
                created_by="test",
                valid_from=_JAN,
            )
        )
        session.flush()
        session.add(
            Claim(
                claim_id=f"claim-b-{uuid.uuid4().hex[:8]}",
                workspace_id=hermetic_workspace,
                subject_type="person",
                subject_id=person_id,
                predicate="title",
                object_text="Engineer",
                confidence=0.9,
                status="active",
                evidence_doc_ids=[doc_id],
                created_by="test",
                valid_from=_JAN,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_uq_claims_live_object_index_exists(hermetic_workspace) -> None:
    from org_memory.db.engine import session_scope

    with session_scope() as session:
        name = session.execute(
            sql(
                """
                SELECT indexname FROM pg_indexes
                WHERE tablename = 'claims' AND indexname = 'uq_claims_live_object'
                """
            )
        ).scalar()
    assert name == "uq_claims_live_object"
