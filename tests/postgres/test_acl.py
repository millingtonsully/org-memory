"""Hermetic Postgres ACL tests. DATABASE_URL only. No vendor calls."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from tests.postgres.helpers import make_doc

pytestmark = pytest.mark.postgres

USER_ALICE = "user:11111111-1111-1111-1111-111111111111"
USER_BOB = "user:99999999-9999-9999-9999-999999999999"


def test_entities_require_all_visible_evidence(hermetic_workspace):
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Entity
    from org_memory.db.repositories import GraphRepository
    from org_memory.domain.models import Principal

    t0 = datetime(2026, 7, 1, tzinfo=UTC)
    public_id = f"test:ent-public-{hermetic_workspace}"
    private_id = f"test:ent-private-{hermetic_workspace}"
    mixed_id = f"entity:{uuid.uuid4()}"
    public_only_id = f"entity:{uuid.uuid4()}"

    with session_scope() as session:
        session.add(
            make_doc(
                doc_id=public_id,
                workspace_id=hermetic_workspace,
                org_visible=True,
                allowed_principals=[],
                event_time=t0,
            )
        )
        session.add(
            make_doc(
                doc_id=private_id,
                workspace_id=hermetic_workspace,
                org_visible=False,
                allowed_principals=[USER_ALICE],
                event_time=t0,
            )
        )
        session.add(
            Entity(
                entity_id=mixed_id,
                workspace_id=hermetic_workspace,
                entity_type="product",
                name="Secret Product",
                normalized_name="secret product",
                description="leaks if any-visible",
                attributes={},
                evidence_doc_ids=[public_id, private_id],
            )
        )
        session.add(
            Entity(
                entity_id=public_only_id,
                workspace_id=hermetic_workspace,
                entity_type="product",
                name="Public Product",
                normalized_name="public product",
                description="ok",
                attributes={},
                evidence_doc_ids=[public_id],
            )
        )

    alice = Principal(principal_id=USER_ALICE)
    bob = Principal(principal_id=USER_BOB)
    with session_scope() as session:
        graph = GraphRepository(session)
        assert graph.get_entity_for_viewer(mixed_id, alice) is not None
        assert graph.get_entity_for_viewer(mixed_id, bob) is None
        assert graph.get_entity_for_viewer(public_only_id, bob) is not None


def test_delete_retracts_structured_claim_and_entity_evidence(hermetic_workspace):
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Claim, Entity
    from org_memory.db.repositories import GraphRepository
    from org_memory.domain.fact_lifecycle import FactStatus

    t0 = datetime(2026, 7, 1, tzinfo=UTC)
    doc_id = f"test:structured-{hermetic_workspace}"
    subject = f"person:{uuid.uuid4()}"
    entity_id = f"entity:{uuid.uuid4()}"

    with session_scope() as session:
        session.add(
            make_doc(
                doc_id=doc_id,
                workspace_id=hermetic_workspace,
                org_visible=True,
                allowed_principals=[],
                event_time=t0,
            )
        )
        session.add(
            Claim(
                workspace_id=hermetic_workspace,
                subject_type="person",
                subject_id=subject,
                predicate="department",
                object_text="Engineering",
                confidence=1.0,
                status="active",
                evidence_doc_ids=[doc_id],
                created_by="structured_field",
            )
        )
        session.add(
            Entity(
                entity_id=entity_id,
                workspace_id=hermetic_workspace,
                entity_type="team",
                name="Eng",
                normalized_name="eng",
                evidence_doc_ids=[doc_id],
            )
        )

    with session_scope() as session:
        GraphRepository(session).remove_document_evidence(doc_id)

    with session_scope() as session:
        claim = session.query(Claim).filter(Claim.workspace_id == hermetic_workspace).one()
        assert claim.status == FactStatus.retracted.value
        assert claim.evidence_doc_ids == []
        entity = session.get(Entity, entity_id)
        assert entity is not None
        assert entity.evidence_doc_ids == []
