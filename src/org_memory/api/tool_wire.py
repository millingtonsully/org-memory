"""Tool wire adapters (request/response shapes at the HTTP edge).

These helpers translate to the MCP and Worldbuilder envelopes the caller platform already uses.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from org_memory.domain.models import Passage, SearchResponse

# Closed source_type filter used by search_knowledge_base on the caller side.
KbSourceType = Literal["calendar_event", "slack_message", "notion_page"]

# Worldbuilder connector enum to our free-string source_type labels.
CONNECTOR_TO_SOURCE_TYPE: dict[str, str] = {
    "slack": "slack_message",
    "notion": "notion_page",
    "gmail": "gmail_email",
    "github": "github_item",
    "gcal": "calendar_event",
    "jira": "jira_issue",
}


class ToolConnector(str, Enum):
    slack = "slack"
    notion = "notion"
    gmail = "gmail"
    github = "github"
    gcal = "gcal"
    jira = "jira"


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
    """Minimal stable hit object for MCP search_knowledge_base responses."""
    created = passage.event_time.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return {
        "doc_id": passage.doc_id,
        "text": passage.text,
        "source_type": passage.source_type,
        "author": passage.author_display_name,
        "created_at": created,
        "updated_at": created,
        "title": passage.title,
        "deep_link": passage.deep_link,
        "score": passage.score,
    }


def search_response_to_mcp(response: SearchResponse) -> McpToolResponse:
    hits = [passage_to_kb_hit(p) for p in response.passages]
    return McpToolResponse(
        content=[{"type": "text", "text": json.dumps(hits, separators=(",", ":"))}],
        isError=False,
        status=200,
    )


def empty_mcp() -> McpToolResponse:
    return McpToolResponse(
        content=[{"type": "text", "text": "[]"}],
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
    source_type: KbSourceType | None = None
    author: str | None = None
    date_from: date | None = Field(default=None, description="YYYY-MM-DD inclusive start")
    date_to: date | None = Field(default=None, description="YYYY-MM-DD inclusive end")
    updated_from: date | None = None
    updated_to: date | None = None
    doc_id: str | None = None
    limit: int = Field(default=50, ge=1, le=200)
    half_life_days: float = Field(default=30.0, ge=1, le=365)
    min_decay: float = Field(default=0.0, ge=0, le=1)

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
    connector: ToolConnector | None = None
    mode: RetrievalMode = RetrievalMode.hybrid
    limit: int = Field(default=10, ge=1, le=25)

    @model_validator(mode="after")
    def _require_about_or_query(self) -> WorldbuilderKbRequest:
        if not (self.about or self.query):
            raise ValueError("Provide `about` (preferred) or `query`.")
        return self

    def resolved_query(self) -> str:
        return (self.about or self.query or "").strip()

    def resolved_source_type(self) -> str | None:
        if self.connector is None:
            return None
        return CONNECTOR_TO_SOURCE_TYPE[self.connector.value]


class WorldbuilderLookupAction(str, Enum):
    profile = "profile"
    read_source = "read_source"


class WorldbuilderLookupRequest(BaseModel):
    """worldbuilder_lookup arguments."""

    action: WorldbuilderLookupAction | None = None
    name: str | None = None
    category: str | None = None
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
