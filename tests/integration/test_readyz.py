"""Integration markers: real credentials only; skip when absent."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


def _has_integration_creds() -> bool:
    return bool(
        os.environ.get("DATABASE_URL")
        and os.environ.get("SERVICE_API_KEY")
        and os.environ.get("EMBEDDING_API_KEY")
        and os.environ.get("RERANK_API_KEY")
        and os.environ.get("RETENTION_DAYS")
        and os.environ.get("SPEND_ALERT_TOKENS_MONTHLY")
        and os.environ.get("SPEND_HARD_LIMIT_TOKENS_MONTHLY")
        and (
            (
                os.environ.get("OBJECT_STORE_BACKEND", "supabase") == "supabase"
                and os.environ.get("SUPABASE_PROJECT_URL")
                and os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
            )
            or (
                os.environ.get("OBJECT_STORE_BACKEND") == "s3"
                and os.environ.get("S3_BUCKET_NAME")
                and os.environ.get("AWS_REGION")
            )
        )
    )


@pytest.fixture(autouse=True)
def _require_creds() -> None:
    if not _has_integration_creds():
        pytest.skip("integration credentials absent")


def test_readyz_against_live_stack() -> None:
    from fastapi.testclient import TestClient

    from org_memory.core.settings import get_settings
    from org_memory.main import app

    get_settings.cache_clear()
    client = TestClient(app)
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ok"
