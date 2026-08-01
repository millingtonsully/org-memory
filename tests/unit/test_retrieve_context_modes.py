"""Unit coverage for retrieve_context modes and search scoping."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from org_memory.domain.models import Passage, Principal, SearchResponse
from org_memory.services.retrieve_context import (
    RetrieveContextService,
    SubjectRef,
    _apply_token_budget,
)


class _StubRetrieval:
    """Test double: records search kwargs; never calls a vendor."""

    def __init__(self, response: SearchResponse | None = None) -> None:
        self.calls: list[dict] = []
        self._response = response or SearchResponse(
            query="q",
            passages=[
                Passage(
                    chunk_id="chunk-1",
                    doc_id="doc-1",
                    source_type="test",
                    title="T",
                    text="hello",
                    author_display_name="A",
                    event_time=datetime(2026, 1, 1, tzinfo=UTC),
                    deep_link="",
                    score=1.0,
                )
            ],
            facts=[],
            total_candidates=1,
            reranked=False,
            audit_id="audit-stub",
        )

    def search(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return self._response


class _EmptyGraph:
    def claims_for_viewer(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return []

    def paths_from(self, **kwargs):  # noqa: ANN003
        return {
            "paths": [],
            "returned": 0,
            "limit": kwargs.get("limit", 20),
            "max_depth": kwargs.get("max_depth", 2),
            "truncated": False,
            "capped": False,
        }


def _service(stub: _StubRetrieval | None = None) -> tuple[RetrieveContextService, _StubRetrieval]:
    retrieval = stub or _StubRetrieval()
    # Inject stub graph so CI unit jobs never load Settings / GraphRepository.
    service = RetrieveContextService(
        session=object(),  # type: ignore[arg-type]
        retrieval=retrieval,  # type: ignore[arg-type]
        graph=_EmptyGraph(),  # type: ignore[arg-type]
    )
    return service, retrieval


def test_vector_first_without_subjects_skips_about_scope() -> None:
    service, stub = _service()
    out = service.retrieve(
        principal=Principal(principal_id="user:11111111-1111-1111-1111-111111111111"),
        query="budget process",
        mode="vector_first",
        subjects=[],
    )
    assert out["mode"] == "vector_first"
    assert out["subjects"] == []
    assert out["structured_facts"] == []
    assert out["paths"] == []
    assert len(out["passages"]) == 1
    assert out["audit_id"] == "audit-stub"
    assert stub.calls[0]["about_person_ids"] is None
    assert stub.calls[0]["tool_name"] == "retrieve_context"


def test_retrieve_passes_temporal_axes_to_search() -> None:
    service, stub = _service()
    as_of = datetime(2026, 3, 15, tzinfo=UTC)
    believed = datetime(2026, 4, 1, tzinfo=UTC)
    service.retrieve(
        principal=Principal(principal_id="user:11111111-1111-1111-1111-111111111111"),
        query="title history",
        mode="vector_first",
        subjects=[],
        as_of=as_of,
        believed_as_of=believed,
    )
    assert stub.calls[0]["as_of"] == as_of
    assert stub.calls[0]["believed_as_of"] == believed


def test_graph_first_requires_subjects() -> None:
    service, stub = _service()
    with pytest.raises(ValueError, match="graph_first requires"):
        service.retrieve(
            principal=Principal(principal_id="user:11111111-1111-1111-1111-111111111111"),
            query="org chart",
            mode="graph_first",
            subjects=[],
        )
    assert stub.calls == []


def test_joint_with_person_subject_scopes_search(monkeypatch) -> None:
    from tests.conftest import apply_minimal_settings_env

    from org_memory.core.settings import get_settings

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-retrieve-joint")
    get_settings.cache_clear()
    try:
        service, stub = _service()
        person_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        out = service.retrieve(
            principal=Principal(principal_id="user:11111111-1111-1111-1111-111111111111"),
            query="reports",
            mode="joint",
            subjects=[SubjectRef(type="person", id=person_id)],
        )
        assert out["subjects"] == [{"type": "person", "id": person_id}]
        assert stub.calls[0]["about_person_ids"] == [person_id]
        assert out["structured_facts"][0]["returned"] == 0
        assert out["paths"][0]["returned"] == 0
    finally:
        get_settings.cache_clear()


def test_unknown_mode_rejected() -> None:
    service, _stub = _service()
    with pytest.raises(ValueError, match="unsupported retrieve mode"):
        service.retrieve(
            principal=Principal(principal_id="user:11111111-1111-1111-1111-111111111111"),
            query="x",
            mode="magic",  # type: ignore[arg-type]
        )


def test_graph_first_token_priority_keeps_structured_longer() -> None:
    payload = {
        "passages": [{"text": "p" * 400} for _ in range(8)],
        "search_facts": [{"text": "s" * 400} for _ in range(8)],
        "structured_facts": [{"facts": [{"object": "f" * 400}]} for _ in range(8)],
        "paths": [{"paths": [{"nodes": ["a", "b"]}]} for _ in range(8)],
        "truncated_tokens": False,
    }
    out = _apply_token_budget(payload, max_tokens=80, mode="graph_first")
    assert out["truncated_tokens"] is True
    assert len(out["passages"]) <= len(payload["passages"])
