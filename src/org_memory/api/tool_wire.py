"""Tool wire adapters (request/response shapes at the HTTP edge).

These helpers translate to the MCP and Worldbuilder envelopes the caller platform already uses.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from org_memory.domain.models import FactPassage, Passage, SearchResponse


class RetrievalMode(str, Enum):
    hybrid = "hybrid"
    semantic = "semantic"
    keyword = "keyword"


class McpToolResponse(BaseModel):
    """MCP tool result envelope used by the caller agent runtime."""

    content: list[dict[str, str]]
    isError: bool = False
    status: int = 200


def parse_yyyy_mm_dd(value: date | datetime | str | None, *, end_of_day: bool = False) -> datetime | None:
    """Accept YYYY-MM-DD dates (or datetime) as UTC bounds."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    day = value if isinstance(value, date) else date.fromisoformat(value)
    if end_of_day:
        return datetime.combine(day, time(23, 59, 59, 999999), tzinfo=UTC)
    return datetime.combine(day, time.min, tzinfo=UTC)


def passage_to_kb_hit(passage: Passage) -> dict[str, Any]:
    """Stable hit object for MCP search_knowledge_base responses."""
    event_time = passage.event_time.astimezone(UTC).isoformat().replace("+00:00", "Z")
    hit: dict[str, Any] = {
        "chunk_id": passage.chunk_id,
        "doc_id": passage.doc_id,
        "text": passage.text,
        "source_type": passage.source_type,
        "author": passage.author_display_name,
        "event_time": event_time,
        "title": passage.title,
        "deep_link": passage.deep_link,
        "score": passage.score,
    }
    if passage.source_system:
        hit["source_system"] = passage.source_system
    if passage.updated_at is not None:
        hit["updated_at"] = passage.updated_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return hit


def fact_to_kb_hit(fact: FactPassage) -> dict[str, Any]:
    hit: dict[str, Any] = {
        "fact_id": fact.fact_id,
        "fact_type": fact.fact_type,
        "text": fact.text,
        "confidence": fact.confidence,
        "evidence_doc_ids": fact.evidence_doc_ids,
        "evidence_quotes": fact.evidence_quotes,
        "score": fact.score,
        "status": fact.status,
    }
    if fact.valid_from is not None:
        hit["valid_from"] = fact.valid_from.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if fact.valid_to is not None:
        hit["valid_to"] = fact.valid_to.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return hit


def search_response_to_mcp(response: SearchResponse) -> McpToolResponse:
    payload = {
        "passages": [passage_to_kb_hit(p) for p in response.passages],
        "facts": [fact_to_kb_hit(f) for f in response.facts],
    }
    return McpToolResponse(
        content=[{"type": "text", "text": json.dumps(payload, separators=(",", ":"))}],
        isError=False,
        status=200,
    )


def worldbuilder_envelope(
    *,
    status: str,
    items: list[Any],
    summary: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "items": items,
        "summary": summary,
        "metadata": metadata or {},
    }


class SearchKnowledgeBaseRequest(BaseModel):
    """MCP search_knowledge_base arguments."""

    query: str = Field(min_length=1)
    source_type: str | None = Field(
        default=None,
        description="Opaque object-kind label (free string; same as ChangeEnvelope.source_type).",
    )
    source_system: str | None = Field(
        default=None,
        description="Opaque connector/system id (free string; same as ChangeEnvelope.source_system).",
    )
    author: str | None = None
    date_from: date | None = Field(default=None, description="YYYY-MM-DD inclusive start")
    date_to: date | None = Field(default=None, description="YYYY-MM-DD inclusive end")
    updated_from: date | None = None
    updated_to: date | None = None
    doc_id: str | None = None
    limit: int = Field(default=50, ge=1, le=200)
    half_life_days: float = Field(default=90.0, ge=1, le=365)
    min_decay: float = Field(default=0.3, ge=0, le=1)

    @field_validator("date_from", "date_to", "updated_from", "updated_to", mode="before")
    @classmethod
    def _coerce_date(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value.date()
        return value


class WorldbuilderKbRequest(BaseModel):
    """worldbuilder_kb arguments."""

    about: str | None = Field(default=None, min_length=1)
    # Alias kept for older callers. Preferred field is `about`.
    query: str | None = Field(default=None, min_length=1)
    author: str | None = None
    author_canonical_entity_id: str | None = None
    source_type: str | None = Field(
        default=None,
        description="Opaque object-kind label (free string).",
    )
    source_system: str | None = Field(
        default=None,
        description="Opaque connector/system id (free string).",
    )
    mode: RetrievalMode = RetrievalMode.hybrid
    limit: int = Field(default=10, ge=1, le=25)

    @model_validator(mode="after")
    def _require_about_or_query(self) -> WorldbuilderKbRequest:
        if not (self.about or self.query):
            raise ValueError("Provide `about` (preferred) or `query`.")
        return self

    def resolved_query(self) -> str:
        return (self.about or self.query or "").strip()


class WorldbuilderLookupAction(str, Enum):
    profile = "profile"
    read_source = "read_source"


class WorldbuilderLookupRequest(BaseModel):
    """worldbuilder_lookup arguments."""

    action: WorldbuilderLookupAction | None = None
    name: str | None = None
    query: str | None = None
    source_document_ids: list[str] | None = None
    source_record_ids: list[str] | None = None
    # Legacy alias used by existing callers.
    read_source: list[str] | None = None

    def document_ids(self) -> list[str]:
        ids = self.source_document_ids or self.source_record_ids or self.read_source or []
        return [doc_id for doc_id in ids if doc_id.strip()]

    def resolved_action(self) -> WorldbuilderLookupAction:
        if self.action is not None:
            return self.action
        if self.document_ids() and not self.name:
            return WorldbuilderLookupAction.read_source
        if self.name and not self.document_ids():
            return WorldbuilderLookupAction.profile
        raise ValueError(
            "Provide action=profile with name, or action=read_source with source_document_ids."
        )
