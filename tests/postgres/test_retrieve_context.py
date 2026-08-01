"""Postgres coverage for retrieve_context compose paths (no vendor embed)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from tests.postgres.helpers import make_doc

pytestmark = pytest.mark.postgres

USER_ALICE = "user:11111111-1111-1111-1111-111111111111"
USER_BOB = "user:99999999-9999-9999-9999-999999999999"
_JAN = datetime(2026, 1, 1, tzinfo=UTC)


def _headers(principal_id: str = USER_ALICE) -> dict[str, str]:
    from org_memory.core.settings import get_settings

    return {
        "X-Api-Key": get_settings().service_api_key,
        "X-Principal-Id": principal_id,
    }


class _StubRetrieval:
    def __init__(self) -> None:
        from org_memory.domain.models import Passage, SearchResponse

        self.calls: list[dict] = []
        self._response = SearchResponse(
            query="q",
            passages=[
                Passage(
                    chunk_id="chunk-stub",
                    doc_id="doc-stub",
                    source_type="test",
                    title="Stub",
                    text="stub passage",
                    author_display_name="Stub",
                    event_time=_JAN,
                    deep_link="",
                    score=0.9,
                )
            ],
            facts=[],
            total_candidates=1,
            reranked=False,
            audit_id="audit-pg-stub",
        )

    def search(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return self._response


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


def test_retrieve_context_http_fail_closed_on_embed_error(hermetic_workspace) -> None:
    """Hermetic embed key is unusable; vendor failure must surface as 502, not empty hits."""
    from fastapi.testclient import TestClient

    from org_memory.main import app

    client = TestClient(app)
    response = client.post(
        "/tools/retrieve_context",
        headers=_headers(),
        json={"query": "what is the budget process", "mode": "vector_first", "limit": 5},
    )
    assert response.status_code == 502
    assert "passages" not in response.json() or response.json().get("detail")


def test_retrieve_service_acl_on_structured_facts_and_paths(hermetic_workspace) -> None:
    """Compose keeps all-visible ACL for claims and path edges."""
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Claim, Relationship
    from org_memory.db.repositories import GraphRepository
    from org_memory.domain.models import Principal
    from org_memory.services.retrieve_context import RetrieveContextService, SubjectRef

    person_a = str(uuid.uuid4())
    person_b = str(uuid.uuid4())
    public_id = f"test:ret-svc-public-{hermetic_workspace}"
    private_id = f"test:ret-svc-private-{hermetic_workspace}"
    claim_id = f"claim-ret-svc-{uuid.uuid4().hex[:8]}"

    with session_scope() as session:
        session.add(
            make_doc(
                doc_id=public_id,
                workspace_id=hermetic_workspace,
                org_visible=True,
                allowed_principals=[],
                event_time=_JAN,
            )
        )
        session.add(
            make_doc(
                doc_id=private_id,
                workspace_id=hermetic_workspace,
                org_visible=False,
                allowed_principals=[USER_ALICE],
                event_time=_JAN,
            )
        )
        session.add(
            Claim(
                claim_id=claim_id,
                workspace_id=hermetic_workspace,
                subject_type="person",
                subject_id=person_a,
                predicate="title",
                object_text="Engineer",
                confidence=1.0,
                status="active",
                evidence_doc_ids=[public_id, private_id],
                created_by="test",
            )
        )
        session.add(
            Relationship(
                workspace_id=hermetic_workspace,
                from_type="person",
                from_id=person_a,
                to_type="person",
                to_id=person_b,
                relationship_type="reports_to",
                status="active",
                evidence_doc_ids=[public_id, private_id],
                created_by="test",
            )
        )

    stub = _StubRetrieval()
    with session_scope() as session:
        service = RetrieveContextService(
            session=session,
            retrieval=stub,  # type: ignore[arg-type]
            graph=GraphRepository(session),
        )
        alice = service.retrieve(
            principal=Principal(principal_id=USER_ALICE),
            query="title",
            mode="graph_first",
            subjects=[SubjectRef(type="person", id=person_a)],
        )
        bob = service.retrieve(
            principal=Principal(principal_id=USER_BOB),
            query="title",
            mode="graph_first",
            subjects=[SubjectRef(type="person", id=person_a)],
        )

    assert alice["structured_facts"][0]["returned"] == 1
    assert alice["paths"][0]["returned"] == 1
    assert bob["structured_facts"][0]["returned"] == 0
    assert bob["paths"][0]["returned"] == 0
    assert stub.calls and stub.calls[0]["about_person_ids"] == [person_a]


def test_retrieve_service_empty_graph_subject(hermetic_workspace) -> None:
    """Unknown subject yields empty expand channels, not an error."""
    from org_memory.db.engine import session_scope
    from org_memory.db.repositories import GraphRepository
    from org_memory.domain.models import Principal
    from org_memory.services.retrieve_context import RetrieveContextService, SubjectRef

    stub = _StubRetrieval()
    missing = str(uuid.uuid4())
    with session_scope() as session:
        out = RetrieveContextService(
            session=session,
            retrieval=stub,  # type: ignore[arg-type]
            graph=GraphRepository(session),
        ).retrieve(
            principal=Principal(principal_id=USER_ALICE),
            query="anything",
            mode="vector_first",
            subjects=[SubjectRef(type="person", id=missing)],
        )

    assert out["passages"]
    assert out["structured_facts"][0]["returned"] == 0
    assert out["paths"][0]["returned"] == 0
    assert out["truncated_tokens"] is False


def test_retrieve_service_vector_first_without_subjects(hermetic_workspace) -> None:
    from org_memory.db.engine import session_scope
    from org_memory.db.repositories import GraphRepository
    from org_memory.domain.models import Principal
    from org_memory.services.retrieve_context import RetrieveContextService

    stub = _StubRetrieval()
    with session_scope() as session:
        out = RetrieveContextService(
            session=session,
            retrieval=stub,  # type: ignore[arg-type]
            graph=GraphRepository(session),
        ).retrieve(
            principal=Principal(principal_id=USER_ALICE),
            query="budget",
            mode="vector_first",
        )

    assert out["subjects"] == []
    assert out["structured_facts"] == []
    assert out["paths"] == []
    assert stub.calls[0]["about_person_ids"] is None


def test_facts_query_helper_respects_acl(hermetic_workspace) -> None:
    """Shared helper used by retrieve_context keeps all-visible claim ACL."""
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Claim
    from org_memory.db.repositories import GraphRepository
    from org_memory.domain.models import Principal
    from org_memory.services.facts_query import query_subject_facts

    subject = f"person:{uuid.uuid4()}"
    public_id = f"test:retrieve-acl-public-{hermetic_workspace}"
    private_id = f"test:retrieve-acl-private-{hermetic_workspace}"
    claim_id = f"claim-retrieve-acl-{uuid.uuid4().hex[:8]}"

    with session_scope() as session:
        session.add(
            make_doc(
                doc_id=public_id,
                workspace_id=hermetic_workspace,
                org_visible=True,
                allowed_principals=[],
                event_time=_JAN,
            )
        )
        session.add(
            make_doc(
                doc_id=private_id,
                workspace_id=hermetic_workspace,
                org_visible=False,
                allowed_principals=[USER_ALICE],
                event_time=_JAN,
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
