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


def test_query_facts_http_believed_as_of_returns_superseded(hermetic_workspace) -> None:
    """HTTP believed_as_of must not drop superseded claims that were believed then."""
    from fastapi.testclient import TestClient

    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Claim
    from org_memory.main import app

    subject = _subject()
    doc_id = f"test:facts-http-belief-{hermetic_workspace}"
    claim_id = f"claim-http-belief-{uuid.uuid4().hex[:8]}"

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

    client = TestClient(app)
    response = client.post(
        "/tools/query_facts",
        headers=_headers(),
        json={
            "subject_type": "person",
            "subject_id": subject,
            "predicate": "title",
            "believed_as_of": _MAR.isoformat(),
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["returned"] == 1
    assert body["facts"][0]["fact_id"] == claim_id
    assert body["facts"][0]["status"] == "superseded"
    assert body["believed_as_of"] is not None

    after = client.post(
        "/tools/query_facts",
        headers=_headers(),
        json={
            "subject_type": "person",
            "subject_id": subject,
            "predicate": "title",
            "believed_as_of": _AUG.isoformat(),
        },
    )
    assert after.status_code == 200
    assert after.json()["returned"] == 0


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
        )["paths"]
        depth2 = graph.paths_from(
            start_type="person",
            start_id=person_a,
            principal=principal,
            relationship_types=["reports_to"],
            max_depth=2,
            limit=50,
        )["paths"]
        depth3 = graph.paths_from(
            start_type="person",
            start_id=person_a,
            principal=principal,
            relationship_types=["reports_to"],
            max_depth=3,
            limit=50,
        )["paths"]

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
        )["paths"]
        bob_paths = graph.paths_from(
            start_type="person",
            start_id=person_a,
            principal=bob,
            max_depth=1,
        )["paths"]

    assert len(alice_paths) == 1
    assert bob_paths == []


def test_paths_from_does_not_starve_on_private_siblings(hermetic_workspace) -> None:
    """Private edges must not fill the path budget ahead of a visible edge.

    Before in-SQL ACL, the walk overfetched then filtered in Python; many
    private siblings could consume the CTE limit and hide the public path.
    """
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Relationship
    from org_memory.db.repositories import GraphRepository
    from org_memory.domain.models import Principal

    public_id = f"test:paths-starve-public-{hermetic_workspace}"
    private_id = f"test:paths-starve-private-{hermetic_workspace}"
    person_a = str(uuid.uuid4())
    public_target = str(uuid.uuid4())

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
        # Lexicographically early ids so private edges sort first under
        # ORDER BY depth, relationship_id.
        for i in range(20):
            session.add(
                Relationship(
                    relationship_id=f"0000-private-{i:02d}-{hermetic_workspace}",
                    workspace_id=hermetic_workspace,
                    from_type="person",
                    from_id=person_a,
                    to_type="person",
                    to_id=str(uuid.uuid4()),
                    relationship_type="reports_to",
                    status="active",
                    evidence_doc_ids=[private_id],
                    created_by="test",
                )
            )
        session.add(
            Relationship(
                relationship_id=f"zzzz-public-{hermetic_workspace}",
                workspace_id=hermetic_workspace,
                from_type="person",
                from_id=person_a,
                to_type="person",
                to_id=public_target,
                relationship_type="reports_to",
                status="active",
                evidence_doc_ids=[public_id],
                created_by="test",
            )
        )

    bob = Principal(principal_id=USER_BOB)
    with session_scope() as session:
        bob_paths = GraphRepository(session).paths_from(
            start_type="person",
            start_id=person_a,
            principal=bob,
            max_depth=1,
            limit=1,
        )["paths"]

    assert len(bob_paths) == 1
    assert bob_paths[0]["edges"][0]["to"]["id"] == public_target


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
        )["paths"]
        in_august = graph.paths_from(
            start_type="person",
            start_id=person_a,
            principal=principal,
            max_depth=1,
            as_of=_AUG,
        )["paths"]

    assert len(in_march) == 1
    assert in_march[0]["nodes"][-1] == f"person:{person_b}"
    assert len(in_august) == 1
    assert in_august[0]["nodes"][-1] == f"person:{person_c}"


def test_paths_from_filters_by_believed_as_of(hermetic_workspace) -> None:
    """believed_as_of reconstructs edges the service held at a past moment."""
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Relationship
    from org_memory.db.repositories import GraphRepository
    from org_memory.domain.models import Principal

    doc_id = f"test:paths-belief-{hermetic_workspace}"
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
                recorded_at=_JAN,
                invalidated_at=_JUL,
                status="superseded",
                evidence_doc_ids=[doc_id],
                created_by="test",
            )
        )

    principal = Principal(principal_id=USER_ALICE)
    with session_scope() as session:
        graph = GraphRepository(session)
        while_believed = graph.paths_from(
            start_type="person",
            start_id=person_a,
            principal=principal,
            max_depth=1,
            believed_as_of=_MAR,
        )["paths"]
        after_invalidation = graph.paths_from(
            start_type="person",
            start_id=person_a,
            principal=principal,
            max_depth=1,
            believed_as_of=_AUG,
        )["paths"]
        current_active_only = graph.paths_from(
            start_type="person",
            start_id=person_a,
            principal=principal,
            max_depth=1,
        )["paths"]

    assert len(while_believed) == 1
    assert while_believed[0]["nodes"][-1] == f"person:{person_b}"
    assert after_invalidation == []
    assert current_active_only == []


