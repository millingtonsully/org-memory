"""Ground extracted time fields against document event_time (t_ref)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from org_memory.services.temporality.grain import normalize_grain, shift_months
from org_memory.services.temporality.types import GroundedInterval, TimeGrain

_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)
_WEEKS_AGO = re.compile(r"\b(\d+)\s+weeks?\s+ago\b", re.I)
_DAYS_AGO = re.compile(r"\b(\d+)\s+days?\s+ago\b", re.I)
_MONTHS_AGO = re.compile(r"\b(\d+)\s+months?\s+ago\b", re.I)
_YEARS_AGO = re.compile(r"\b(\d+)\s+years?\s+ago\b", re.I)
_YESTERDAY = re.compile(r"\byesterday\b", re.I)
_TODAY = re.compile(r"\btoday\b", re.I)
_LAST_WEEK = re.compile(r"\blast\s+week\b", re.I)
_LAST_MONTH = re.compile(r"\blast\s+month\b", re.I)
_LAST_YEAR = re.compile(r"\blast\s+year\b", re.I)
_THIS_MONTH = re.compile(r"\bthis\s+month\b", re.I)
_THIS_YEAR = re.compile(r"\bthis\s+year\b", re.I)
_LAST_SUMMER = re.compile(r"\blast\s+summer\b", re.I)
_LAST_WINTER = re.compile(r"\blast\s+winter\b", re.I)
_A_FEW_WEEKS = re.compile(r"\ba\s+few\s+weeks?\s+ago\b", re.I)


def ground_fact_times(
    item: dict[str, Any],
    *,
    t_ref: datetime,
) -> GroundedInterval | None:
    """Build a world-time window from extractor fields and document event_time.

    Returns None when the window is contradictory (valid_from > valid_to).
    """
    if t_ref.tzinfo is None:
        t_ref = t_ref.replace(tzinfo=UTC)

    expression = str(item.get("time_expression") or "").strip()
    grain = normalize_grain(item.get("time_grain"))
    valid_from = _parse_instant(item.get("valid_from"))
    valid_to = _parse_instant(item.get("valid_to"))

    if valid_from is None and expression:
        resolved = _resolve_relative(expression, t_ref)
        if resolved is not None:
            valid_from, rel_grain = resolved
            if grain == "unknown":
                grain = rel_grain

    if valid_from is None:
        valid_from = t_ref
        if grain == "unknown":
            grain = "day"

    if valid_to is not None and valid_from is not None and valid_from > valid_to:
        return None

    return GroundedInterval(
        valid_from=valid_from,
        valid_to=valid_to,
        time_grain=grain,
        time_expression=expression,
    )


def _parse_instant(raw: Any) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    text = str(raw).strip()
    if not text or not _ISO_RE.match(text):
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _resolve_relative(
    expression: str, t_ref: datetime
) -> tuple[datetime, TimeGrain] | None:
    if _A_FEW_WEEKS.search(expression):
        return t_ref - __import__("datetime").timedelta(weeks=3), "day"
    match = _WEEKS_AGO.search(expression)
    if match:
        weeks = int(match.group(1))
        return t_ref - __import__("datetime").timedelta(weeks=weeks), "day"
    match = _DAYS_AGO.search(expression)
    if match:
        days = int(match.group(1))
        return t_ref - __import__("datetime").timedelta(days=days), "day"
    match = _MONTHS_AGO.search(expression)
    if match:
        months = int(match.group(1))
        return shift_months(t_ref, -months), "month"
    match = _YEARS_AGO.search(expression)
    if match:
        years = int(match.group(1))
        return shift_months(t_ref, -12 * years), "year"
    if _YESTERDAY.search(expression):
        return t_ref - __import__("datetime").timedelta(days=1), "day"
    if _TODAY.search(expression):
        return t_ref, "day"
    if _LAST_WEEK.search(expression):
        return t_ref - __import__("datetime").timedelta(weeks=1), "day"
    if _LAST_MONTH.search(expression):
        return shift_months(t_ref, -1), "month"
    if _LAST_YEAR.search(expression):
        return shift_months(t_ref, -12), "year"
    if _THIS_MONTH.search(expression):
        return datetime(t_ref.year, t_ref.month, 1, tzinfo=UTC), "month"
    if _THIS_YEAR.search(expression):
        return datetime(t_ref.year, 1, 1, tzinfo=UTC), "year"
    if _LAST_SUMMER.search(expression):
        year = t_ref.year if t_ref.month >= 9 else t_ref.year - 1
        return datetime(year, 7, 15, tzinfo=UTC), "month"
    if _LAST_WINTER.search(expression):
        # Northern-hemisphere winter mid-point: prior Jan 15 relative to t_ref.
        year = t_ref.year if t_ref.month >= 3 else t_ref.year - 1
        return datetime(year, 1, 15, tzinfo=UTC), "month"
    return None
