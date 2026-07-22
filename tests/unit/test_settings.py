"""Settings validation: required retention and spend limits."""

from __future__ import annotations

import pytest
from tests.conftest import apply_minimal_settings_env


def test_settings_require_retention_and_spend(monkeypatch) -> None:
    apply_minimal_settings_env(monkeypatch, workspace_id="ws-settings")
    from org_memory.core.settings import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    assert settings.retention_days == 30
    assert settings.spend_alert_tokens_monthly == 1000
    assert settings.spend_hard_limit_tokens_monthly == 2000
    get_settings.cache_clear()


def test_hard_limit_must_be_at_least_alert(monkeypatch) -> None:
    apply_minimal_settings_env(monkeypatch, workspace_id="ws-settings-bad")
    monkeypatch.setenv("SPEND_ALERT_TOKENS_MONTHLY", "5000")
    monkeypatch.setenv("SPEND_HARD_LIMIT_TOKENS_MONTHLY", "1000")
    from org_memory.core.errors import ConfigurationError
    from org_memory.core.settings import get_settings

    get_settings.cache_clear()
    with pytest.raises(ConfigurationError):
        get_settings()
    get_settings.cache_clear()


def test_retention_zero_allowed_when_explicit(monkeypatch) -> None:
    apply_minimal_settings_env(monkeypatch, workspace_id="ws-retention-0")
    monkeypatch.setenv("RETENTION_DAYS", "0")
    from org_memory.core.settings import get_settings

    get_settings.cache_clear()
    assert get_settings().retention_days == 0
    get_settings.cache_clear()
