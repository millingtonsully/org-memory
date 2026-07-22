from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)

# Process-local registry so tests can isolate if needed.
REGISTRY = CollectorRegistry()

INGEST_OK = Counter(
    "org_memory_ingest_ok_total",
    "Successful ChangeEnvelope ingests",
    registry=REGISTRY,
)
INGEST_FAIL = Counter(
    "org_memory_ingest_fail_total",
    "Failed ChangeEnvelope ingests",
    registry=REGISTRY,
)
VENDOR_ERRORS = Counter(
    "org_memory_vendor_errors_total",
    "Upstream vendor API errors",
    ["vendor"],
    registry=REGISTRY,
)
JOBS_BY_STATUS = Gauge(
    "org_memory_jobs",
    "Job counts by status and type",
    ["status", "job_type"],
    registry=REGISTRY,
)
EMBED_BACKLOG = Gauge(
    "org_memory_embed_backlog_chunks",
    "Chunks with null embedding and not deleted",
    registry=REGISTRY,
)
SPEND_TOKENS_MONTH = Gauge(
    "org_memory_spend_tokens_month",
    "Spend ledger tokens this calendar month",
    registry=REGISTRY,
)
SPEND_ALERT = Gauge(
    "org_memory_spend_alert",
    "1 when monthly tokens exceed SPEND_ALERT_TOKENS_MONTHLY",
    registry=REGISTRY,
)
SPEND_HARD_LIMIT_HIT = Gauge(
    "org_memory_spend_hard_limit_hit",
    "1 when monthly tokens meet or exceed SPEND_HARD_LIMIT_TOKENS_MONTHLY",
    registry=REGISTRY,
)
RETENTION_UNSET = Gauge(
    "org_memory_retention_unset",
    "1 when RETENTION_DAYS is 0 (no automatic purge)",
    registry=REGISTRY,
)
WORKER_HEARTBEAT = Gauge(
    "org_memory_worker_heartbeat_unixtime",
    "Unix timestamp of last successful worker poll loop",
    registry=REGISTRY,
)


def metrics_payload() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
