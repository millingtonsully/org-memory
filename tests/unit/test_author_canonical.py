"""author_canonical_entity_id expands to author patterns; unknown id fails closed."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from org_memory.domain.models import Principal
from org_memory.services.retrieval import RetrievalService

USER_A = "user:11111111-1111-1111-1111-111111111111"


@pytest.fixture()
def retrieval_settings(monkeypatch):
    from tests.conftest import apply_minimal_settings_env

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-retrieval-test")
    from org_memory.core.settings import get_settings

    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def test_unknown_canonical_returns_empty_without_search(retrieval_settings) -> None:
    from contextlib import contextmanager
    from unittest.mock import patch

    search = MagicMock()
    audit = MagicMock()
    audit.record_retrieval.return_value = "audit-1"
    persons = MagicMock()
    persons.get.return_value = None
    svc = RetrievalService(
        search_repo=search,
        audit_repo=audit,
        embedder=MagicMock(),
        reranker=MagicMock(),
        graph_repo=MagicMock(),
        person_repo=persons,
    )

    @contextmanager
    def _fake_session_scope():
        yield MagicMock()

    with (
        patch("org_memory.services.retrieval.session_scope", _fake_session_scope),
        patch("org_memory.services.retrieval.SpendRepository") as spend_cls,
    ):
        spend_repo = MagicMock()
        spend_repo.assert_under_hard_limit = MagicMock()
        spend_cls.return_value = spend_repo
        result = svc.search(
            principal=Principal(principal_id=USER_A),
            query="hello",
            author_canonical_entity_id="missing-person",
            tool_name="worldbuilder_kb",
        )
    assert result.passages == []
    assert result.total_candidates == 0
    search.vector_candidates.assert_not_called()


def test_canonical_expands_to_person_id(retrieval_settings) -> None:
    persons = MagicMock()
    persons.get.return_value = SimpleNamespace(
        canonical_id="p1",
        display_name="Ada Lovelace",
        merged_into_id=None,
        workspace_id="ws",
    )
    svc = RetrievalService(
        search_repo=MagicMock(),
        audit_repo=MagicMock(),
        embedder=MagicMock(),
        reranker=MagicMock(),
        graph_repo=MagicMock(),
        person_repo=persons,
    )
    assert svc._expand_canonical_author("p1") == ["p1"]
