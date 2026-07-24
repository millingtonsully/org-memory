"""PromotionService validation and exclusive-slot supersede."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from org_memory.domain.models import Principal
from org_memory.services.promotions import PromotionService


def _principal() -> Principal:
    return Principal(principal_id="user:11111111-1111-1111-1111-111111111111", groups=[])


def test_promote_rejects_empty_value(monkeypatch) -> None:
    from tests.conftest import apply_minimal_settings_env

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-promo")
    from org_memory.core.settings import get_settings

    get_settings.cache_clear()
    service = PromotionService(MagicMock())
    with pytest.raises(ValueError, match="value must be nonempty"):
        service.promote(
            principal=_principal(),
            om_canonical_id="person-1",
            subject_type="person",
            taxonomy_key="person",
            field_key="title",
            value="   ",
            evidence_doc_ids=["doc:1"],
        )
    get_settings.cache_clear()


def test_promote_rejects_empty_evidence(monkeypatch) -> None:
    from tests.conftest import apply_minimal_settings_env

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-promo")
    from org_memory.core.settings import get_settings

    get_settings.cache_clear()
    service = PromotionService(MagicMock())
    with pytest.raises(ValueError, match="evidence_doc_ids must be nonempty"):
        service.promote(
            principal=_principal(),
            om_canonical_id="person-1",
            subject_type="person",
            taxonomy_key="person",
            field_key="title",
            value="VP",
            evidence_doc_ids=[],
        )
    get_settings.cache_clear()


def test_promote_rejects_unknown_binding(monkeypatch) -> None:
    from tests.conftest import apply_minimal_settings_env

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-promo")
    from org_memory.core.settings import get_settings
    from org_memory.taxonomy_registry import clear_taxonomy_registry_cache

    get_settings.cache_clear()
    clear_taxonomy_registry_cache()
    service = PromotionService(MagicMock())
    with pytest.raises(ValueError, match="No taxonomy_registry binding"):
        service.promote(
            principal=_principal(),
            om_canonical_id="person-1",
            subject_type="person",
            taxonomy_key="not-a-key",
            field_key="not-a-field",
            value="VP",
            evidence_doc_ids=["doc:1"],
        )
    get_settings.cache_clear()
    clear_taxonomy_registry_cache()


def test_promote_writes_claim_and_supersedes_rivals(monkeypatch) -> None:
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
        evidence_doc_ids=["doc:1"],
    )
    proposal = SimpleNamespace(proposal_id="prop-1")
    service._graph = MagicMock()
    service._graph.visible_evidence_doc_ids.return_value = ["doc:1"]
    service._graph.latest_evidence_time.return_value = None
    service._graph.add_claim.return_value = claim
    service._proposals = MagicMock()
    service._proposals.upsert_pending.return_value = proposal
    service._jobs = MagicMock()

    result = service.promote(
        principal=_principal(),
        om_canonical_id="person-1",
        subject_type="person",
        taxonomy_key="person",
        field_key="title",
        value="VP",
        evidence_doc_ids=["doc:1"],
        host_entity_id="host-42",
    )

    assert result == {
        "proposal_id": "prop-1",
        "claim_id": "claim-new",
        "predicate": "title",
        "status": "pending",
    }
    service._graph.supersede_slot_rivals.assert_called_once()
    pending = service._proposals.upsert_pending.call_args.args[0]
    assert pending.host_entity_id == "host-42"
    get_settings.cache_clear()
    clear_taxonomy_registry_cache()
