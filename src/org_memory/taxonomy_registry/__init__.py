"""Closed, versioned taxonomy schema for extraction, facts, and proposals."""

from __future__ import annotations

from functools import lru_cache

from org_memory.taxonomy_registry.loader import load_taxonomy_registry, resolve_registry_dir
from org_memory.taxonomy_registry.models import (
    EntityTypeDef,
    PlatformBinding,
    PredicateDef,
    RelationshipTypeDef,
    TaxonomyRegistry,
)

__all__ = [
    "EntityTypeDef",
    "PlatformBinding",
    "PredicateDef",
    "RelationshipTypeDef",
    "TaxonomyRegistry",
    "clear_taxonomy_registry_cache",
    "get_taxonomy_registry",
    "load_taxonomy_registry",
    "resolve_registry_dir",
]


@lru_cache
def get_taxonomy_registry() -> TaxonomyRegistry:
    """Load registry once per process; invalid YAML fails at first call."""
    from org_memory.core.settings import get_settings

    return load_taxonomy_registry(get_settings().taxonomy_registry_dir)


def clear_taxonomy_registry_cache() -> None:
    get_taxonomy_registry.cache_clear()
