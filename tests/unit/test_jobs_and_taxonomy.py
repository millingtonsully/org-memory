"""Job type contract and taxonomy registry load."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from org_memory.core.errors import ConfigurationError
from org_memory.domain.jobs import JobType
from org_memory.taxonomy_registry.loader import load_taxonomy_registry


def test_job_types_cover_worker_queue() -> None:
    names = {member.value for member in JobType}
    assert "embed_chunks" in names
    assert "extract_graph" in names
    assert "refresh_identity_embedding" in names
    assert "push_taxonomy_proposal_webhook" in names


def test_knowledge_ontology_registry_loads() -> None:
    root = Path(__file__).resolve().parents[2] / "config" / "taxonomy_registry"
    registry = load_taxonomy_registry(root)
    assert "person" in registry.entity_types
    assert "team" in registry.entity_types
    assert "project" in registry.entity_types
    assert "glossary" in registry.entity_types
    assert "title" in registry.predicates
    assert "definition" in registry.predicates
    assert "uses_term" in registry.relationship_types
    assert registry.ground_truth_predicate_for_structured_key("directory.title") is not None


def test_invalid_registry_json_fails_closed(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": 1, "predicates": [{"key": "x"}]}), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="JSON Schema validation"):
        load_taxonomy_registry(tmp_path)


def test_empty_registry_dir_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="No taxonomy registry JSON"):
        load_taxonomy_registry(tmp_path)
