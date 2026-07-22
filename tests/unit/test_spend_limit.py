"""Spend hard-limit gate."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from tests.conftest import apply_minimal_settings_env

from org_memory.core.errors import SpendLimitError
from org_memory.db.repositories.spend import SpendRepository


@pytest.fixture()
def spend_settings(monkeypatch):
    apply_minimal_settings_env(monkeypatch, workspace_id="ws-spend")
    monkeypatch.setenv("SPEND_ALERT_TOKENS_MONTHLY", "100")
    monkeypatch.setenv("SPEND_HARD_LIMIT_TOKENS_MONTHLY", "200")
    from org_memory.core.settings import get_settings

    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def test_assert_under_hard_limit_passes(spend_settings) -> None:
    repo = object.__new__(SpendRepository)
    repo._session = MagicMock()
    repo._lock_workspace_spend = MagicMock()  # type: ignore[method-assign]
    repo.totals_by_class_this_month = MagicMock(return_value={"embed": 50})  # type: ignore[method-assign]
    repo.assert_under_hard_limit()


def test_assert_under_hard_limit_raises(spend_settings) -> None:
    repo = object.__new__(SpendRepository)
    repo._session = MagicMock()
    repo._lock_workspace_spend = MagicMock()  # type: ignore[method-assign]
    repo.totals_by_class_this_month = MagicMock(return_value={"embed": 200})  # type: ignore[method-assign]
    with pytest.raises(SpendLimitError) as exc:
        repo.assert_under_hard_limit()
    assert exc.value.tokens_used == 200
    assert exc.value.hard_limit == 200


def test_record_raises_when_at_hard_limit(spend_settings) -> None:
    repo = object.__new__(SpendRepository)
    repo._session = MagicMock()
    repo._lock_workspace_spend = MagicMock()  # type: ignore[method-assign]
    repo.totals_by_class_this_month = MagicMock(return_value={"embed": 200})  # type: ignore[method-assign]
    with pytest.raises(SpendLimitError):
        repo.record("embed", "embedding", "model", 10)
    repo._session.add.assert_not_called()


def test_record_raises_when_batch_would_exceed_limit(spend_settings) -> None:
    repo = object.__new__(SpendRepository)
    repo._session = MagicMock()
    repo._lock_workspace_spend = MagicMock()  # type: ignore[method-assign]
    repo.totals_by_class_this_month = MagicMock(return_value={"embed": 195})  # type: ignore[method-assign]
    with pytest.raises(SpendLimitError):
        repo.record("embed", "embedding", "model", 10)
    repo._session.add.assert_not_called()
