"""Job cancel helper."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from org_memory.db.repositories.jobs import JobRepository


def test_cancel_pending_job() -> None:
    job = SimpleNamespace(status="pending", locked_until="x", updated_at=None)
    # Bypass __init__ DB settings by calling unbound method logic via instance mock.
    repo = object.__new__(JobRepository)
    repo.cancel(job)
    assert job.status == "cancelled"
    assert job.locked_until is None


def test_cancel_rejects_done() -> None:
    job = SimpleNamespace(status="done", locked_until=None, updated_at=None)
    repo = object.__new__(JobRepository)
    with pytest.raises(ValueError, match="cannot cancel"):
        repo.cancel(job)
