"""Postgres contract for POST /tools/diff_facts snapshot pairs."""

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
_AUG = datetime(2026, 8, 1, tzinfo=UTC)


def _headers() -> dict[str, str]:
    from org_memory.core.settings import get_settings

    return {
        "X-Api-Key": get_settings().service_api_key,
        "X-Principal-Id": USER_ALICE,
    }


def test_diff_facts_world_title_change(hermetic_workspace) -> None:
    from fastapi.testclient import TestClient

    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Claim
    from org_memory.main import app

    subject = f"person:{uuid.uuid4()}"
    doc_id = f"test:diff-world-{hermetic_workspace}"
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

    client = TestClient(app)
    resp = client.post(
        "/tools/diff_facts",
        headers=_headers(),
        json={
            "subject_type": "person",
            "subject_id": subject,
            "as_of_from": _MAR.isoformat(),
            "as_of_to": _AUG.isoformat(),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["axis"] == "world"
    assert body["counts"]["changed"] == 1
    assert body["changed"][0]["from"]["fact_id"] == old_id
    assert body["changed"][0]["to"]["fact_id"] == new_id
    assert body["changed"][0]["from"]["object"] == "Engineer"
    assert body["changed"][0]["to"]["object"] == "Staff Engineer"


def test_diff_facts_rejects_incomplete_pair(hermetic_workspace) -> None:
    from fastapi.testclient import TestClient

    from org_memory.main import app

    client = TestClient(app)
    resp = client.post(
        "/tools/diff_facts",
        headers=_headers(),
        json={
            "subject_type": "person",
            "subject_id": f"person:{uuid.uuid4()}",
            "as_of_from": _MAR.isoformat(),
        },
    )
    assert resp.status_code == 422
