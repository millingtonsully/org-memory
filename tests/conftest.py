"""Shared test helpers."""

from __future__ import annotations

import os

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "postgres: hermetic Postgres ACL/SQL tests; require DATABASE_URL only",
    )
    config.addinivalue_line(
        "markers",
        "integration: requires real credentials (skipped when absent)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not os.environ.get("DATABASE_URL"):
        skip_pg = pytest.mark.skip(reason="DATABASE_URL absent (set it to run hermetic postgres tests)")
        for item in items:
            if "postgres" in item.keywords:
                item.add_marker(skip_pg)


def apply_minimal_settings_env(monkeypatch: pytest.MonkeyPatch, *, workspace_id: str) -> None:
    """Env required for get_settings() in unit tests (no real services)."""
    monkeypatch.setenv("WORKSPACE_ID", workspace_id)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("SERVICE_API_KEY", "test-key")
    monkeypatch.setenv("EMBEDDING_API_KEY", "embed-key")
    monkeypatch.setenv("RERANK_API_KEY", "rerank-key")
    monkeypatch.setenv("OBJECT_STORE_BACKEND", "supabase")
    monkeypatch.setenv("SUPABASE_PROJECT_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role")
    monkeypatch.setenv("RETENTION_DAYS", "30")
    monkeypatch.setenv("SPEND_ALERT_TOKENS_MONTHLY", "1000")
    monkeypatch.setenv("SPEND_HARD_LIMIT_TOKENS_MONTHLY", "2000")
