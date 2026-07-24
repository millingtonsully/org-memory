"""Job type contract and taxonomy registry load."""

from __future__ import annotations

from pathlib import Path

from org_memory.domain.jobs import JobType
from org_memory.taxonomy_registry.loader import load_taxonomy_registry


def test_job_types_cover_worker_queue() -> None:
    names = {member.value for member in JobType}
    assert "embed_chunks" in names
    assert "extract_graph" in names
    assert "refresh_identity_embedding" in names
    assert "push_taxonomy_proposal_webhook" in names


def test_pilot_taxonomy_registry_loads() -> None:
    root = Path(__file__).resolve().parents[2] / "config" / "taxonomy_registry"
    registry = load_taxonomy_registry(root)
    assert len(registry.predicates) >= 1
    assert "person" in registry.entity_types
