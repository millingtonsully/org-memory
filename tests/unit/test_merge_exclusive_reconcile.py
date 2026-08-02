"""After person merge, exclusive slots get eager close + conflict enqueue."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from org_memory.db.orm import Claim, Relationship
from org_memory.domain.fact_lifecycle import FactStatus
from org_memory.domain.jobs import JobType
from org_memory.services.identity_merge import reconcile_exclusive_slots_after_person_merge
from org_memory.services.temporality.eager_close import (
    eager_close_relationship_slot_and_enqueue_conflict,
)


def test_reconcile_after_merge_closes_exclusive_title_skips_team(monkeypatch) -> None:
    from tests.conftest import apply_minimal_settings_env

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-merge-excl")
    from org_memory.core.settings import get_settings
    from org_memory.taxonomy_registry import clear_taxonomy_registry_cache

    get_settings.cache_clear()
    clear_taxonomy_registry_cache()

    root = SimpleNamespace(canonical_id="person-keep", workspace_id="ws-merge-excl")
    session = MagicMock()

    title_claim = SimpleNamespace(
        claim_id="c-mgr",
        status=FactStatus.active.value,
        predicate="title",
        subject_type="person",
        subject_id="person-keep",
    )

    graph = MagicMock()
    graph.active_claims_for_slot_locked.return_value = [title_claim]
    jobs = MagicMock()

    pred_chain = MagicMock()
    pred_chain.filter.return_value = pred_chain
    pred_chain.distinct.return_value = pred_chain
    pred_chain.all.return_value = [("title",), ("team",)]

    rel_chain = MagicMock()
    rel_chain.filter.return_value = rel_chain
    rel_chain.order_by.return_value = rel_chain
    rel_chain.with_for_update.return_value = rel_chain
    rel_chain.all.return_value = []

    def session_query(*args, **kwargs):
        if args and args[0] is Claim.predicate:
            return pred_chain
        return rel_chain

    session.query.side_effect = session_query

    with (
        patch(
            "org_memory.services.identity_merge.GraphRepository",
            return_value=graph,
        ),
        patch(
            "org_memory.services.identity_merge.JobRepository",
            return_value=jobs,
        ),
        patch(
            "org_memory.services.identity_merge.eager_close_claim_slot_and_enqueue_conflict",
        ) as close_claim,
        patch(
            "org_memory.services.identity_merge.eager_close_relationship_slot_and_enqueue_conflict",
        ) as close_rel,
    ):
        reconcile_exclusive_slots_after_person_merge(session, root)

    graph.active_claims_for_slot_locked.assert_called_once_with(
        "person", "person-keep", "title"
    )
    close_claim.assert_called_once_with(graph, jobs, title_claim)
    close_rel.assert_not_called()

    get_settings.cache_clear()
    clear_taxonomy_registry_cache()


def test_reconcile_after_merge_closes_exclusive_reports_to(monkeypatch) -> None:
    from tests.conftest import apply_minimal_settings_env

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-merge-rel")
    from org_memory.core.settings import get_settings
    from org_memory.taxonomy_registry import clear_taxonomy_registry_cache

    get_settings.cache_clear()
    clear_taxonomy_registry_cache()

    root = SimpleNamespace(canonical_id="person-keep", workspace_id="ws-merge-rel")
    session = MagicMock()

    edge = SimpleNamespace(
        relationship_id="r1",
        status=FactStatus.active.value,
        relationship_type="reports_to",
        from_type="person",
        from_id="person-keep",
    )

    graph = MagicMock()
    jobs = MagicMock()

    pred_chain = MagicMock()
    pred_chain.filter.return_value = pred_chain
    pred_chain.distinct.return_value = pred_chain
    pred_chain.all.return_value = []

    rel_chain = MagicMock()
    rel_chain.filter.return_value = rel_chain
    rel_chain.order_by.return_value = rel_chain
    rel_chain.with_for_update.return_value = rel_chain
    rel_chain.all.return_value = [edge]

    def session_query(*args, **kwargs):
        if args and args[0] is Claim.predicate:
            return pred_chain
        if args and args[0] is Relationship:
            return rel_chain
        return MagicMock()

    session.query.side_effect = session_query

    registry = MagicMock()
    registry.predicate_mutually_exclusive.return_value = False
    registry.relationship_types = {
        "reports_to": SimpleNamespace(mutually_exclusive=True),
        "member_of": SimpleNamespace(mutually_exclusive=False),
    }

    with (
        patch(
            "org_memory.services.identity_merge.GraphRepository",
            return_value=graph,
        ),
        patch(
            "org_memory.services.identity_merge.JobRepository",
            return_value=jobs,
        ),
        patch(
            "org_memory.services.identity_merge.get_taxonomy_registry",
            return_value=registry,
        ),
        patch(
            "org_memory.services.identity_merge.eager_close_claim_slot_and_enqueue_conflict",
        ) as close_claim,
        patch(
            "org_memory.services.identity_merge.eager_close_relationship_slot_and_enqueue_conflict",
        ) as close_rel,
    ):
        reconcile_exclusive_slots_after_person_merge(session, root)

    close_claim.assert_not_called()
    close_rel.assert_called_once_with(graph, jobs, edge)

    get_settings.cache_clear()
    clear_taxonomy_registry_cache()


def test_relationship_enqueue_helper_enqueues_when_targets_remain() -> None:
    graph = MagicMock()
    jobs = MagicMock()
    graph._ws = "ws"
    winner = SimpleNamespace(
        status=FactStatus.active.value,
        relationship_type="reports_to",
        from_type="person",
        from_id="p1",
        to_type="person",
        to_id="boss-a",
        relationship_id="r1",
        confidence=0.9,
        evidence_doc_ids=["d1"],
        updated_at=datetime(2026, 6, 1, tzinfo=UTC),
        created_by="extraction",
        valid_from=datetime(2026, 6, 1, tzinfo=UTC),
    )
    graph._session = MagicMock()
    query = MagicMock()
    graph._session.query.return_value = query
    query.filter.return_value = query
    query.order_by.return_value = query
    query.with_for_update.return_value = query
    query.all.return_value = [winner]
    query.distinct.return_value = query
    query.count.return_value = 2

    assert eager_close_relationship_slot_and_enqueue_conflict(graph, jobs, winner) == 0
    jobs.enqueue.assert_called_once_with(
        JobType.resolve_relationship_conflict,
        {
            "from_type": "person",
            "from_id": "p1",
            "relationship_type": "reports_to",
        },
    )
