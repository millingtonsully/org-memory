"""Principals and ChangeEnvelope validation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from org_memory.domain.models import ChangeEnvelope, ChangeKind, Principal
from org_memory.domain.principals import is_valid_principal, require_principal

USER_A = "user:11111111-1111-1111-1111-111111111111"
GROUP_B = "group:22222222-2222-2222-2222-222222222222"
_EVENT = datetime(2026, 7, 1, tzinfo=UTC)


def test_is_valid_principal_accepts_user_and_group_uuids() -> None:
    assert is_valid_principal(USER_A)
    assert is_valid_principal(GROUP_B)
    assert not is_valid_principal("alice")


def test_require_principal_rejects_bad_forms() -> None:
    with pytest.raises(ValueError, match="user:<uuid>"):
        require_principal("group:xyz")


def test_org_visible_false_requires_principals() -> None:
    with pytest.raises(ValidationError, match="allowed_principals"):
        ChangeEnvelope(
            source_system="test",
            external_id="1",
            change_kind=ChangeKind.create,
            source_type="doc",
            event_time=_EVENT,
            org_visible=False,
            allowed_principals=[],
        )


def test_delete_exempt_from_principal_requirement() -> None:
    envelope = ChangeEnvelope(
        source_system="test",
        external_id="1",
        change_kind=ChangeKind.delete,
        source_type="doc",
        event_time=_EVENT,
        org_visible=False,
        allowed_principals=[],
    )
    assert envelope.change_kind is ChangeKind.delete


def test_principal_model() -> None:
    p = Principal(principal_id=USER_A, groups=[GROUP_B])
    assert USER_A in p.all_principals()
    assert GROUP_B in p.all_principals()
