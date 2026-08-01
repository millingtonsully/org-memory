"""Smoke tests for extract_graph handler early-exit paths."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from org_memory.workers.handlers.graph_extraction import handle_extract_graph


def test_extract_graph_skips_missing_document() -> None:
    session = MagicMock()
    session.get.return_value = None
    synthesizer = MagicMock()
    embedder = MagicMock()
    handle_extract_graph(session, {"doc_id": "missing"}, synthesizer, embedder)
    synthesizer.complete.assert_not_called()


def test_extract_graph_skips_stale_content_hash() -> None:
    session = MagicMock()
    session.get.return_value = SimpleNamespace(
        doc_id="doc:1",
        deleted=False,
        rendered_text="current text",
    )
    synthesizer = MagicMock()
    embedder = MagicMock()
    handle_extract_graph(
        session,
        {"doc_id": "doc:1", "content_hash": "not-the-current-hash"},
        synthesizer,
        embedder,
    )
    synthesizer.complete.assert_not_called()
