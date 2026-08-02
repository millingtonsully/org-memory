"""Ingest blob ordering: DB before put; orphan cleanup after successful put."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from org_memory.domain.models import ChangeEnvelope, ChangeKind
from org_memory.services.ingest import IngestResult, IngestService

USER_A = "user:11111111-1111-1111-1111-111111111111"
_EVENT = datetime(2026, 7, 1, tzinfo=UTC)


class RecordingStore:
    def __init__(self, *, fail_put: bool = False) -> None:
        self.puts: list[str] = []
        self.deletes: list[str] = []
        self.fail_put = fail_put

    def put(self, key: str, content: bytes, content_type: str) -> None:
        if self.fail_put:
            raise RuntimeError("put failed")
        self.puts.append(key)

    def get(self, key: str) -> bytes:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        self.deletes.append(key)

    def ping(self) -> None:
        return


@pytest.fixture()
def ingest_settings(monkeypatch):
    from tests.conftest import apply_minimal_settings_env

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-ingest-test")
    from org_memory.core.settings import get_settings

    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def test_put_runs_after_db_and_fails_closed(ingest_settings) -> None:
    store = RecordingStore(fail_put=True)
    svc = IngestService(session=MagicMock(), object_store=store, entity_resolution=MagicMock())
    svc._apply_envelope = MagicMock(return_value=IngestResult(doc_id="test:1", status="accepted"))  # type: ignore[method-assign]
    envelope = ChangeEnvelope(
        source_system="test",
        external_id="1",
        change_kind=ChangeKind.create,
        source_type="doc",
        event_time=_EVENT,
        org_visible=True,
        allowed_principals=[],
        text="hello",
        title="t",
    )
    with pytest.raises(RuntimeError, match="put failed"):
        svc.ingest_envelope(envelope, b'{"ok":true}')
    svc._apply_envelope.assert_called_once()
    assert store.deletes == []


def test_orphan_blob_deleted_when_post_put_step_fails(ingest_settings) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    store = RecordingStore()
    session = sessionmaker(bind=create_engine("sqlite:///:memory:"))()
    svc = IngestService(session=session, object_store=store, entity_resolution=MagicMock())
    svc._apply_envelope = MagicMock(return_value=IngestResult(doc_id="test:1", status="accepted"))  # type: ignore[method-assign]
    svc._on_blob_archived = MagicMock(side_effect=RuntimeError("commit barrier failed"))  # type: ignore[method-assign]
    envelope = ChangeEnvelope(
        source_system="test",
        external_id="1",
        change_kind=ChangeKind.create,
        source_type="doc",
        event_time=_EVENT,
        org_visible=False,
        allowed_principals=[USER_A],
        text="hello",
        title="t",
    )
    with pytest.raises(RuntimeError, match="commit barrier"):
        svc.ingest_envelope(envelope, b'{"ok":true}')
    assert len(store.puts) == 1
    assert store.deletes == store.puts


def test_blob_deleted_when_session_rolls_back_after_successful_put(ingest_settings) -> None:
    """Put can succeed and ingest return; caller rollback must still clean the blob."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from org_memory.services.ingest import track_blob_for_rollback

    store = RecordingStore()
    engine = create_engine("sqlite:///:memory:")
    session = sessionmaker(bind=engine)()
    session.execute(text("SELECT 1"))
    blob_key = "ws/envelopes/test/1/20260701T000000Z-abcd.json"
    track_blob_for_rollback(session, store, blob_key)
    session.rollback()
    assert store.deletes == [blob_key]


def test_blob_kept_when_session_commits_after_put(ingest_settings) -> None:
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from org_memory.services.ingest import track_blob_for_rollback

    store = RecordingStore()
    engine = create_engine("sqlite:///:memory:")
    session = sessionmaker(bind=engine)()
    blob_key = "ws/envelopes/test/1/20260701T000000Z-efgh.json"
    track_blob_for_rollback(session, store, blob_key)
    session.execute(text("SELECT 1"))
    session.commit()
    assert store.deletes == []
