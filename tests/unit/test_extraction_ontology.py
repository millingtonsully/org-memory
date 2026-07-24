"""Unit tests for extraction entity typing, glossary seeding, and ref resolution."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from org_memory.domain.fact_lifecycle import FactStatus
from org_memory.services.extraction import ExtractionService


def test_resolve_ref_typed_team(monkeypatch) -> None:
    from tests.conftest import apply_minimal_settings_env

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-extract")
    from org_memory.core.settings import get_settings
    from org_memory.taxonomy_registry import clear_taxonomy_registry_cache

    get_settings.cache_clear()
    clear_taxonomy_registry_cache()

    service = ExtractionService(MagicMock(), MagicMock(), MagicMock())
    entity = SimpleNamespace(
        entity_id="team-1",
        entity_type="team",
        normalized_name="platform",
        name="Platform",
    )
    service._graph = MagicMock()
    service._graph.normalize_name.side_effect = lambda n: n.strip().casefold()
    service._graph.search_entities.return_value = [entity]
    summary = {"skipped_mentions": 0, "dropped_untyped": 0}
    assert service._resolve_ref({"type": "team", "name": "Platform"}, summary) == (
        "team",
        "team-1",
    )
    get_settings.cache_clear()
    clear_taxonomy_registry_cache()


def test_apply_seeds_glossary_definition(monkeypatch) -> None:
    from tests.conftest import apply_minimal_settings_env

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-extract")
    from org_memory.core.settings import get_settings
    from org_memory.taxonomy_registry import clear_taxonomy_registry_cache

    get_settings.cache_clear()
    clear_taxonomy_registry_cache()

    service = ExtractionService(MagicMock(), MagicMock(), MagicMock())
    entity = SimpleNamespace(
        entity_id="gloss-1",
        entity_type="glossary",
        name="CarePod",
        normalized_name="carepod",
    )
    service._graph = MagicMock()
    service._graph.normalize_name.side_effect = lambda n: n.strip().casefold()
    service._graph.upsert_entity.return_value = entity
    service._graph.get_entity.return_value = entity
    stored = SimpleNamespace(
        status=FactStatus.active.value,
        subject_type="glossary",
        subject_id="gloss-1",
        predicate="definition",
    )
    service._graph.add_claim.return_value = stored

    doc = SimpleNamespace(
        doc_id="doc:1",
        workspace_id="ws-extract",
        event_time=None,
    )
    window = "CarePod is a cross-functional clinical unit for a patient panel."
    parsed = {
        "entities": [
            {
                "type": "glossary",
                "name": "CarePod",
                "description": "A cross-functional clinical unit for a patient panel.",
                "evidence_quote": "CarePod is a cross-functional clinical unit for a patient panel.",
            }
        ],
        "relationships": [],
        "claims": [],
    }
    summary = {
        "entities": 0,
        "relationships": 0,
        "claims": 0,
        "active_facts": 0,
        "proposed_facts": 0,
        "skipped_mentions": 0,
        "dropped_unverifiable": 0,
        "dropped_untyped": 0,
    }
    service._apply_extraction(doc, parsed, summary, window)
    assert summary["entities"] == 1
    assert summary["claims"] == 1
    assert service._graph.add_claim.called
    claim = service._graph.add_claim.call_args.args[0]
    assert claim.predicate == "definition"
    assert claim.subject_type == "glossary"
    get_settings.cache_clear()
    clear_taxonomy_registry_cache()


def test_person_entity_rows_are_not_upserted(monkeypatch) -> None:
    from tests.conftest import apply_minimal_settings_env

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-extract")
    from org_memory.core.settings import get_settings
    from org_memory.taxonomy_registry import clear_taxonomy_registry_cache

    get_settings.cache_clear()
    clear_taxonomy_registry_cache()
    service = ExtractionService(MagicMock(), MagicMock(), MagicMock())
    service._graph = MagicMock()
    doc = SimpleNamespace(doc_id="doc:1", workspace_id="ws-extract", event_time=None)
    summary = {
        "entities": 0,
        "relationships": 0,
        "claims": 0,
        "active_facts": 0,
        "proposed_facts": 0,
        "skipped_mentions": 0,
        "dropped_unverifiable": 0,
        "dropped_untyped": 0,
    }
    parsed = {
        "entities": [
            {
                "type": "person",
                "name": "Sarah",
                "description": "eng",
                "evidence_quote": "Sarah joined the platform team yesterday.",
            }
        ],
        "relationships": [],
        "claims": [],
    }
    service._apply_extraction(
        doc,
        parsed,
        summary,
        "Sarah joined the platform team yesterday.",
    )
    assert service._graph.upsert_entity.call_count == 0
    assert summary["skipped_mentions"] == 1
    get_settings.cache_clear()
    clear_taxonomy_registry_cache()
