"""Helpers shared by job handlers: spend gating and structured LLM output parsing."""

from __future__ import annotations

import json

from org_memory.core.errors import VendorAPIError
from org_memory.db.engine import session_scope
from org_memory.db.repositories import SpendRepository


def assert_spend_under_hard_limit() -> None:
    """Enforce the monthly cap in a short-lived transaction.

    ``pg_advisory_xact_lock`` is held until commit. Calling it on the long-lived
    job session, then recording spend in a nested ``session_scope``, deadlocks
    the worker against itself (statement timeout means the job never commits as
    running). A dedicated short transaction avoids that.
    """
    with session_scope() as spend_session:
        SpendRepository(spend_session).assert_under_hard_limit()


def parse_llm_json(vendor: str, raw: str) -> dict:
    """Parse a JSON object from an LLM completion, tolerating a ```json fence.

    Raises ``VendorAPIError`` with the raw response attached when the output is
    unparseable, so the job queue records the real vendor output for debugging
    instead of a bare JSONDecodeError.
    """
    try:
        parsed = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
    except json.JSONDecodeError as exc:
        raise VendorAPIError(
            vendor,
            200,
            "model returned non-JSON output",
            raw_response=raw,
        ) from exc
    if not isinstance(parsed, dict):
        raise VendorAPIError(
            vendor,
            200,
            "model returned JSON that is not an object",
            raw_response=raw,
        )
    return parsed
