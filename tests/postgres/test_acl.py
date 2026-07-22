"""Hermetic Postgres ACL tests. DATABASE_URL only. No vendor calls."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.postgres

USER_ALICE = "user:11111111-1111-1111-1111-111111111111"
USER_BOB = "user:99999999-9999-9999-9999-999999999999"


@pytest.fixture()
def hermetic_workspace(monkeypatch):
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL absent")
    ws = f"hermetic-{uuid.uuid4().hex[:12]}"
    monkeypatch.setenv("WORKSPACE_ID", ws)
    monkeypatch.setenv("EMBEDDING_API_KEY", os.environ.get("EMBEDDING_API_KEY", "hermetic-unused"))
    monkeypatch.setenv("RERANK_API_KEY", os.environ.get("RERANK_API_KEY", "hermetic-unused"))
    monkeypatch.setenv("SERVICE_API_KEY", os.environ.get("SERVICE_API_KEY", "hermetic-unused"))
    monkeypatch.setenv("OBJECT_STORE_BACKEND", "supabase")
    monkeypatch.setenv(
        "SUPABASE_PROJECT_URL",
        os.environ.get("SUPABASE_PROJECT_URL", "https://hermetic.invalid"),
    )
    monkeypatch.setenv(
        "SUPABASE_SERVICE_ROLE_KEY",
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "hermetic-unused"),
    )
    monkeypatch.setenv("RETENTION_DAYS", os.environ.get("RETENTION_DAYS", "30"))
    monkeypatch.setenv(
        "SPEND_ALERT_TOKENS_MONTHLY",
        os.environ.get("SPEND_ALERT_TOKENS_MONTHLY", "1000000"),
    )
    monkeypatch.setenv(
        "SPEND_HARD_LIMIT_TOKENS_MONTHLY",
        os.environ.get("SPEND_HARD_LIMIT_TOKENS_MONTHLY", "2000000"),
    )
    from org_memory.core.settings import get_settings
    from org_memory.db import engine as engine_mod

    get_settings.cache_clear()
    engine_mod._engine = None
    engine_mod._session_factory = None
    yield ws
    get_settings.cache_clear()
    engine_mod._engine = None
    engine_mod._session_factory = None


def _doc(
    *,
    doc_id: str,
    workspace_id: str,
    org_visible: bool,
    allowed_principals: list[str],
    event_time: datetime,
):
    from org_memory.db.orm import Document

    return Document(
        doc_id=doc_id,
        workspace_id=workspace_id,
        source_system="test",
        external_id=doc_id.split(":", 1)[-1],
        source_type="test_doc",
        title=doc_id,
        rendered_text=f"body of {doc_id}",
        event_time=event_time,
        org_visible=org_visible,
        allowed_principals=allowed_principals,
        acl_event_time=event_time,
    )


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
            _doc(
                doc_id=public_id,
                workspace_id=hermetic_workspace,
                org_visible=True,
                allowed_principals=[],
                event_time=t0,
            )
        )
        session.add(
            _doc(
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
            _doc(
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
