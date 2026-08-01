"""Rule-based temporal intent: query text → TemporalQueryPlan."""

from __future__ import annotations

import calendar
import re
from datetime import UTC, datetime

from org_memory.services.temporality.types import TemporalQueryPlan, TimeGrain

_BELIEF_RE = re.compile(
    r"\b("
    r"what did we (think|believe|report|know)"
    r"|before the correction"
    r"|as of our records"
    r"|according to (the )?(old |previous )?(wiki|records|system)"
    r"|what (was|were) (we|the system) (showing|reporting)"
    r")\b",
    re.I,
)

_MONTH_RE = re.compile(
    r"\bin (?P<month>january|february|march|april|may|june|july|"
    r"august|september|october|november|december)"
    r"(?:\s+(?P<year>20\d{2}))?\b",
    re.I,
)
_YEAR_RE = re.compile(r"\bin (?P<year>20\d{2})\b", re.I)
_QUARTER_RE = re.compile(
    r"\bin q(?P<quarter>[1-4])(?:\s+(?P<qyear>20\d{2}))?\b", re.I
)
_AS_OF_RE = re.compile(r"\bas of\b", re.I)
_DURING_RE = re.compile(
    r"\b(during (the )?(reorg|reorganization|transition)|when (she|he|they) was)\b",
    re.I,
)
_CURRENT_RE = re.compile(r"\b(now|current|currently|today|who is|what is)\b", re.I)
_BETWEEN_RE = re.compile(
    r"\b(what\s+changed|changes?|difference|diff)\b.*\bbetween\b",
    re.I,
)
_BETWEEN_SPLIT_RE = re.compile(r"\bbetween\b(.+?)\band\b(.+)$", re.I)

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def plan_temporal_query(
    query: str,
    *,
    now: datetime | None = None,
) -> TemporalQueryPlan:
    """Map natural language to a temporal plan, or mark ambiguity.

    Deterministic rules only. Explicit API timestamps override this plan at
    the compose layer.
    """
    text = (query or "").strip()
    clock = now or datetime.now(UTC)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=UTC)

    if not text:
        return TemporalQueryPlan(
            axis="current",
            status="ok",
            confidence=1.0,
            rationale="empty_query_defaults_current",
        )

    has_belief = bool(_BELIEF_RE.search(text))
    point, grain = _extract_calendar_point(text, clock)
    has_calendar = point is not None
    has_world_soft = bool(_AS_OF_RE.search(text) or _DURING_RE.search(text))
    has_current = bool(_CURRENT_RE.search(text))
    has_between = bool(_BETWEEN_RE.search(text))

    if has_between:
        pair = _extract_between_points(text, clock)
        if pair is None:
            return TemporalQueryPlan(
                axis="belief" if has_belief else "world",
                status="ambiguous",
                confidence=0.25,
                rationale="between_without_two_resolvable_points",
            )
        start, end, pair_grain = pair
        if has_belief:
            return TemporalQueryPlan(
                axis="belief",
                believed_as_of=start,
                range_end=end,
                grain=pair_grain,
                confidence=0.85,
                status="ok",
                rationale="belief_snapshot_diff",
            )
        return TemporalQueryPlan(
            axis="world",
            as_of=start,
            range_end=end,
            grain=pair_grain,
            confidence=0.9,
            status="ok",
            rationale="world_snapshot_diff",
        )

    if has_belief and has_world_soft and not has_calendar:
        # e.g. belief cue + "as of" without a resolvable date
        return TemporalQueryPlan(
            axis="belief",
            status="ambiguous",
            confidence=0.2,
            rationale="belief_without_time_point",
        )

    if has_belief and has_calendar:
        return TemporalQueryPlan(
            axis="belief",
            believed_as_of=point,
            grain=grain,
            confidence=0.85,
            status="ok",
            rationale="belief_axis_with_point",
        )

    if has_belief:
        return TemporalQueryPlan(
            axis="belief",
            status="ambiguous",
            confidence=0.2,
            rationale="belief_without_time_point",
        )

    if has_calendar:
        return TemporalQueryPlan(
            axis="world",
            as_of=point,
            grain=grain,
            confidence=0.9,
            status="ok",
            rationale="world_axis_calendar",
        )

    if has_world_soft:
        return TemporalQueryPlan(
            axis="world",
            status="ambiguous",
            confidence=0.3,
            rationale="world_cue_without_resolvable_point",
        )

    return TemporalQueryPlan(
        axis="current",
        status="ok",
        confidence=0.95 if has_current else 0.8,
        rationale="current_or_default",
    )


def _extract_between_points(
    text: str, clock: datetime
) -> tuple[datetime, datetime, TimeGrain] | None:
    split = _BETWEEN_SPLIT_RE.search(text)
    if not split:
        return None
    left = split.group(1).strip()
    right = split.group(2).strip()
    # Prefer an explicit year on the right for a bare month on the left.
    right_point, right_grain = _extract_calendar_point(right, clock)
    if right_point is None:
        return None
    left_clock = right_point
    left_point, left_grain = _extract_calendar_point(left, left_clock)
    if left_point is None:
        return None
    start, end = left_point, right_point
    if start > end:
        start, end = end, start
    if start == end:
        return None
    grain: TimeGrain = left_grain if left_grain == right_grain else "unknown"
    return start, end, grain


def _extract_calendar_point(
    text: str, clock: datetime
) -> tuple[datetime | None, TimeGrain]:
    match = _QUARTER_RE.search(text)
    if match:
        year = int(match.group("qyear") or clock.year)
        quarter = int(match.group("quarter"))
        month = (quarter - 1) * 3 + 2  # mid-quarter month
        return datetime(year, month, 15, tzinfo=UTC), "quarter"

    match = _MONTH_RE.search(text)
    if match:
        month = _MONTHS[match.group("month").lower()]
        year = int(match.group("year") or clock.year)
        last_day = calendar.monthrange(year, month)[1]
        return datetime(year, month, min(15, last_day), tzinfo=UTC), "month"

    # Bare month name (no leading "in") for between-clauses.
    bare = re.search(
        r"\b(?P<month>january|february|march|april|may|june|july|"
        r"august|september|october|november|december)"
        r"(?:\s+(?P<year>20\d{2}))?\b",
        text,
        re.I,
    )
    if bare:
        month = _MONTHS[bare.group("month").lower()]
        year = int(bare.group("year") or clock.year)
        last_day = calendar.monthrange(year, month)[1]
        return datetime(year, month, min(15, last_day), tzinfo=UTC), "month"

    match = _YEAR_RE.search(text)
    if match:
        year = int(match.group("year"))
        return datetime(year, 7, 1, tzinfo=UTC), "year"

    year_bare = re.search(r"\b(?P<year>20\d{2})\b", text)
    if year_bare and not bare:
        year = int(year_bare.group("year"))
        return datetime(year, 7, 1, tzinfo=UTC), "year"

    return None, "unknown"
