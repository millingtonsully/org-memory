"""Unit tests for search/retrieve channel diagnostics."""

from __future__ import annotations

from org_memory.services.retrieval import build_search_diagnostics


def test_build_search_diagnostics_shape_and_no_denied_doc_ids() -> None:
    diag = build_search_diagnostics(
        mode="hybrid",
        limit=10,
        candidate_pool=100,
        rrf_k=60,
        vector_count=12,
        keyword_count=8,
        fact_count=3,
        fused_count=15,
        shortlist_count=15,
        returned_passages=7,
        returned_facts=2,
        parent_dedupe_removed=1,
        reranked=True,
        rerank_skipped_reason=None,
        filters={
            "source_type": "email",
            "about_person_count": 1,
            "about_doc_count": 0,
            "doc_id": None,
        },
    )
    assert diag["channels"]["vector_candidates"] == 12
    assert diag["channels"]["keyword_candidates"] == 8
    assert diag["channels"]["fact_candidates"] == 3
    assert diag["rerank"]["applied"] is True
    assert diag["returned"]["parent_dedupe_removed"] == 1
    # Counts and filter labels only — no denied-doc enumeration keys.
    blob = str(diag)
    assert "denied" not in blob
    assert "allowed_principals" not in blob


def test_retrieve_diagnostics_include_graph_and_search(monkeypatch) -> None:
    from datetime import UTC, datetime

    from tests.conftest import apply_minimal_settings_env

    from org_memory.core.settings import get_settings
    from org_memory.domain.models import Passage, Principal, SearchResponse
    from org_memory.services.retrieve_context import RetrieveContextService, SubjectRef

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-retrieve-diag")
    get_settings.cache_clear()

    class StubRetrieval:
        def search(self, **kwargs):  # noqa: ANN003
            return SearchResponse(
                query=kwargs["query"],
                passages=[
                    Passage(
                        chunk_id="c1",
                        doc_id="d1",
                        source_type="test",
                        title="t",
                        text="hi",
                        author_display_name="a",
                        event_time=datetime(2026, 1, 1, tzinfo=UTC),
                        deep_link="",
                        score=1.0,
                    )
                ],
                facts=[],
                total_candidates=1,
                reranked=False,
                audit_id="a1",
                diagnostics={"channels": {"vector_candidates": 1}},
            )

    class EmptyGraph:
        def claims_for_viewer(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return []

        def paths_from(self, **kwargs):  # noqa: ANN003
            return {
                "paths": [],
                "returned": 0,
                "limit": 20,
                "max_depth": 2,
                "truncated": False,
                "capped": False,
            }

    try:
        service = RetrieveContextService(
            session=object(),  # type: ignore[arg-type]
            retrieval=StubRetrieval(),  # type: ignore[arg-type]
            graph=EmptyGraph(),  # type: ignore[arg-type]
        )
        out = service.retrieve(
            principal=Principal(
                principal_id="user:11111111-1111-1111-1111-111111111111"
            ),
            query="q",
            mode="joint",
            subjects=[
                SubjectRef(type="person", id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
            ],
        )
        assert out["diagnostics"]["search"]["channels"]["vector_candidates"] == 1
        assert out["diagnostics"]["graph"]["subject_count"] == 1
        assert out["diagnostics"]["graph"]["paths_returned"] == 0
        assert out["diagnostics"]["packing"]["truncated_tokens"] is False
        assert out["diagnostics"]["passage_temporal"]["derived_from"] is None
    finally:
        get_settings.cache_clear()


def test_retrieve_derives_passage_date_to_from_as_of(monkeypatch) -> None:
    from datetime import UTC, datetime

    from tests.conftest import apply_minimal_settings_env

    from org_memory.core.settings import get_settings
    from org_memory.domain.models import Principal, SearchResponse
    from org_memory.services.retrieve_context import RetrieveContextService

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-passage-asof")
    get_settings.cache_clear()
    captured: dict = {}

    class StubRetrieval:
        def search(self, **kwargs):  # noqa: ANN003
            captured.update(kwargs)
            return SearchResponse(
                query=kwargs["query"],
                passages=[],
                facts=[],
                total_candidates=0,
                reranked=False,
                audit_id="a1",
                diagnostics={},
            )

    try:
        service = RetrieveContextService(
            session=object(),  # type: ignore[arg-type]
            retrieval=StubRetrieval(),  # type: ignore[arg-type]
            graph=object(),  # type: ignore[arg-type]
        )
        as_of = datetime(2026, 3, 15, tzinfo=UTC)
        out = service.retrieve(
            principal=Principal(
                principal_id="user:11111111-1111-1111-1111-111111111111"
            ),
            query="title in March",
            mode="vector_first",
            as_of=as_of,
        )
        assert captured["date_to"] == as_of
        assert out["diagnostics"]["passage_temporal"]["derived_from"] == "as_of"
        assert out["diagnostics"]["passage_temporal"]["event_time_to"] == as_of.isoformat()
    finally:
        get_settings.cache_clear()


def test_retrieve_derives_passage_from_and_to_for_range_plan(monkeypatch) -> None:
    from datetime import UTC, datetime

    from tests.conftest import apply_minimal_settings_env

    from org_memory.core.settings import get_settings
    from org_memory.domain.models import Principal, SearchResponse
    from org_memory.services.retrieve_context import RetrieveContextService, SubjectRef

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-passage-range")
    get_settings.cache_clear()
    captured: dict = {}

    class StubRetrieval:
        def search(self, **kwargs):  # noqa: ANN003
            captured.update(kwargs)
            return SearchResponse(
                query=kwargs["query"],
                passages=[],
                facts=[],
                total_candidates=0,
                reranked=False,
                audit_id="a1",
                diagnostics={},
            )

    class EmptyGraph:
        def claims_for_viewer(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return []

        def paths_from(self, **kwargs):  # noqa: ANN003
            return {
                "paths": [],
                "returned": 0,
                "limit": 20,
                "max_depth": 2,
                "truncated": False,
                "capped": False,
            }

    try:
        service = RetrieveContextService(
            session=object(),  # type: ignore[arg-type]
            retrieval=StubRetrieval(),  # type: ignore[arg-type]
            graph=EmptyGraph(),  # type: ignore[arg-type]
        )
        out = service.retrieve(
            principal=Principal(
                principal_id="user:11111111-1111-1111-1111-111111111111"
            ),
            query="What changed in Alice's title between March 2026 and August 2026?",
            mode="joint",
            subjects=[
                SubjectRef(type="person", id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
            ],
        )
        assert captured["date_from"] == datetime(2026, 3, 15, tzinfo=UTC)
        assert captured["date_to"] == datetime(2026, 8, 15, tzinfo=UTC)
        pt = out["diagnostics"]["passage_temporal"]
        assert pt["derived_from"] == "as_of_range"
        assert pt["event_time_from"] == datetime(2026, 3, 15, tzinfo=UTC).isoformat()
        assert pt["event_time_to"] == datetime(2026, 8, 15, tzinfo=UTC).isoformat()
    finally:
        get_settings.cache_clear()
