"""Tool wire request parsing (MCP / worldbuilder shapes)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from org_memory.api.tool_wire import (
    SearchKnowledgeBaseRequest,
    WorldbuilderKbRequest,
    fact_to_kb_hit,
    parse_yyyy_mm_dd,
)
from org_memory.domain.models import FactPassage


def test_parse_yyyy_mm_dd_end_of_day() -> None:
    dt = parse_yyyy_mm_dd("2026-07-01", end_of_day=True)
    assert dt is not None
    assert dt.day == 1
    assert dt.hour == 23


def test_search_kb_request_coerces_dates() -> None:
    body = SearchKnowledgeBaseRequest(query="hello", date_from=date(2026, 1, 1))
    assert body.query == "hello"
    assert body.date_from == date(2026, 1, 1)


def test_worldbuilder_kb_requires_about_or_query() -> None:
    with pytest.raises(ValueError, match="about"):
        WorldbuilderKbRequest()


def test_search_kb_accepts_any_source_type() -> None:
    body = SearchKnowledgeBaseRequest(query="hello", source_type="jira_issue")
    assert body.source_type == "jira_issue"


def test_worldbuilder_kb_resolves_about() -> None:
    body = WorldbuilderKbRequest(about="Ada")
    assert body.resolved_query() == "Ada"


def test_fact_to_kb_hit_includes_time_grain() -> None:
    hit = fact_to_kb_hit(
        FactPassage(
            fact_id="c1",
            fact_type="claim",
            text="title: Engineer",
            confidence=0.9,
            evidence_doc_ids=["doc:1"],
            status="active",
            valid_from=datetime(2026, 3, 1, tzinfo=UTC),
            time_grain="month",
            score=1.0,
        )
    )
    assert hit["time_grain"] == "month"
    assert hit["status"] == "active"
