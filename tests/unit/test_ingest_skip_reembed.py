"""Ingest skips chunk replace/embed when title and text are unchanged."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from tests.conftest import apply_minimal_settings_env

from org_memory.domain.jobs import JobType
from org_memory.domain.models import ChangeEnvelope, ChangeKind
from org_memory.services.ingest import IngestService

_EVENT = datetime(2026, 7, 1, tzinfo=UTC)


@pytest.fixture()
def ingest_settings(monkeypatch):
    apply_minimal_settings_env(monkeypatch, workspace_id="ws-ingest-skip")
    from org_memory.core.settings import get_settings

    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def _envelope(text: str = "same body", title: str = "same title") -> ChangeEnvelope:
    return ChangeEnvelope(
        source_system="test",
        external_id="1",
        change_kind=ChangeKind.create,
        source_type="doc",
        event_time=_EVENT,
        org_visible=True,
        allowed_principals=[],
        text=text,
        title=title,
    )


def test_unchanged_content_skips_replace_and_embed(ingest_settings) -> None:
    session = MagicMock()
    existing = SimpleNamespace(
        rendered_text="same body",
        title="same title",
        deleted=False,
        org_visible=True,
        allowed_principals=[],
        author_display_name="",
        event_time=_EVENT,
        deep_link="",
        source_type="doc",
    )
    session.get.return_value = existing
    stored = SimpleNamespace(
        org_visible=True,
        allowed_principals=[],
        title="same title",
        author_display_name="",
        event_time=_EVENT,
        deep_link="",
        source_type="doc",
    )
    docs = MagicMock()
    docs.upsert_content.return_value = stored
    jobs = MagicMock()
    entities = MagicMock()
    entities.observe_identity.return_value = None
    svc = IngestService(session=session, object_store=MagicMock(), entity_resolution=entities)
    svc._docs = docs
    svc._jobs = jobs
    svc._versions = MagicMock()
    svc._connectors = MagicMock()
    svc._structured = MagicMock()
    svc._structured.apply.return_value = []

    svc._apply_envelope(_envelope(), "test:1", "blob", "hash", "ws-ingest-skip")

    docs.replace_chunks.assert_not_called()
    docs.sync_chunk_metadata.assert_called_once()
    embed_calls = [
        call
        for call in jobs.enqueue.call_args_list
        if call.args and call.args[0] == JobType.embed_chunks
    ]
    assert embed_calls == []


def test_changed_content_replaces_and_embeds(ingest_settings) -> None:
    session = MagicMock()
    session.get.return_value = SimpleNamespace(rendered_text="old", title="old", deleted=False)
    stored = SimpleNamespace(
        org_visible=True,
        allowed_principals=[],
        title="new",
        author_display_name="",
        event_time=_EVENT,
        deep_link="",
        source_type="doc",
    )
    docs = MagicMock()
    docs.upsert_content.return_value = stored
    docs.replace_chunks.return_value = 0
    jobs = MagicMock()
    entities = MagicMock()
    entities.observe_identity.return_value = None
    svc = IngestService(session=session, object_store=MagicMock(), entity_resolution=entities)
    svc._docs = docs
    svc._jobs = jobs
    svc._versions = MagicMock()
    svc._connectors = MagicMock()
    svc._structured = MagicMock()
    svc._structured.apply.return_value = []

    svc._apply_envelope(
        _envelope(text="new body", title="new"),
        "test:1",
        "blob",
        "hash",
        "ws-ingest-skip",
    )

    docs.replace_chunks.assert_called_once()
    assert any(
        call.args and call.args[0] == JobType.embed_chunks for call in jobs.enqueue.call_args_list
    )


def test_full_embedding_carry_over_skips_embed_job(ingest_settings) -> None:
    """When every child chunk keeps its embedding, no embed job is enqueued."""
    session = MagicMock()
    session.get.return_value = SimpleNamespace(rendered_text="old", title="old", deleted=False)
    stored = SimpleNamespace(
        org_visible=True,
        allowed_principals=[],
        title="new",
        author_display_name="",
        event_time=_EVENT,
        deep_link="",
        source_type="doc",
    )
    docs = MagicMock()
    docs.upsert_content.return_value = stored
    # Report that every child chunk reused a stored embedding.
    docs.replace_chunks.side_effect = lambda doc_id, chunks, **kwargs: sum(
        1 for chunk in chunks if chunk.chunk_role == "child"
    )
    jobs = MagicMock()
    entities = MagicMock()
    entities.observe_identity.return_value = None
    svc = IngestService(session=session, object_store=MagicMock(), entity_resolution=entities)
    svc._docs = docs
    svc._jobs = jobs
    svc._versions = MagicMock()
    svc._connectors = MagicMock()
    svc._structured = MagicMock()
    svc._structured.apply.return_value = []

    svc._apply_envelope(
        _envelope(text="new body", title="new"),
        "test:1",
        "blob",
        "hash",
        "ws-ingest-skip",
    )

    docs.replace_chunks.assert_called_once()
    embed_calls = [
        call
        for call in jobs.enqueue.call_args_list
        if call.args and call.args[0] == JobType.embed_chunks
    ]
    assert embed_calls == []


def test_replace_chunks_receives_active_embedding_model(ingest_settings) -> None:
    """Carry-over is keyed to the configured model so vectors never cross models."""
    session = MagicMock()
    session.get.return_value = None
    stored = SimpleNamespace(
        org_visible=True,
        allowed_principals=[],
        title="t",
        author_display_name="",
        event_time=_EVENT,
        deep_link="",
        source_type="doc",
    )
    docs = MagicMock()
    docs.upsert_content.return_value = stored
    docs.replace_chunks.return_value = 0
    entities = MagicMock()
    entities.observe_identity.return_value = None
    svc = IngestService(session=session, object_store=MagicMock(), entity_resolution=entities)
    svc._docs = docs
    svc._jobs = MagicMock()
    svc._versions = MagicMock()
    svc._connectors = MagicMock()
    svc._structured = MagicMock()
    svc._structured.apply.return_value = []

    svc._apply_envelope(_envelope(), "test:1", "blob", "hash", "ws-ingest-skip")

    kwargs = docs.replace_chunks.call_args.kwargs
    assert kwargs["reuse_embeddings_for_model"] == ingest_settings.embedding_model
    chunks = docs.replace_chunks.call_args.args[1]
    assert all(chunk.content_hash for chunk in chunks)


def test_tombstone_revive_replaces_chunks(ingest_settings) -> None:
    session = MagicMock()
    session.get.return_value = SimpleNamespace(
        rendered_text="same body",
        title="same title",
        deleted=True,
    )
    stored = SimpleNamespace(
        org_visible=True,
        allowed_principals=[],
        title="same title",
        author_display_name="",
        event_time=_EVENT,
        deep_link="",
        source_type="doc",
    )
    docs = MagicMock()
    docs.upsert_content.return_value = stored
    docs.replace_chunks.return_value = 0
    jobs = MagicMock()
    entities = MagicMock()
    entities.observe_identity.return_value = None
    svc = IngestService(session=session, object_store=MagicMock(), entity_resolution=entities)
    svc._docs = docs
    svc._jobs = jobs
    svc._versions = MagicMock()
    svc._connectors = MagicMock()
    svc._structured = MagicMock()
    svc._structured.apply.return_value = []

    svc._apply_envelope(_envelope(), "test:1", "blob", "hash", "ws-ingest-skip")

    docs.replace_chunks.assert_called_once()
    docs.sync_chunk_metadata.assert_not_called()
    assert any(
        call.args and call.args[0] == JobType.embed_chunks for call in jobs.enqueue.call_args_list
    )
