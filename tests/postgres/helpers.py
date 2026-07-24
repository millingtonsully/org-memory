"""Helpers for hermetic Postgres tests."""

from __future__ import annotations

from datetime import datetime


def make_doc(
    *,
    doc_id: str,
    workspace_id: str,
    org_visible: bool,
    allowed_principals: list[str],
    event_time: datetime,
    rendered_text: str | None = None,
):
    from org_memory.db.orm import Document

    return Document(
        doc_id=doc_id,
        workspace_id=workspace_id,
        source_system="test",
        external_id=doc_id.split(":", 1)[-1],
        source_type="test_doc",
        title=doc_id,
        rendered_text=rendered_text or f"body of {doc_id}",
        event_time=event_time,
        org_visible=org_visible,
        allowed_principals=allowed_principals,
        acl_event_time=event_time,
    )
