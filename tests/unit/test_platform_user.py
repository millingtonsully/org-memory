"""platform_user identity helpers and promote auto-fill."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from org_memory.domain.identity_namespaces import (
    PLATFORM_USER_NAMESPACE,
    PLATFORM_USER_SOURCE_SYSTEM,
)
from org_memory.domain.models import Principal
from org_memory.services.promotions import PromotionService


def test_platform_user_namespace_constants() -> None:
    assert PLATFORM_USER_NAMESPACE == "platform_user"
    assert PLATFORM_USER_SOURCE_SYSTEM == "identity:platform_user"


def test_platform_user_id_for_single_alias() -> None:
    from org_memory.db.repositories.people import PersonRepository

    repo = PersonRepository.__new__(PersonRepository)
    repo.aliases_for = MagicMock(
        return_value=[
            SimpleNamespace(
                source_system=PLATFORM_USER_SOURCE_SYSTEM,
                external_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            )
        ]
    )
    assert repo.platform_user_id_for("person-1") == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def test_platform_user_id_for_missing_or_ambiguous() -> None:
    from org_memory.db.repositories.people import PersonRepository

    repo = PersonRepository.__new__(PersonRepository)
    repo.aliases_for = MagicMock(return_value=[])
    assert repo.platform_user_id_for("person-1") is None

    repo.aliases_for = MagicMock(
        return_value=[
            SimpleNamespace(source_system=PLATFORM_USER_SOURCE_SYSTEM, external_id="a"),
            SimpleNamespace(source_system=PLATFORM_USER_SOURCE_SYSTEM, external_id="b"),
        ]
    )
    assert repo.platform_user_id_for("person-1") is None


def test_promote_autofills_host_entity_id_from_platform_user(monkeypatch) -> None:
    from datetime import UTC, datetime

    from tests.conftest import apply_minimal_settings_env

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-promo")
    from org_memory.core.settings import get_settings
    from org_memory.taxonomy_registry import clear_taxonomy_registry_cache

    get_settings.cache_clear()
    clear_taxonomy_registry_cache()

    session = MagicMock()
    service = PromotionService(session)
    claim = SimpleNamespace(
        claim_id="claim-new",
        subject_type="person",
        subject_id="person-1",
        predicate="title",
        object_text="VP",
        confidence=1.0,
        status="active",
        evidence_doc_ids=["doc:1"],
        created_by="agent_promote:user:11111111-1111-1111-1111-111111111111",
        valid_from=datetime(2026, 3, 15, tzinfo=UTC),
        updated_at=datetime(2026, 3, 15, tzinfo=UTC),
    )
    proposal = SimpleNamespace(proposal_id="prop-1")
    service._graph = MagicMock()
    service._graph.visible_evidence_doc_ids.return_value = ["doc:1"]
    service._graph.latest_evidence_time.return_value = datetime(2026, 3, 15, tzinfo=UTC)
    service._graph.add_claim.return_value = claim
    service._graph.active_claims_for_slot_locked.return_value = [claim]
    service._graph.active_claim_count.return_value = 1
    service._proposals = MagicMock()
    service._proposals.upsert_pending.return_value = proposal
    service._jobs = MagicMock()
    service._subject_exists = MagicMock(return_value=True)
    service._require_content_support = MagicMock()

    monkeypatch.setattr(
        "org_memory.db.repositories.PersonRepository.platform_user_id_for",
        lambda self, person_id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    )

    service.promote(
        principal=Principal(
            principal_id="user:11111111-1111-1111-1111-111111111111", groups=[]
        ),
        om_canonical_id="person-1",
        subject_type="person",
        taxonomy_key="person",
        field_key="title",
        value="VP",
        evidence_doc_ids=["doc:1"],
        host_entity_id="",
    )
    pending = service._proposals.upsert_pending.call_args.args[0]
    assert pending.host_entity_id == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    get_settings.cache_clear()
    clear_taxonomy_registry_cache()
