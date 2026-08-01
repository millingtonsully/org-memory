"""Postgres smoke for retrieve_context HTTP guards (no vendor embed calls)."""

from __future__ import annotations

import pytest
from tests.postgres.helpers import make_doc

pytestmark = pytest.mark.postgres

USER_ALICE = "user:11111111-1111-1111-1111-111111111111"
USER_BOB = "user:99999999-9999-9999-9999-999999999999"


def _headers(principal_id: str = USER_ALICE) -> dict[str, str]:
    from org_memory.core.settings import get_settings

    return {
        "X-Api-Key": get_settings().service_api_key,
        "X-Principal-Id": principal_id,
    }


def test_retrieve_context_graph_first_requires_subject(hermetic_workspace) -> None:
    from fastapi.testclient import TestClient

    from org_memory.main import app

    client = TestClient(app)
    response = client.post(
        "/tools/retrieve_context",
        headers=_headers(),
        json={"query": "who reports to whom", "mode": "graph_first"},
    )
    assert response.status_code == 422
    assert "graph_first requires" in response.json()["detail"]


def test_retrieve_context_rejects_unknown_mode(hermetic_workspace) -> None:
    from fastapi.testclient import TestClient

    from org_memory.main import app

    client = TestClient(app)
    response = client.post(
        "/tools/retrieve_context",
        headers=_headers(),
        json={"query": "anything", "mode": "magic"},
    )
    assert response.status_code == 422


def test_facts_query_helper_respects_acl(hermetic_workspace) -> None:
    """Shared helper used by retrieve_context keeps all-visible claim ACL."""
    import uuid
    from datetime import UTC, datetime

    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Claim
    from org_memory.db.repositories import GraphRepository
    from org_memory.domain.models import Principal
    from org_memory.services.facts_query import query_subject_facts

    subject = f"person:{uuid.uuid4()}"
    public_id = f"test:retrieve-acl-public-{hermetic_workspace}"
    private_id = f"test:retrieve-acl-private-{hermetic_workspace}"
    claim_id = f"claim-retrieve-acl-{uuid.uuid4().hex[:8]}"
    now = datetime(2026, 1, 1, tzinfo=UTC)

    with session_scope() as session:
        session.add(
            make_doc(
                doc_id=public_id,
                workspace_id=hermetic_workspace,
                org_visible=True,
                allowed_principals=[],
                event_time=now,
            )
        )
        session.add(
            make_doc(
                doc_id=private_id,
                workspace_id=hermetic_workspace,
                org_visible=False,
                allowed_principals=[USER_ALICE],
                event_time=now,
            )
        )
        session.add(
            Claim(
                claim_id=claim_id,
                workspace_id=hermetic_workspace,
                subject_type="person",
                subject_id=subject,
                predicate="title",
                object_text="Engineer",
                confidence=1.0,
                status="active",
                evidence_doc_ids=[public_id, private_id],
                created_by="test",
            )
        )

    with session_scope() as session:
        graph = GraphRepository(session)
        alice = query_subject_facts(
            graph,
            subject_type="person",
            subject_id=subject,
            principal=Principal(principal_id=USER_ALICE),
        )
        bob = query_subject_facts(
            graph,
            subject_type="person",
            subject_id=subject,
            principal=Principal(principal_id=USER_BOB),
        )

    assert alice["returned"] == 1
    assert bob["returned"] == 0
