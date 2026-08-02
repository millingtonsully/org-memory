"""Graph card temporal parity with query_facts / paths."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from tests.postgres.helpers import make_doc

pytestmark = pytest.mark.postgres

USER_ALICE = "user:11111111-1111-1111-1111-111111111111"

_JAN = datetime(2026, 1, 1, tzinfo=UTC)
_MAR = datetime(2026, 3, 15, tzinfo=UTC)
_JUL = datetime(2026, 7, 1, tzinfo=UTC)


def _headers(principal_id: str = USER_ALICE) -> dict[str, str]:
    from org_memory.core.settings import get_settings

    return {
        "X-Api-Key": get_settings().service_api_key,
        "X-Principal-Id": principal_id,
    }


def _seed_person_with_title_history(workspace_id: str) -> tuple[str, str, str]:
    """Person + org-visible evidence + superseded then active title claims."""
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Claim, DocumentParticipant, Person

    person_id = str(uuid.uuid4())
    doc_id = f"test:graph-card-{workspace_id}"
    old_id = f"claim-card-old-{uuid.uuid4().hex[:8]}"
    new_id = f"claim-card-new-{uuid.uuid4().hex[:8]}"

    with session_scope() as session:
        session.add(
            make_doc(
                doc_id=doc_id,
                workspace_id=workspace_id,
                org_visible=True,
                allowed_principals=[],
                event_time=_JAN,
            )
        )
        session.add(
            Person(
                canonical_id=person_id,
                workspace_id=workspace_id,
                display_name="Card Subject",
                resolution_status="resolved",
            )
        )
        session.flush()
        session.add(
            DocumentParticipant(
                doc_id=doc_id,
                workspace_id=workspace_id,
                role="author",
                identity_kind="person",
                source_system="test",
                external_id="card-subject",
                display_name="Card Subject",
                person_id=person_id,
                observed_person_id=person_id,
            )
        )
        session.add(
            Claim(
                claim_id=old_id,
                workspace_id=workspace_id,
                subject_type="person",
                subject_id=person_id,
                predicate="title",
                object_text="Engineer",
                valid_from=_JAN,
                valid_to=_JUL,
                recorded_at=_JAN,
                invalidated_at=_JUL,
                time_grain="day",
                confidence=0.9,
                status="superseded",
                evidence_doc_ids=[doc_id],
                created_by="test",
            )
        )
        session.add(
            Claim(
                claim_id=new_id,
                workspace_id=workspace_id,
                subject_type="person",
                subject_id=person_id,
                predicate="title",
                object_text="Staff Engineer",
                valid_from=_JUL,
                recorded_at=_JUL,
                time_grain="day",
                confidence=0.95,
                status="active",
                evidence_doc_ids=[doc_id],
                created_by="test",
            )
        )
    return person_id, old_id, new_id


def test_person_card_as_of_returns_superseded_title(hermetic_workspace) -> None:
    """Card as_of must include superseded claims (parity with query_facts)."""
    from fastapi.testclient import TestClient

    from org_memory.main import app

    person_id, old_id, _new_id = _seed_person_with_title_history(hermetic_workspace)
    client = TestClient(app)

    current = client.get(f"/v1/graph/persons/{person_id}", headers=_headers())
    assert current.status_code == 200
    current_titles = [c for c in current.json()["claims"] if c["predicate"] == "title"]
    assert len(current_titles) == 1
    assert current_titles[0]["object"] == "Staff Engineer"
    assert current_titles[0]["status"] == "active"
    assert current_titles[0]["time_grain"] == "day"

    historical = client.get(
        f"/v1/graph/persons/{person_id}",
        headers=_headers(),
        params={"as_of": _MAR.isoformat()},
    )
    assert historical.status_code == 200
    body = historical.json()
    assert body["as_of"] is not None
    titles = [c for c in body["claims"] if c["predicate"] == "title"]
    assert len(titles) == 1
    assert titles[0]["object"] == "Engineer"
    assert titles[0]["status"] == "superseded"
    assert titles[0]["time_grain"] == "day"


def test_person_card_believed_as_of_matches_query_facts(hermetic_workspace) -> None:
    from fastapi.testclient import TestClient

    from org_memory.main import app

    person_id, old_id, _new_id = _seed_person_with_title_history(hermetic_workspace)
    client = TestClient(app)

    card = client.get(
        f"/v1/graph/persons/{person_id}",
        headers=_headers(),
        params={"believed_as_of": _MAR.isoformat()},
    )
    assert card.status_code == 200
    card_titles = {
        c["object"] for c in card.json()["claims"] if c["predicate"] == "title"
    }

    facts = client.post(
        "/tools/query_facts",
        headers=_headers(),
        json={
            "subject_type": "person",
            "subject_id": person_id,
            "predicate": "title",
            "believed_as_of": _MAR.isoformat(),
        },
    )
    assert facts.status_code == 200
    fact_titles = {f["object"] for f in facts.json()["facts"]}
    assert card_titles == fact_titles
    assert "Engineer" in card_titles


def test_person_card_as_of_grain_month_matches_query_facts(hermetic_workspace) -> None:
    from fastapi.testclient import TestClient

    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Claim, DocumentParticipant, Person
    from org_memory.main import app

    person_id = str(uuid.uuid4())
    doc_id = f"test:graph-card-grain-{hermetic_workspace}"
    claim_id = f"claim-grain-{uuid.uuid4().hex[:8]}"
    # valid_from mid-March with month grain; query as_of early March + month grain.
    mid_march = datetime(2026, 3, 20, tzinfo=UTC)
    early_march = datetime(2026, 3, 5, tzinfo=UTC)

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
            Person(
                canonical_id=person_id,
                workspace_id=hermetic_workspace,
                display_name="Grain Subject",
                resolution_status="resolved",
            )
        )
        session.flush()
        session.add(
            DocumentParticipant(
                doc_id=doc_id,
                workspace_id=hermetic_workspace,
                role="author",
                identity_kind="person",
                source_system="test",
                external_id="grain-subject",
                display_name="Grain Subject",
                person_id=person_id,
                observed_person_id=person_id,
            )
        )
        session.add(
            Claim(
                claim_id=claim_id,
                workspace_id=hermetic_workspace,
                subject_type="person",
                subject_id=person_id,
                predicate="title",
                object_text="Engineer",
                valid_from=mid_march,
                time_grain="month",
                recorded_at=mid_march,
                confidence=0.9,
                status="active",
                evidence_doc_ids=[doc_id],
                created_by="test",
            )
        )

    client = TestClient(app)
    params = {
        "as_of": early_march.isoformat(),
        "as_of_grain": "month",
    }
    card = client.get(
        f"/v1/graph/persons/{person_id}",
        headers=_headers(),
        params=params,
    )
    assert card.status_code == 200
    assert card.json()["as_of_grain"] == "month"
    card_objects = [c["object"] for c in card.json()["claims"] if c["predicate"] == "title"]
    assert card_objects == ["Engineer"]

    facts = client.post(
        "/tools/query_facts",
        headers=_headers(),
        json={
            "subject_type": "person",
            "subject_id": person_id,
            "predicate": "title",
            "as_of": early_march.isoformat(),
            "as_of_grain": "month",
        },
    )
    assert facts.status_code == 200
    assert [f["object"] for f in facts.json()["facts"]] == ["Engineer"]


def test_person_card_rejects_invalid_as_of_grain(hermetic_workspace) -> None:
    from fastapi.testclient import TestClient

    from org_memory.main import app

    person_id, _old, _new = _seed_person_with_title_history(hermetic_workspace)
    client = TestClient(app)
    response = client.get(
        f"/v1/graph/persons/{person_id}",
        headers=_headers(),
        params={"as_of_grain": "week"},
    )
    assert response.status_code == 422