def test_paths_from_empty_for_unknown_start(hermetic_workspace) -> None:
    from org_memory.db.engine import session_scope
    from org_memory.db.repositories import GraphRepository
    from org_memory.domain.models import Principal

    with session_scope() as session:
        result = GraphRepository(session).paths_from(
            start_type="person",
            start_id=f"person:{uuid.uuid4()}",
            principal=Principal(principal_id=USER_ALICE),
            max_depth=2,
        )
    assert result["paths"] == []
    assert result["returned"] == 0
    assert result["truncated"] is False


def test_paths_from_respects_limit_and_truncation_flag(hermetic_workspace) -> None:
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
        result = GraphRepository(session).paths_from(
            start_type="person",
            start_id=person_a,
            principal=Principal(principal_id=USER_ALICE),
            max_depth=1,
            limit=2,
        )
    assert result["returned"] == 2
    assert len(result["paths"]) == 2
    assert result["truncated"] is True
    assert result["capped"] is False
    assert result["limit"] == 2


def test_paths_from_sets_capped_when_depth_clamped(hermetic_workspace) -> None:
    from org_memory.db.engine import session_scope
    from org_memory.db.repositories import GraphRepository
    from org_memory.domain.models import Principal

    with session_scope() as session:
        result = GraphRepository(session).paths_from(
            start_type="person",
            start_id=str(uuid.uuid4()),
            principal=Principal(principal_id=USER_ALICE),
            max_depth=99,
            limit=50,
        )
    assert result["capped"] is True
    assert result["max_depth"] == 3


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
                recorded_at=_JAN,
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
            "believed_as_of": _MAR.isoformat(),
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["start"]["id"] == person_a
    assert body["returned"] == 1
    assert body["truncated"] is False
    assert body["capped"] is False
    assert body["limit"] == 10
    assert body["max_depth"] == 1
    assert body["as_of"] is not None
    assert body["believed_as_of"] is not None
    assert len(body["paths"][0]["edges"]) == 1
    assert body["paths"][0]["depth"] == 1


def test_fact_candidates_honor_as_of_for_superseded(hermetic_workspace) -> None:
    """Hybrid fact channel includes superseded claims when as_of is set."""
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Claim
    from org_memory.db.repositories import GraphRepository
    from org_memory.domain.models import Principal

    doc_id = f"test:fact-cand-asof-{hermetic_workspace}"
    subject = str(uuid.uuid4())
    old_id = f"claim-old-{uuid.uuid4().hex[:8]}"
    # Distinctive token so FTS is stable across other workspace noise.
    phrase = f"ZephyrEngineer{hermetic_workspace[:8]}"

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
                object_text=phrase,
                valid_from=_JAN,
                valid_to=_JUL,
                recorded_at=_JAN,
                confidence=0.9,
                status="superseded",
                evidence_doc_ids=[doc_id],
                created_by="test",
            )
        )

    principal = Principal(principal_id=USER_ALICE)
    with session_scope() as session:
        graph = GraphRepository(session)
        without = graph.fact_candidates(phrase, principal, limit=10)
        with_as_of = graph.fact_candidates(
            phrase, principal, limit=10, as_of=_MAR
        )

    assert all(hit["fact_id"] != old_id for hit in without)
    matched = [hit for hit in with_as_of if hit["fact_id"] == old_id]
    assert len(matched) == 1
    assert matched[0]["status"] == "superseded"


def test_fact_candidates_current_honors_month_grain(hermetic_workspace) -> None:
    """Hybrid current expands valid_from by time_grain like query_facts."""
    from unittest.mock import patch

    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Claim
    from org_memory.db.repositories import GraphRepository
    from org_memory.domain.models import Principal

    doc_id = f"test:fact-cand-grain-{hermetic_workspace}"
    subject = str(uuid.uuid4())
    claim_id = f"claim-grain-{uuid.uuid4().hex[:8]}"
    phrase = f"QuasarManager{hermetic_workspace[:8]}"
    # Mid-month "now" with valid_from later the same month: raw valid_from > now
    # would exclude; month grain expansion must include.
    fixed_now = datetime(2026, 8, 10, tzinfo=UTC)
    valid_from = datetime(2026, 8, 20, tzinfo=UTC)

    with session_scope() as session:
        session.add(
            make_doc(
                doc_id=doc_id,
                workspace_id=hermetic_workspace,
                org_visible=True,
                allowed_principals=[],
                event_time=valid_from,
            )
        )
        session.add(
            Claim(
                claim_id=claim_id,
                workspace_id=hermetic_workspace,
                subject_type="person",
                subject_id=subject,
                predicate="title",
                object_text=phrase,
                valid_from=valid_from,
                valid_to=None,
                recorded_at=valid_from,
                time_grain="month",
                confidence=0.95,
                status="active",
                evidence_doc_ids=[doc_id],
                created_by="test",
            )
        )

    principal = Principal(principal_id=USER_ALICE)
    with session_scope() as session:
        graph = GraphRepository(session)
        with patch(
            "org_memory.db.repositories.graph.search.utcnow",
            return_value=fixed_now,
        ):
            hybrid = graph.fact_candidates(phrase, principal, limit=10)
        structured = graph.claims_for_viewer(
            "person",
            subject,
            principal,
            statuses=["active"],
            as_of=fixed_now,
            as_of_grain="day",
        )

    assert any(hit["fact_id"] == claim_id for hit in hybrid)
    assert any(claim.claim_id == claim_id for claim, _ in structured)
