"""Contract tests for query_facts and query_paths.

Hermetic Postgres only (DATABASE_URL). Covers world-time and system-time
filters, all-visible evidence ACL, registry predicate rejection, result
limits, and bounded path traversal.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from tests.postgres.helpers import make_doc

pytestmark = pytest.mark.postgres

USER_ALICE = "user:11111111-1111-1111-1111-111111111111"
USER_BOB = "user:99999999-9999-9999-9999-999999999999"

_JAN = datetime(2026, 1, 1, tzinfo=UTC)
_MAR = datetime(2026, 3, 15, tzinfo=UTC)
_JUL = datetime(2026, 7, 1, tzinfo=UTC)
_AUG = datetime(2026, 8, 1, tzinfo=UTC)
_OCT = datetime(2026, 10, 1, tzinfo=UTC)


def _headers(principal_id: str = USER_ALICE) -> dict[str, str]:
    from org_memory.core.settings import get_settings

    return {
        "X-Api-Key": get_settings().service_api_key,
        "X-Principal-Id": principal_id,
    }


def _subject() -> str:
    return f"person:{uuid.uuid4()}"


def test_claims_for_viewer_filters_by_world_time(hermetic_workspace) -> None:
    """as_of uses the half-open validity window on claims."""
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Claim
    from org_memory.db.repositories import GraphRepository
    from org_memory.domain.models import Principal

    subject = _subject()
    doc_id = f"test:facts-world-{hermetic_workspace}"
    old_id = f"claim-old-{uuid.uuid4().hex[:8]}"
    new_id = f"claim-new-{uuid.uuid4().hex[:8]}"

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
                claim_id=old_id,
                workspace_id=hermetic_workspace,
                subject_type="person",
                subject_id=subject,
                predicate="title",
                object_text="Engineer",
                valid_from=_JAN,
                valid_to=_JUL,
                recorded_at=_JAN,
                confidence=0.9,
                status="superseded",
                evidence_doc_ids=[doc_id],
                created_by="test",
            )
        )
        session.add(
            Claim(
                claim_id=new_id,
                workspace_id=hermetic_workspace,
                subject_type="person",
                subject_id=subject,
                predicate="title",
                object_text="Staff Engineer",
                valid_from=_JUL,
                valid_to=None,
                recorded_at=_JUL,
                confidence=0.95,
                status="active",
                evidence_doc_ids=[doc_id],
                created_by="test",
            )
        )

    principal = Principal(principal_id=USER_ALICE)
    with session_scope() as session:
        graph = GraphRepository(session)
        in_march = graph.claims_for_viewer(
            "person",
            subject,
            principal,
            statuses=["active", "superseded"],
            as_of=_MAR,
        )
        in_august = graph.claims_for_viewer(
            "person",
            subject,
            principal,
            statuses=["active", "superseded"],
            as_of=_AUG,
        )
        current = graph.claims_for_viewer(
            "person",
            subject,
            principal,
            statuses=["active"],
        )

    assert [c.claim_id for c, _ in in_march] == [old_id]
    assert [c.claim_id for c, _ in in_august] == [new_id]
    assert [c.claim_id for c, _ in current] == [new_id]


def test_claims_for_viewer_filters_by_system_time(hermetic_workspace) -> None:
    """believed_as_of reconstructs what the service held at a past moment."""
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Claim
    from org_memory.db.repositories import GraphRepository
    from org_memory.domain.models import Principal

    subject = _subject()
    doc_id = f"test:facts-belief-{hermetic_workspace}"
    claim_id = f"claim-belief-{uuid.uuid4().hex[:8]}"

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
                claim_id=claim_id,
                workspace_id=hermetic_workspace,
                subject_type="person",
                subject_id=subject,
                predicate="title",
                object_text="Engineer",
                valid_from=_JAN,
                valid_to=None,
                recorded_at=_JAN,
                invalidated_at=_JUL,
                confidence=0.9,
                status="superseded",
                evidence_doc_ids=[doc_id],
                created_by="test",
            )
        )

    principal = Principal(principal_id=USER_ALICE)
    with session_scope() as session:
        graph = GraphRepository(session)
        while_believed = graph.claims_for_viewer(
            "person",
            subject,
            principal,
            statuses=["active", "superseded"],
            believed_as_of=_MAR,
        )
        after_invalidation = graph.claims_for_viewer(
            "person",
            subject,
            principal,
            statuses=["active", "superseded"],
            believed_as_of=_AUG,
        )

    assert [c.claim_id for c, _ in while_believed] == [claim_id]
    assert after_invalidation == []


def test_claims_for_viewer_requires_all_evidence_visible(hermetic_workspace) -> None:
    """A claim backed by one private doc is hidden from viewers who lack it."""
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Claim
    from org_memory.db.repositories import GraphRepository
    from org_memory.domain.models import Principal

    subject = _subject()
    public_id = f"test:facts-acl-public-{hermetic_workspace}"
    private_id = f"test:facts-acl-private-{hermetic_workspace}"
    claim_id = f"claim-acl-{uuid.uuid4().hex[:8]}"

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
                evidence_quotes=[
                    {"doc_id": public_id, "quote": "public"},
                    {"doc_id": private_id, "quote": "private"},
                ],
                created_by="test",
            )
        )

    alice = Principal(principal_id=USER_ALICE)
    bob = Principal(principal_id=USER_BOB)
    with session_scope() as session:
        graph = GraphRepository(session)
        alice_rows = graph.claims_for_viewer("person", subject, alice, statuses=["active"])
        bob_rows = graph.claims_for_viewer("person", subject, bob, statuses=["active"])

    assert len(alice_rows) == 1
    assert alice_rows[0][0].claim_id == claim_id
    assert set(alice_rows[0][1]) == {public_id, private_id}
    assert bob_rows == []


def test_query_facts_http_rejects_unknown_predicate(hermetic_workspace) -> None:
    """Registry gate fails closed before any claim lookup."""
    from fastapi.testclient import TestClient

    from org_memory.main import app

    client = TestClient(app)
    response = client.post(
        "/tools/query_facts",
        headers=_headers(),
        json={
            "subject_type": "person",
            "subject_id": _subject(),
            "predicate": "not_a_real_predicate",
        },
    )
    assert response.status_code == 422
    assert "not in taxonomy_registry" in response.json()["detail"]


def test_query_facts_http_truncation_flag(hermetic_workspace) -> None:
    """When more matching facts exist than limit, truncated is true."""
    from fastapi.testclient import TestClient

    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Claim
    from org_memory.main import app

    subject = _subject()
    doc_id = f"test:facts-trunc-{hermetic_workspace}"
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
        # Two active non-exclusive "team" values so both survive without conflict.
        for index, label in enumerate(("Alpha", "Beta", "Gamma")):
            session.add(
                Claim(
                    claim_id=f"claim-trunc-{index}-{uuid.uuid4().hex[:8]}",
                    workspace_id=hermetic_workspace,
                    subject_type="person",
                    subject_id=subject,
                    predicate="team",
                    object_text=label,
                    confidence=0.5 + index * 0.1,
                    status="active",
                    evidence_doc_ids=[doc_id],
                    created_by="test",
                )
            )

    client = TestClient(app)
    response = client.post(
        "/tools/query_facts",
        headers=_headers(),
        json={
            "subject_type": "person",
            "subject_id": subject,
            "predicate": "team",
            "limit": 2,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["returned"] == 2
    assert body["truncated"] is True
    assert len(body["facts"]) == 2


def test_query_facts_http_as_of_returns_superseded_window(hermetic_workspace) -> None:
    """HTTP as_of includes superseded claims whose validity contains the point."""
    from fastapi.testclient import TestClient

    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Claim
    from org_memory.main import app

    subject = _subject()
    doc_id = f"test:facts-http-asof-{hermetic_workspace}"
    old_id = f"claim-http-old-{uuid.uuid4().hex[:8]}"
    new_id = f"claim-http-new-{uuid.uuid4().hex[:8]}"

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
                claim_id=old_id,
                workspace_id=hermetic_workspace,
                subject_type="person",
                subject_id=subject,
                predicate="title",
                object_text="Engineer",
                valid_from=_JAN,
                valid_to=_JUL,
                recorded_at=_JAN,
                confidence=0.9,
                status="superseded",
                evidence_doc_ids=[doc_id],
                created_by="test",
            )
        )
        session.add(
            Claim(
                claim_id=new_id,
                workspace_id=hermetic_workspace,
                subject_type="person",
                subject_id=subject,
                predicate="title",
                object_text="Staff Engineer",
                valid_from=_JUL,
                recorded_at=_JUL,
                confidence=0.95,
                status="active",
                evidence_doc_ids=[doc_id],
                created_by="test",
            )
        )

    client = TestClient(app)
    response = client.post(
        "/tools/query_facts",
        headers=_headers(),
        json={
            "subject_type": "person",
            "subject_id": subject,
            "predicate": "title",
            "as_of": _MAR.isoformat(),
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["returned"] == 1
    assert body["facts"][0]["fact_id"] == old_id
    assert body["facts"][0]["status"] == "superseded"
    assert body["as_of"] is not None


def test_paths_from_respects_depth_and_avoids_cycles(hermetic_workspace) -> None:
    """Traversal is outbound, depth-capped, and does not loop on mutual edges."""
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Relationship
    from org_memory.db.repositories import GraphRepository
    from org_memory.domain.models import Principal

    doc_id = f"test:paths-depth-{hermetic_workspace}"
    person_a = str(uuid.uuid4())
    person_b = str(uuid.uuid4())
    person_c = str(uuid.uuid4())
    team_id = str(uuid.uuid4())

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
        # A -member_of-> team (depth 1). Also A -reports_to-> B -reports_to-> C.
        session.add(
            Relationship(
                workspace_id=hermetic_workspace,
                from_type="person",
                from_id=person_a,
                to_type="team",
                to_id=team_id,
                relationship_type="member_of",
                status="active",
                evidence_doc_ids=[doc_id],
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
                evidence_doc_ids=[doc_id],
                created_by="test",
            )
        )
        session.add(
            Relationship(
                workspace_id=hermetic_workspace,
                from_type="person",
                from_id=person_b,
                to_type="person",
                to_id=person_c,
                relationship_type="reports_to",
                status="active",
                evidence_doc_ids=[doc_id],
                created_by="test",
            )
        )
        # Cycle edge C -> A would loop without the node_path guard.
        session.add(
            Relationship(
                workspace_id=hermetic_workspace,
                from_type="person",
                from_id=person_c,
                to_type="person",
                to_id=person_a,
                relationship_type="reports_to",
                status="active",
                evidence_doc_ids=[doc_id],
                created_by="test",
            )
        )

    principal = Principal(principal_id=USER_ALICE)
    with session_scope() as session:
        graph = GraphRepository(session)
        depth1 = graph.paths_from(
            start_type="person",
            start_id=person_a,
            principal=principal,
            max_depth=1,
            limit=50,
        )
        depth2 = graph.paths_from(
            start_type="person",
            start_id=person_a,
            principal=principal,
            relationship_types=["reports_to"],
            max_depth=2,
            limit=50,
        )
        depth3 = graph.paths_from(
            start_type="person",
            start_id=person_a,
            principal=principal,
            relationship_types=["reports_to"],
            max_depth=3,
            limit=50,
        )

    assert all(path["depth"] == 1 for path in depth1)
    depth1_ends = {path["nodes"][-1] for path in depth1}
    assert f"team:{team_id}" in depth1_ends
    assert f"person:{person_b}" in depth1_ends

    assert any(
        path["depth"] == 2 and path["nodes"][-1] == f"person:{person_c}"
        for path in depth2
    )
    # Cycle back to A must not appear even at depth 3.
    start_node = f"person:{person_a}"
    assert all(path["nodes"].count(start_node) == 1 for path in depth3)


def test_paths_from_hides_edges_with_private_evidence(hermetic_workspace) -> None:
    """Every edge on a returned path must be fully visible to the viewer."""
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Relationship
    from org_memory.db.repositories import GraphRepository
    from org_memory.domain.models import Principal

    public_id = f"test:paths-acl-public-{hermetic_workspace}"
    private_id = f"test:paths-acl-private-{hermetic_workspace}"
    person_a = str(uuid.uuid4())
    person_b = str(uuid.uuid4())

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

    alice = Principal(principal_id=USER_ALICE)
    bob = Principal(principal_id=USER_BOB)
    with session_scope() as session:
        graph = GraphRepository(session)
        alice_paths = graph.paths_from(
            start_type="person",
            start_id=person_a,
            principal=alice,
            max_depth=1,
        )
        bob_paths = graph.paths_from(
            start_type="person",
            start_id=person_a,
            principal=bob,
            max_depth=1,
        )

    assert len(alice_paths) == 1
    assert bob_paths == []


def test_paths_from_filters_by_as_of_validity(hermetic_workspace) -> None:
    """Path edges outside the as_of validity window are excluded."""
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Relationship
    from org_memory.db.repositories import GraphRepository
    from org_memory.domain.models import Principal

    doc_id = f"test:paths-asof-{hermetic_workspace}"
    person_a = str(uuid.uuid4())
    person_b = str(uuid.uuid4())
    person_c = str(uuid.uuid4())

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
            Relationship(
                workspace_id=hermetic_workspace,
                from_type="person",
                from_id=person_a,
                to_type="person",
                to_id=person_b,
                relationship_type="reports_to",
                valid_from=_JAN,
                valid_to=_JUL,
                status="active",
                evidence_doc_ids=[doc_id],
                created_by="test",
            )
        )
        session.add(
            Relationship(
                workspace_id=hermetic_workspace,
                from_type="person",
                from_id=person_a,
                to_type="person",
                to_id=person_c,
                relationship_type="reports_to",
                valid_from=_JUL,
                valid_to=None,
                status="active",
                evidence_doc_ids=[doc_id],
                created_by="test",
            )
        )

    principal = Principal(principal_id=USER_ALICE)
    with session_scope() as session:
        graph = GraphRepository(session)
        in_march = graph.paths_from(
            start_type="person",
            start_id=person_a,
            principal=principal,
            max_depth=1,
            as_of=_MAR,
        )
        in_august = graph.paths_from(
            start_type="person",
            start_id=person_a,
            principal=principal,
            max_depth=1,
            as_of=_AUG,
        )

    assert len(in_march) == 1
    assert in_march[0]["nodes"][-1] == f"person:{person_b}"
    assert len(in_august) == 1
    assert in_august[0]["nodes"][-1] == f"person:{person_c}"


def test_paths_from_empty_for_unknown_start(hermetic_workspace) -> None:
    from org_memory.db.engine import session_scope
    from org_memory.db.repositories import GraphRepository
    from org_memory.domain.models import Principal

    with session_scope() as session:
        paths = GraphRepository(session).paths_from(
            start_type="person",
            start_id=f"person:{uuid.uuid4()}",
            principal=Principal(principal_id=USER_ALICE),
            max_depth=2,
        )
    assert paths == []


def test_paths_from_respects_limit(hermetic_workspace) -> None:
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Relationship
    from org_memory.db.repositories import GraphRepository
    from org_memory.domain.models import Principal

    doc_id = f"test:paths-limit-{hermetic_workspace}"
    person_a = str(uuid.uuid4())

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
        for _ in range(5):
            session.add(
                Relationship(
                    workspace_id=hermetic_workspace,
                    from_type="person",
                    from_id=person_a,
                    to_type="team",
                    to_id=str(uuid.uuid4()),
                    relationship_type="member_of",
                    status="active",
                    evidence_doc_ids=[doc_id],
                    created_by="test",
                )
            )

    with session_scope() as session:
        paths = GraphRepository(session).paths_from(
            start_type="person",
            start_id=person_a,
            principal=Principal(principal_id=USER_ALICE),
            max_depth=1,
            limit=2,
        )
    assert len(paths) == 2


def test_query_paths_http_wires_as_of_and_limit(hermetic_workspace) -> None:
    """HTTP query_paths returns the bounded path payload shape."""
    from fastapi.testclient import TestClient

    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Relationship
    from org_memory.main import app

    doc_id = f"test:paths-http-{hermetic_workspace}"
    person_a = str(uuid.uuid4())
    person_b = str(uuid.uuid4())

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
            Relationship(
                workspace_id=hermetic_workspace,
                from_type="person",
                from_id=person_a,
                to_type="person",
                to_id=person_b,
                relationship_type="reports_to",
                valid_from=_JAN,
                status="active",
                evidence_doc_ids=[doc_id],
                created_by="test",
            )
        )

    client = TestClient(app)
    response = client.post(
        "/tools/query_paths",
        headers=_headers(),
        json={
            "start_type": "person",
            "start_id": person_a,
            "max_depth": 1,
            "limit": 10,
            "as_of": _MAR.isoformat(),
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["start"]["id"] == person_a
    assert body["returned"] == 1
    assert len(body["paths"][0]["edges"]) == 1
    assert body["paths"][0]["depth"] == 1
