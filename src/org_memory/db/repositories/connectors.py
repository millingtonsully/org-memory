"""Connector ingest freshness and failure surfacing."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from org_memory.core.settings import get_settings
from org_memory.db.orm import ConnectorStatus, utcnow

_RECENT_ERROR_CAP = 20


class ConnectorStatusRepository:
    def __init__(self, session: Session):
        self._session = session
        self._ws = get_settings().workspace_id

    def _get_or_create(self, source_system: str) -> ConnectorStatus:
        status = self._session.get(ConnectorStatus, (self._ws, source_system))
        if status is None:
            status = ConnectorStatus(
                workspace_id=self._ws,
                source_system=source_system,
                envelopes_total=0,
                failures_total=0,
                last_error="",
                recent_errors=[],
            )
            self._session.add(status)
        return status

    def record_envelope(self, source_system: str, event_time: datetime) -> None:
        status = self._get_or_create(source_system)
        status.last_envelope_at = utcnow()
        status.last_event_time = event_time
        status.envelopes_total = int(status.envelopes_total or 0) + 1
        status.updated_at = utcnow()

    def record_failure(self, source_system: str, error: str) -> None:
        status = self._get_or_create(source_system)
        now = utcnow()
        status.failures_total = int(status.failures_total or 0) + 1
        status.last_error = error[:4000]
        status.last_failure_at = now
        sample = {"at": now.isoformat(), "error": error[:1000]}
        recent = list(status.recent_errors or [])
        recent.append(sample)
        status.recent_errors = recent[-_RECENT_ERROR_CAP:]
        status.updated_at = now

    def all_statuses(self) -> list[ConnectorStatus]:
        return (
            self._session.query(ConnectorStatus)
            .filter(ConnectorStatus.workspace_id == self._ws)
            .order_by(ConnectorStatus.source_system)
            .all()
        )
