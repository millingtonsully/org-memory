"""Grain-aware world-time matching (pure helpers + SQL fragments)."""

from __future__ import annotations

import calendar
from datetime import UTC, datetime
from typing import Any

from org_memory.services.temporality.types import TimeGrain

_GRAINS: set[str] = {"day", "month", "quarter", "year", "unknown"}
_GRAIN_CHOICES = "day, month, quarter, year, unknown"


def normalize_grain(raw: Any) -> TimeGrain:
    """Coerce a stored/inferred grain to a known value (unknown if unrecognized).

    Use ``parse_host_as_of_grain`` for host/API ``as_of_grain`` (fail closed).
    """
    value = str(raw or "unknown").strip().lower()
    if value in _GRAINS:
        return value  # type: ignore[return-value]
    return "unknown"


def parse_host_as_of_grain(raw: Any) -> TimeGrain | None:
    """Parse host ``as_of_grain``; omit/blank → None; invalid → ValueError."""
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if not value:
        return None
    if value not in _GRAINS:
        raise ValueError(
            f"as_of_grain must be one of: {_GRAIN_CHOICES} (got {raw!r})"
        )
    return value  # type: ignore[return-value]


def expand_valid_from(
    valid_from: datetime | None, grain: TimeGrain | str | None
) -> datetime | None:
    """Push a stored valid_from down to the start of its declared grain."""
    if valid_from is None:
        return None
    instant = valid_from if valid_from.tzinfo else valid_from.replace(tzinfo=UTC)
    g = normalize_grain(grain)
    if g == "month":
        return datetime(instant.year, instant.month, 1, tzinfo=UTC)
    if g == "quarter":
        month = ((instant.month - 1) // 3) * 3 + 1
        return datetime(instant.year, month, 1, tzinfo=UTC)
    if g == "year":
        return datetime(instant.year, 1, 1, tzinfo=UTC)
    return instant


def query_bucket(
    as_of: datetime, grain: TimeGrain | str | None
) -> tuple[datetime, datetime] | None:
    """Return half-open [start, end) for a coarse query grain; None for point."""
    instant = as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)
    g = normalize_grain(grain)
    if g == "month":
        start = datetime(instant.year, instant.month, 1, tzinfo=UTC)
        if instant.month == 12:
            end = datetime(instant.year + 1, 1, 1, tzinfo=UTC)
        else:
            end = datetime(instant.year, instant.month + 1, 1, tzinfo=UTC)
        return start, end
    if g == "quarter":
        month = ((instant.month - 1) // 3) * 3 + 1
        start = datetime(instant.year, month, 1, tzinfo=UTC)
        end_month = month + 3
        if end_month > 12:
            end = datetime(instant.year + 1, end_month - 12, 1, tzinfo=UTC)
        else:
            end = datetime(instant.year, end_month, 1, tzinfo=UTC)
        return start, end
    if g == "year":
        start = datetime(instant.year, 1, 1, tzinfo=UTC)
        end = datetime(instant.year + 1, 1, 1, tzinfo=UTC)
        return start, end
    return None


def fact_matches_as_of(
    *,
    valid_from: datetime | None,
    valid_to: datetime | None,
    fact_grain: TimeGrain | str | None,
    as_of: datetime,
    query_grain: TimeGrain | str | None = "unknown",
) -> bool:
    """True when the fact's grain-expanded window meets the query as_of semantics."""
    fact_from = expand_valid_from(valid_from, fact_grain)
    bucket = query_bucket(as_of, query_grain)
    if bucket is None:
        point = as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)
        if fact_from is not None and fact_from > point:
            return False
        return valid_to is None or valid_to > point
    start, end = bucket
    if fact_from is not None and fact_from >= end:
        return False
    return valid_to is None or valid_to > start


