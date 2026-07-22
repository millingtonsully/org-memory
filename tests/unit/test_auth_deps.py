"""Auth dependency behavior."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from tests.conftest import apply_minimal_settings_env


@pytest.fixture()
def auth_settings(monkeypatch):
    apply_minimal_settings_env(monkeypatch, workspace_id="ws-auth")
    from org_memory.core.settings import get_settings

    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def test_require_api_key_rejects_wrong_key(auth_settings) -> None:
    from org_memory.api.deps import require_api_key

    with pytest.raises(HTTPException) as exc:
        require_api_key(x_api_key="wrong")
    assert exc.value.status_code == 401


def test_require_api_key_accepts_exact_key(auth_settings) -> None:
    from org_memory.api.deps import require_api_key

    require_api_key(x_api_key="test-key")


def test_bind_principal_requires_header(auth_settings) -> None:
    from org_memory.api.deps import bind_principal

    with pytest.raises(HTTPException) as exc:
        bind_principal(x_principal_id="", x_principal_groups="")
    assert exc.value.status_code == 400


def test_bind_admin_requires_role(auth_settings) -> None:
    from org_memory.api.deps import bind_admin
    from org_memory.domain.models import Principal

    principal = Principal(principal_id="user:11111111-1111-1111-1111-111111111111")
    with pytest.raises(HTTPException) as exc:
        bind_admin(principal=principal, x_principal_roles="")
    assert exc.value.status_code == 403
