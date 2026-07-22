"""S3 object store adapter (moto-backed)."""

from __future__ import annotations

import pytest

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")


@pytest.fixture()
def s3_settings(monkeypatch):
    from tests.conftest import apply_minimal_settings_env

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-s3-test")
    monkeypatch.setenv("OBJECT_STORE_BACKEND", "s3")
    monkeypatch.setenv("S3_BUCKET_NAME", "org-memory-test")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("SUPABASE_PROJECT_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.setenv("S3_SSE", "AES256")
    from org_memory.core.settings import get_settings

    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def test_s3_put_get_delete_roundtrip(s3_settings) -> None:
    from moto import mock_aws

    from org_memory.adapters.s3_storage import S3ObjectStore
    from org_memory.core.errors import NotFoundError

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="org-memory-test")
        store = S3ObjectStore(s3_settings)
        store.put("a/b.json", b'{"ok":true}', "application/json")
        assert store.get("a/b.json") == b'{"ok":true}'
        store.delete("a/b.json")
        with pytest.raises(NotFoundError):
            store.get("a/b.json")


def test_s3_delete_missing_is_idempotent(s3_settings) -> None:
    from moto import mock_aws

    from org_memory.adapters.s3_storage import S3ObjectStore

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="org-memory-test")
        store = S3ObjectStore(s3_settings)
        store.delete("never-existed")


def test_proposal_payload_includes_evidence_doc_ids() -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from org_memory.services.proposal_webhook import proposal_payload

    row = SimpleNamespace(
        proposal_id="prop-1",
        subject_type="person",
        subject_id="p1",
        taxonomy_key="person",
        field_key="title",
        predicate="title",
        value_text="VP",
        confidence=0.9,
        evidence_doc_ids=["slack:1"],
        source_claim_id="c1",
        precedence_class="extraction_multi",
        status="pending",
        created_at=datetime(2026, 7, 21, tzinfo=UTC),
    )
    payload = proposal_payload(row)  # type: ignore[arg-type]
    assert payload["evidence_doc_ids"] == ["slack:1"]
