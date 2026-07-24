"""Rerank is skipped when the shortlist fits within the request limit."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from org_memory.domain.models import Principal
from org_memory.services.retrieval import RetrievalService

USER_A = "user:11111111-1111-1111-1111-111111111111"


@pytest.fixture()
def retrieval_settings(monkeypatch):
    from tests.conftest import apply_minimal_settings_env

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-rerank-test")
    from org_memory.core.settings import get_settings

    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def test_shortlist_within_limit_skips_rerank(retrieval_settings) -> None:
    search = MagicMock()
    hit = {
        "chunk_id": "doc#0",
        "doc_id": "doc",
        "source_type": "notion",
        "title": "T",
        "text": "parent section text",
        "author_display_name": "Ada",
        "event_time": datetime(2024, 1, 1, tzinfo=UTC),
        "deep_link": "",
        "rank": 1,
    }
    search.vector_candidates.return_value = [hit]
    search.keyword_candidates.return_value = []
    graph = MagicMock()
    graph.fact_candidates.return_value = []
    audit = MagicMock()
    audit.record_retrieval.return_value = "audit-1"
    embedder = MagicMock()
    embedder.embed_texts.return_value = ([[0.1, 0.2]], 3)
    embedder.model_name = "embed-model"
    reranker = MagicMock()

    svc = RetrievalService(
        search_repo=search,
        audit_repo=audit,
        embedder=embedder,
        reranker=reranker,
        graph_repo=graph,
    )

    @contextmanager
    def _fake_session_scope():
        yield MagicMock()

    with (
        patch("org_memory.services.retrieval.session_scope", _fake_session_scope),
        patch("org_memory.services.retrieval.SpendRepository") as spend_cls,
    ):
        spend_repo = MagicMock(spec=["assert_under_hard_limit", "record"])
        spend_cls.return_value = spend_repo
        result = svc.search(
            principal=Principal(principal_id=USER_A),
            query="widgets",
            limit=10,
        )

    assert result.reranked is False
    assert len(result.passages) == 1
    assert result.passages[0].text == "parent section text"
    reranker.rerank.assert_not_called()