def resolve_validity_query_point(
    *,
    as_of: datetime | None,
    believed_as_of: datetime | None,
    as_of_grain: str | None,
    now: datetime,
) -> tuple[datetime, TimeGrain]:
    """World-time point + grain used for validity matching.

    - Host ``as_of`` wins for the world clock (joint belief+world still uses it).
    - Belief-only (``believed_as_of`` without ``as_of``) uses the belief instant as
      the world point so the read reconstructs what would have been current then.
    - Both omitted → ``now`` with day grain (unless host supplied ``as_of_grain``).
    """
    if as_of is not None:
        point = as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)
        return point, normalize_grain(as_of_grain)
    if believed_as_of is not None:
        point = (
            believed_as_of
            if believed_as_of.tzinfo
            else believed_as_of.replace(tzinfo=UTC)
        )
        grain: TimeGrain = (
            "day" if as_of_grain is None else normalize_grain(as_of_grain)
        )
        return point, grain
    clock = now if now.tzinfo else now.replace(tzinfo=UTC)
    grain = "day" if as_of_grain is None else normalize_grain(as_of_grain)
    return clock, grain


def temporal_read_statuses(
    as_of: datetime | None,
    believed_as_of: datetime | None,
) -> list[str]:
    """Statuses eligible for a temporal point read vs a current read.

    Point reads (world or belief) include superseded rows whose windows still
    contain the point. Current reads return active rows only.
    """
    if as_of is not None or believed_as_of is not None:
        return ["active", "superseded"]
    return ["active"]


def belief_as_of_sql(alias: str) -> str:
    """SQL belief-axis predicate using bind ``:believed_as_of`` (NULL = open)."""
    return f"""
    (CAST(:believed_as_of AS timestamptz) IS NULL
     OR ({alias}.recorded_at <= :believed_as_of
         AND ({alias}.invalidated_at IS NULL
              OR {alias}.invalidated_at > :believed_as_of)))
    """


def validity_as_of_sql(alias: str) -> str:
    """SQL predicate using binds ``:as_of`` and ``:as_of_grain``.

    Expands the row's ``valid_from`` by its ``time_grain``. When
    ``as_of_grain`` is month/quarter/year, uses overlap with that calendar
    bucket; otherwise uses half-open point containment.
    """
    af = f"{alias}.valid_from"
    at = f"{alias}.valid_to"
    ag = f"COALESCE({alias}.time_grain, 'unknown')"
    fact_from = (
        f"(CASE {ag} "
        f"WHEN 'month' THEN date_trunc('month', {af}) "
        f"WHEN 'quarter' THEN date_trunc('quarter', {af}) "
        f"WHEN 'year' THEN date_trunc('year', {af}) "
        f"ELSE {af} END)"
    )
    return f"""
    (CAST(:as_of AS timestamptz) IS NULL
     OR (
       CASE COALESCE(CAST(:as_of_grain AS text), 'unknown')
         WHEN 'month' THEN
           ({fact_from} IS NULL
            OR {fact_from} < date_trunc('month', CAST(:as_of AS timestamptz))
                            + interval '1 month')
           AND ({at} IS NULL
                OR {at} > date_trunc('month', CAST(:as_of AS timestamptz)))
         WHEN 'quarter' THEN
           ({fact_from} IS NULL
            OR {fact_from} < date_trunc('quarter', CAST(:as_of AS timestamptz))
                            + interval '3 months')
           AND ({at} IS NULL
                OR {at} > date_trunc('quarter', CAST(:as_of AS timestamptz)))
         WHEN 'year' THEN
           ({fact_from} IS NULL
            OR {fact_from} < date_trunc('year', CAST(:as_of AS timestamptz))
                            + interval '1 year')
           AND ({at} IS NULL
                OR {at} > date_trunc('year', CAST(:as_of AS timestamptz)))
         ELSE
           ({fact_from} IS NULL OR {fact_from} <= CAST(:as_of AS timestamptz))
           AND ({at} IS NULL OR {at} > CAST(:as_of AS timestamptz))
       END
     ))
    """


def month_end_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def shift_months(instant: datetime, delta: int) -> datetime:
    """Shift calendar months, clamping the day to the target month length."""
    month_index = instant.month - 1 + delta
    year = instant.year + month_index // 12
    month = month_index % 12 + 1
    day = min(instant.day, month_end_day(year, month))
    return datetime(
        year,
        month,
        day,
        instant.hour,
        instant.minute,
        instant.second,
        instant.microsecond,
        tzinfo=instant.tzinfo or UTC,
    )
