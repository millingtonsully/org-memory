"""Job heartbeat commits lease extension on a separate connection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from org_memory.db.repositories.jobs import JobRepository


def test_heartbeat_updates_lease_via_committed_connection() -> None:
    repo = object.__new__(JobRepository)
    repo._session = MagicMock()
    job = SimpleNamespace(job_id="job-1", locked_until=None, updated_at=None, status="running")
    conn = MagicMock()
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    engine.connect.return_value.__exit__.return_value = None
    with (
        patch("org_memory.db.engine.get_engine", return_value=engine),
        patch("org_memory.db.repositories.jobs.utcnow", return_value=datetime(2026, 7, 1, tzinfo=UTC)),
    ):
        repo.heartbeat(job, lease_seconds=60)
    conn.execute.assert_called_once()
    conn.commit.assert_called_once()
    assert job.locked_until == datetime(2026, 7, 1, tzinfo=UTC) + timedelta(seconds=60)
