"""Load and validate taxonomy registry YAML from disk."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from org_memory.core.errors import ConfigurationError
from org_memory.taxonomy_registry.models import TaxonomyRegistry, TaxonomyRegistryFile


def resolve_registry_dir(configured: str) -> Path:
    """Resolve TAXONOMY_REGISTRY_DIR relative to cwd, then repo root."""
    path = Path(configured)
    if path.is_absolute() and path.is_dir():
        return path
    cwd_candidate = (Path.cwd() / path).resolve()
    if cwd_candidate.is_dir():
        return cwd_candidate
    # src/org_memory/taxonomy_registry/loader.py → repo root is parents[3]
    repo_root = Path(__file__).resolve().parents[3]
    repo_candidate = (repo_root / path).resolve()
    if repo_candidate.is_dir():
        return repo_candidate
    raise ConfigurationError(
        f"TAXONOMY_REGISTRY_DIR not found: {configured!r} "
        f"(tried {cwd_candidate} and {repo_candidate})"
    )


def load_taxonomy_registry(directory: str | Path) -> TaxonomyRegistry:
    """Load all *.yaml / *.yml files in directory; fail closed on any error."""
    root = directory if isinstance(directory, Path) else resolve_registry_dir(str(directory))
    files = sorted([*root.glob("*.yaml"), *root.glob("*.yml")])
    if not files:
        raise ConfigurationError(f"No taxonomy registry YAML files in {root}")
    parsed: list[TaxonomyRegistryFile] = []
    for path in files:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigurationError(f"Invalid YAML in {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigurationError(f"Registry file must be a mapping: {path}")
        try:
            parsed.append(TaxonomyRegistryFile.model_validate(raw))
        except ValidationError as exc:
            raise ConfigurationError(f"Invalid taxonomy registry schema in {path}: {exc}") from exc
    try:
        return TaxonomyRegistry(parsed)
    except ValueError as exc:
        raise ConfigurationError(f"Taxonomy registry merge failed: {exc}") from exc
