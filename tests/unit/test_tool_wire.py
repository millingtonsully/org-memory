"""Tool wire request parsing (MCP / worldbuilder shapes)."""

from __future__ import annotations

from datetime import date

import pytest

from org_memory.api.tool_wire import (
    SearchKnowledgeBaseRequest,
    WorldbuilderKbRequest,
    parse_yyyy_mm_dd,
)


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
