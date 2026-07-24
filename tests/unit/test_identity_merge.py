"""Identity email normalize, corroboration, and multi-hop merge/split."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from org_memory.services.identity_merge import (
    corroborating_signals,
    hard_identity_conflicts,
    has_sufficient_corroboration,
    merge_people,
    normalize_email,
    reconcile_merged_identity_conflicts,
    split_person,
)


def test_normalize_email_strips_plus_and_gmail_dots() -> None:
    assert normalize_email("Alice.Smith+tag@Gmail.com") == "alicesmith@gmail.com"
    assert normalize_email("alice.smith@googlemail.com") == "alicesmith@gmail.com"
    assert normalize_email("  Bob+news@Example.COM ") == "bob@example.com"
    assert normalize_email("a.b@company.com") == "a.b@company.com"
    assert normalize_email("Ada@AVIARY.AI") == "ada@aviary.ai"


def test_corroboration_requires_name_and_email() -> None:
    assert has_sufficient_corroboration(
        ["shared_normalized_name", "shared_email_address"]
    )
    assert not has_sufficient_corroboration(
        ["shared_normalized_name", "shared_verified_email_domain"]
    )
    assert not has_sufficient_corroboration(["shared_email_address"])
    assert not has_sufficient_corroboration(
        ["shared_normalized_name", "very_high_identity_similarity"]
    )


def test_shared_email_signal_requires_verified() -> None:
    person_a = SimpleNamespace(display_name="Ada Lovelace", identity_embedding=None)
    person_b = SimpleNamespace(display_name="Ada Lovelace", identity_embedding=None)
    aliases_a = [SimpleNamespace(display_name="Ada", email="ada@x.com", email_verified=False)]
    aliases_b = [SimpleNamespace(display_name="Ada", email="ada@x.com", email_verified=True)]
    signals = corroborating_signals(aliases_a, aliases_b, person_a, person_b, similarity=0.0)
    assert "shared_normalized_name" in signals
    assert "shared_email_address" not in signals


def _person(
    canonical_id: str,
    *,
    display_name: str = "Ada",
    primary_email: str = "",
    merged_into_id: str | None = None,
    name_aliases: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        canonical_id=canonical_id,
        workspace_id="ws",
        display_name=display_name,
        primary_email=primary_email,
        name_aliases=list(name_aliases or []),
        merged_into_id=merged_into_id,
        resolution_status="canonical" if merged_into_id is None else "merged",
        identity_metadata={},
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        updated_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


def _session_tracking_updates() -> MagicMock:
    session = MagicMock()
    query = MagicMock()
    session.query.return_value = query
    query.filter.return_value = query
    query.update.return_value = 0
    query.all.return_value = []
    query.order_by.return_value = query
    query.first.return_value = None
    return session


def test_merge_rejects_non_roots() -> None:
    session = _session_tracking_updates()
    keep = _person("b")
    merge = _person("a", merged_into_id="b")
    try:
        merge_people(session, keep, merge)
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_merge_chain_a_into_b_then_b_into_c() -> None:
    session = _session_tracking_updates()
    a = _person("a", display_name="Ada A", primary_email="")
    b = _person("b", display_name="Ada B", primary_email="ada@x.com")
    c = _person("c", display_name="Ada C", primary_email="")

    merge_people(session, b, a)
    assert a.merged_into_id == "b"
    assert a.resolution_status == "merged"
    assert "Ada A" in b.name_aliases or b.display_name == "Ada B"

    merge_people(session, c, b)
    assert b.merged_into_id == "c"
    assert c.primary_email == "ada@x.com"
    assert c.resolution_status == "canonical"


def test_split_restores_child_and_marks_decision() -> None:
    session = _session_tracking_updates()
    root = _person("root", display_name="Root")
    child = _person("child", display_name="Child", merged_into_id="root")
    decision = SimpleNamespace(
        workspace_id="ws",
        status="auto_merged",
        a_id="root",
        b_id="child",
        reversed_at=None,
        reversal_reason="",
    )
    query = session.query.return_value
    query.filter.return_value = query
    query.order_by.return_value = query
    query.first.return_value = decision
    query.all.return_value = []

    split_person(session, root, child, "email_conflict")
    assert child.merged_into_id is None
    assert child.resolution_status == "provisional"
    assert decision.status == "split_conflict"
    assert decision.reversal_reason == "email_conflict"


def test_hard_conflict_on_distinct_source_ids() -> None:
    aliases_a = [
        SimpleNamespace(source_system="slack", external_id="U1", email="", email_verified=False)
    ]
    aliases_b = [
        SimpleNamespace(source_system="slack", external_id="U2", email="", email_verified=False)
    ]
    assert hard_identity_conflicts(aliases_a, aliases_b) == ["conflicting_source_id:slack"]


def test_reconcile_splits_on_hard_conflict(monkeypatch) -> None:
    session = _session_tracking_updates()
    root = _person("root")
    child = _person("child", merged_into_id="root")

    class FakePeople:
        def aliases_observed_for(self, person_id: str):
            if person_id == "root":
                return [
                    SimpleNamespace(
                        source_system="slack",
                        external_id="U1",
                        email="",
                        email_verified=False,
                        display_name="Root",
                        observed_person_id="root",
                    )
                ]
            return [
                SimpleNamespace(
                    source_system="slack",
                    external_id="U2",
                    email="",
                    email_verified=False,
                    display_name="Child",
                    observed_person_id="child",
                )
            ]

        def merged_children(self, person_id: str):
            return [child] if person_id == "root" else []

    monkeypatch.setattr(
        "org_memory.services.identity_merge.PersonRepository",
        lambda _session: FakePeople(),
    )
    decisions = MagicMock()
    monkeypatch.setattr(
        "org_memory.services.identity_merge.PersonMergeDecisionRepository",
        lambda _session: decisions,
    )
    query = session.query.return_value
    query.filter.return_value = query
    query.order_by.return_value = query
    query.first.return_value = None
    query.all.return_value = []

    reconcile_merged_identity_conflicts(session, root)
    assert child.merged_into_id is None
    decisions.add.assert_called_once()
