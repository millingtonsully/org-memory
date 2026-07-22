"""Retention service no-op when retention_days is 0."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from tests.conftest import apply_minimal_settings_env

from org_memory.services.retention import RetentionService


@pytest.fixture()
def retention_off(monkeypatch):
    apply_minimal_settings_env(monkeypatch, workspace_id="ws-retention")
    monkeypatch.setenv("RETENTION_DAYS", "0")
    from org_memory.core.settings import get_settings

    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def test_purge_noop_when_retention_disabled(retention_off) -> None:
    store = MagicMock()
    session = MagicMock()
    result = RetentionService(session, store).purge_expired(batch_limit=10)
    assert result["purged"] == 0
    store.delete.assert_not_called()


def test_purge_document_clears_graph_evidence(retention_off, monkeypatch) -> None:
    monkeypatch.setenv("RETENTION_DAYS", "30")
    from org_memory.core.settings import get_settings

    get_settings.cache_clear()
    store = MagicMock()
    session = MagicMock()
    session.query.return_value.filter.return_value.all.return_value = []
    svc = RetentionService(session, store)
    svc._graph = MagicMock()
    doc = MagicMock()
    doc.doc_id = "doc:1"
    doc.raw_blob_key = ""
    doc.doc_metadata = {}
    svc._purge_document(doc)
    svc._graph.remove_document_evidence.assert_called_once_with("doc:1")
    get_settings.cache_clear()
