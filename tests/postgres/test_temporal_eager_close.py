"""Postgres: eager exclusive supersession leaves one active title."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from tests.postgres.helpers import make_doc

pytestmark = pytest.mark.postgres

_JAN = datetime(2026, 1, 1, tzinfo=UTC)
_JUN = datetime(2026, 6, 1, tzinfo=UTC)


def test_eager_close_leaves_one_active_title(hermetic_workspace) -> None:
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Claim
    from org_memory.db.repositories import GraphRepository
    from org_memory.services.temporality.eager_close import eager_close_claim_slot

    doc_old = f"test:eager-old-{hermetic_workspace}"
    doc_new = f"test:eager-new-{hermetic_workspace}"
    person_id = str(uuid.uuid4())

    with session_scope() as session:
        session.add(
            make_doc(
                doc_id=doc_old,
                workspace_id=hermetic_workspace,
                org_visible=True,
                allowed_principals=[],
                event_time=_JAN,
            )
        )
        session.add(
            make_doc(
                doc_id=doc_new,
                workspace_id=hermetic_workspace,
                org_visible=True,
                allowed_principals=[],
                event_time=_JUN,
            )
        )
        graph = GraphRepository(session)
        old = graph.add_claim(
            Claim(
                workspace_id=hermetic_workspace,
                subject_type="person",
                subject_id=person_id,
                predicate="title",
                object_text="Engineer",
                confidence=0.9,
                status="active",
                evidence_doc_ids=[doc_old],
                created_by="extraction",
                decided_by="automatic:confidence_gate",
                valid_from=_JAN,
                time_grain="day",
            )
        )
        new = graph.add_claim(
            Claim(
                workspace_id=hermetic_workspace,
                subject_type="person",
                subject_id=person_id,
                predicate="title",
                object_text="Manager",
                confidence=0.95,
                status="active",
                evidence_doc_ids=[doc_new],
                created_by="extraction",
                decided_by="automatic:confidence_gate",
                valid_from=_JUN,
                time_grain="day",
            )
        )
        superseded = eager_close_claim_slot(graph, new)
        assert superseded == 1
        session.refresh(old)
        session.refresh(new)
        assert new.status == "active"
        assert old.status == "superseded"
        assert old.valid_to == _JUN
        assert old.superseded_by_claim_id == new.claim_id
