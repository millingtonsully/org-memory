"""Shared hermetic Postgres fixtures. DATABASE_URL only. No vendor calls."""

from __future__ import annotations

import os
import uuid

import pytest


@pytest.fixture()
def hermetic_workspace(monkeypatch):
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL absent")
    ws = f"hermetic-{uuid.uuid4().hex[:12]}"
    monkeypatch.setenv("WORKSPACE_ID", ws)
    monkeypatch.setenv("EMBEDDING_API_KEY", os.environ.get("EMBEDDING_API_KEY", "hermetic-unused"))
    monkeypatch.setenv("RERANK_API_KEY", os.environ.get("RERANK_API_KEY", "hermetic-unused"))
    monkeypatch.setenv("SERVICE_API_KEY", os.environ.get("SERVICE_API_KEY", "hermetic-unused"))
    monkeypatch.setenv("OBJECT_STORE_BACKEND", "supabase")
    monkeypatch.setenv(
        "SUPABASE_PROJECT_URL",
        os.environ.get("SUPABASE_PROJECT_URL", "https://hermetic.invalid"),
    )
    monkeypatch.setenv(
        "SUPABASE_SERVICE_ROLE_KEY",
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "hermetic-unused"),
    )
    monkeypatch.setenv("RETENTION_DAYS", os.environ.get("RETENTION_DAYS", "30"))
    monkeypatch.setenv(
        "SPEND_ALERT_TOKENS_MONTHLY",
        os.environ.get("SPEND_ALERT_TOKENS_MONTHLY", "1000000"),
    )
    monkeypatch.setenv(
        "SPEND_HARD_LIMIT_TOKENS_MONTHLY",
        os.environ.get("SPEND_HARD_LIMIT_TOKENS_MONTHLY", "2000000"),
    )
    monkeypatch.setenv("WORLDBUILDER_CACHE_TTL_SECONDS", "3600")
    from org_memory.core.settings import get_settings
    from org_memory.db import engine as engine_mod

    get_settings.cache_clear()
    engine_mod._engine = None
    engine_mod._session_factory = None
    yield ws
    get_settings.cache_clear()
    engine_mod._engine = None
    engine_mod._session_factory = None
