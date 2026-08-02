"""Structured writer enqueues conflict jobs after exclusive eager close."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from org_memory.domain.fact_lifecycle import FactStatus
from org_memory.domain.jobs import JobType
from org_memory.domain.models import StructuredField
from org_memory.services.structured_writers import RegistryBackedStructuredFieldWriter
from org_memory.services.temporality.eager_close import (
    eager_close_claim_slot_and_enqueue_conflict,
)


def test_enqueue_helper_skips_multi_valued_predicate() -> None:
    graph = MagicMock()
    jobs = MagicMock()
    claim = SimpleNamespace(
        status=FactStatus.active.value,
        predicate="team",
        subject_type="person",
        subject_id="p1",
    )
    assert eager_close_claim_slot_and_enqueue_conflict(graph, jobs, claim) == 0
    jobs.enqueue.assert_not_called()
    graph.active_object_texts.assert_not_called()


def test_enqueue_helper_enqueues_when_exclusive_rivals_remain() -> None:
    graph = MagicMock()
    jobs = MagicMock()
    claim = SimpleNamespace(
        claim_id="c-new",
        status=FactStatus.active.value,
        predicate="title",
        subject_type="person",
        subject_id="p1",
        object_text="Manager",
        confidence=0.9,
        evidence_doc_ids=["d2"],
        updated_at=datetime(2026, 6, 1, tzinfo=UTC),
        created_by="structured_field:ground_truth",
        valid_from=datetime(2026, 6, 1, tzinfo=UTC),
    )
    # Eager close sees only the winner (race already committed elsewhere).
    graph.active_claims_for_slot_locked.return_value = [claim]
    graph.active_object_texts.return_value = ["Manager", "Engineer"]

    assert eager_close_claim_slot_and_enqueue_conflict(graph, jobs, claim) == 0
    jobs.enqueue.assert_called_once_with(
        JobType.resolve_claim_conflict,
        {
            "subject_type": "person",
            "subject_id": "p1",
            "predicate": "title",
        },
    )


def test_enqueue_helper_skips_when_single_active_value() -> None:
    graph = MagicMock()
    jobs = MagicMock()
    claim = SimpleNamespace(
        claim_id="c-new",
        status=FactStatus.active.value,
        predicate="title",
        subject_type="person",
        subject_id="p1",
        object_text="Manager",
        confidence=0.9,
        evidence_doc_ids=["d2"],
        updated_at=datetime(2026, 6, 1, tzinfo=UTC),
        created_by="structured_field:ground_truth",
        valid_from=datetime(2026, 6, 1, tzinfo=UTC),
    )
    graph.active_claims_for_slot_locked.return_value = [claim]
    graph.active_object_texts.return_value = ["Manager"]

    assert eager_close_claim_slot_and_enqueue_conflict(graph, jobs, claim) == 0
    jobs.enqueue.assert_not_called()


def test_structured_writer_enqueues_conflict_for_exclusive_title(monkeypatch) -> None:
    from tests.conftest import apply_minimal_settings_env

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-structured")
    from org_memory.core.settings import get_settings
    from org_memory.taxonomy_registry import clear_taxonomy_registry_cache

    get_settings.cache_clear()
    clear_taxonomy_registry_cache()

    session = MagicMock()
    doc = SimpleNamespace(
        doc_id="doc:1",
        workspace_id="ws-structured",
        event_time=datetime(2026, 6, 1, tzinfo=UTC),
        source_system="hr",
        author_external_id="emp-1",
        author_email=None,
        doc_metadata={},
    )
    session.get.return_value = doc

    claim = SimpleNamespace(
        claim_id="claim-1",
        subject_type="person",
        subject_id="person-1",
        predicate="title",
        status=FactStatus.active.value,
    )
    graph = MagicMock()
    graph.add_claim.return_value = claim
    jobs = MagicMock()

    writer = RegistryBackedStructuredFieldWriter()
    with (
        patch(
            "org_memory.services.structured_writers._author_person_subject",
            return_value=("person", "person-1"),
        ),
        patch(
            "org_memory.services.structured_writers.GraphRepository",
            return_value=graph,
        ),
        patch(
            "org_memory.services.structured_writers.JobRepository",
            return_value=jobs,
        ),
        patch(
            "org_memory.services.structured_writers.eager_close_claim_slot_and_enqueue_conflict",
        ) as close_and_enqueue,
    ):
        written = writer.apply(
            session,
            doc_id="doc:1",
            fields=[StructuredField(key="hr.title", value="Manager")],
        )

    assert written == ["claim-1"]
    close_and_enqueue.assert_called_once_with(graph, jobs, claim)

    get_settings.cache_clear()
    clear_taxonomy_registry_cache()
