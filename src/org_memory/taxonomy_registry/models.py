"""Typed taxonomy registry shapes (closed schema for extraction + proposals)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class PlatformBinding(BaseModel):
    """Maps a registry predicate to a caller-platform taxonomy field."""

    taxonomy_key: str = Field(min_length=1)
    field_key: str = Field(min_length=1)


class EntityTypeDef(BaseModel):
    key: str = Field(min_length=1)
    description: str = ""


class PredicateDef(BaseModel):
    key: str = Field(min_length=1)
    subject_types: list[str] = Field(min_length=1)
    value_type: Literal["string", "number", "boolean", "datetime", "enum", "reference"] = "string"
    mutually_exclusive: bool = False
    structured_field_keys: list[str] = Field(default_factory=list)
    platform_binding: PlatformBinding | None = None
    description: str = ""

    @field_validator("key", "subject_types", mode="before")
    @classmethod
    def _normalize_keys(cls, value):
        if isinstance(value, str):
            return value.strip().lower()
        if isinstance(value, list):
            return [str(v).strip().lower() for v in value]
        return value


class RelationshipTypeDef(BaseModel):
    key: str = Field(min_length=1)
    from_types: list[str] = Field(min_length=1)
    to_types: list[str] = Field(min_length=1)
    mutually_exclusive: bool = False
    description: str = ""
    platform_binding: PlatformBinding | None = None

    @field_validator("key", "from_types", "to_types", mode="before")
    @classmethod
    def _normalize_keys(cls, value):
        if isinstance(value, str):
            return value.strip().lower()
        if isinstance(value, list):
            return [str(v).strip().lower() for v in value]
        return value


class TaxonomyRegistryFile(BaseModel):
    version: int = Field(ge=1)
    entity_types: list[EntityTypeDef] = Field(default_factory=list)
    predicates: list[PredicateDef] = Field(default_factory=list)
    relationship_types: list[RelationshipTypeDef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_content(self) -> TaxonomyRegistryFile:
        if not self.predicates and not self.relationship_types:
            raise ValueError("registry must define at least one predicate or relationship_type")
        pred_keys = [p.key for p in self.predicates]
        if len(pred_keys) != len(set(pred_keys)):
            raise ValueError("duplicate predicate keys in registry")
        rel_keys = [r.key for r in self.relationship_types]
        if len(rel_keys) != len(set(rel_keys)):
            raise ValueError("duplicate relationship_type keys in registry")
        return self


class TaxonomyRegistry:
    """In-memory index over one or more registry YAML files."""

    def __init__(self, files: list[TaxonomyRegistryFile]):
        self._files = files
        self.entity_types: dict[str, EntityTypeDef] = {}
        self.predicates: dict[str, PredicateDef] = {}
        self.relationship_types: dict[str, RelationshipTypeDef] = {}
        self._structured_to_predicate: dict[str, PredicateDef] = {}
        for file in files:
            for et in file.entity_types:
                if et.key in self.entity_types:
                    raise ValueError(f"duplicate entity_type key: {et.key}")
                self.entity_types[et.key] = et
            for pred in file.predicates:
                if pred.key in self.predicates:
                    raise ValueError(f"duplicate predicate key across files: {pred.key}")
                self.predicates[pred.key] = pred
                for sf_key in pred.structured_field_keys:
                    normalized = sf_key.strip()
                    if normalized in self._structured_to_predicate:
                        raise ValueError(f"structured_field_key claimed twice: {normalized}")
                    self._structured_to_predicate[normalized] = pred
            for rel in file.relationship_types:
                if rel.key in self.relationship_types:
                    raise ValueError(f"duplicate relationship_type across files: {rel.key}")
                self.relationship_types[rel.key] = rel

    def is_known_predicate(self, predicate: str) -> bool:
        return predicate.strip().lower() in self.predicates

    def is_known_relationship_type(self, relationship_type: str) -> bool:
        return relationship_type.strip().lower() in self.relationship_types

    def predicate_mutually_exclusive(self, predicate: str) -> bool | None:
        """True/False when registry defines exclusivity; None if unknown."""
        pred = self.predicates.get(predicate.strip().lower())
        if pred is None:
            return None
        return pred.mutually_exclusive

    def ground_truth_predicate_for_structured_key(self, key: str) -> PredicateDef | None:
        return self._structured_to_predicate.get(key.strip())

    def allowed_predicate_keys(self) -> list[str]:
        return sorted(self.predicates)

    def allowed_relationship_keys(self) -> list[str]:
        return sorted(self.relationship_types)

    def prompt_constraint_block(self) -> str:
        preds = ", ".join(self.allowed_predicate_keys()) or "(none)"
        rels = ", ".join(self.allowed_relationship_keys()) or "(none)"
        return (
            "Closed schema (taxonomy_registry):\n"
            f"- Allowed claim predicates ONLY: {preds}\n"
            f"- Allowed relationship_type values ONLY: {rels}\n"
            "- Do not invent predicates or relationship types outside these lists.\n"
            "- If the text supports a fact outside the lists, omit it."
        )
