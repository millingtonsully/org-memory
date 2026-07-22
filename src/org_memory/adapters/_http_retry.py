"""Shared retry for outbound vendor HTTP calls.

Retries only on transient, retry-safe statuses (429 Too Many Requests, 503
Service Unavailable), honoring a capped retry-after hint. Everything else
-- auth failures, other 4xx, schema errors -- is returned unchanged so it fails
fast and honestly. No fallback currently.
"""

from __future__ import annotations

import random
import time

import httpx

from org_memory.core.errors import VendorAPIError

_RETRYABLE_STATUSES = frozenset({429, 503})
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 0.5
# Cap per-attempt wait so a synchronous request path (query embed + rerank) isn't waiting on a hostile retry-after.  # noqa: E501
_BACKOFF_CAP_SECONDS = 8.0


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        # HTTP-date form is not honored; fall back to computed backoff.
        return None


def _sleep_seconds(response: httpx.Response, attempt: int) -> float:
    hinted = _retry_after_seconds(response)
    if hinted is not None:
        return min(hinted, _BACKOFF_CAP_SECONDS)
    backoff = min(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), _BACKOFF_CAP_SECONDS)
    # Jitter avoids many workers retrying in lockstep after a shared 429.
    return backoff + random.uniform(0.0, _BACKOFF_BASE_SECONDS)


def post_with_retry(
    client: httpx.Client,
    url: str,
    *,
    json: dict,
    vendor: str,
) -> httpx.Response:
    """POST with bounded retries on 429/503. Transport failures fail fast.

    Returns the final response (which the caller still validates for status and
    schema). Retries are exhausted after _MAX_ATTEMPTS; the last response is
    returned so the caller raises its normal VendorAPIError.
    """
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = client.post(url, json=json)
        except httpx.HTTPError as exc:
            raise VendorAPIError(vendor, None, f"transport failure: {exc}") from exc
        if response.status_code not in _RETRYABLE_STATUSES or attempt == _MAX_ATTEMPTS:
            return response
        time.sleep(_sleep_seconds(response, attempt))
    raise AssertionError("unreachable: loop always returns on the final attempt")
