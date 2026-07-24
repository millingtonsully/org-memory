"""App settings loaded from the environment.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from org_memory.core.errors import ConfigurationError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # One deployment serves one workspace (strong tenant isolation).
    workspace_id: str

    # Postgres. Same code against any Postgres host; only the URL changes.
    # Must use the psycopg3 scheme: postgresql+psycopg://...
    database_url: str

    # Object storage. OBJECT_STORE_BACKEND selects the provider; only the
    # selected provider's credentials are required. Providers are peers: neither
    # is a fallback for the other.
    object_store_backend: Literal["supabase", "s3"] = "supabase"

    # Supabase Storage. Uses the project's API gateway URL + service_role key.
    supabase_project_url: str = ""
    supabase_service_role_key: str = ""
    supabase_storage_bucket: str = "org-memory-blobs"

    # Amazon S3 (or S3-compatible: MinIO, Cloudflare R2). AWS credentials come
    # from the standard AWS provider chain (env vars, shared config, IAM role),
    # never from these app settings.
    s3_bucket_name: str = ""
    aws_region: str = ""
    s3_endpoint_url: str = ""  # S3-compatible stores (MinIO, R2); leave blank for AWS S3
    # Path-style addressing. When S3_ENDPOINT_URL is set, boot forces this True
    # (virtual-hosted breaks most compat endpoints). For AWS S3 leave False.
    s3_force_path_style: bool = False
    # Server-side encryption on every put. AES256 always; aws:kms needs a CMK.
    s3_sse: Literal["AES256", "aws:kms"] = "AES256"
    s3_sse_kms_key_id: str = ""
    # Objects at or above this size use multipart upload via boto3 TransferManager.
    s3_multipart_threshold_bytes: int = 8_388_608

    # Embeddings: any OpenAI-compatible /embeddings endpoint.
    embedding_api_key: str
    embedding_api_url: str = "https://api.openai.com/v1"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536

    # Chat / synthesis: any OpenAI-compatible /chat/completions endpoint.
    synthesis_api_key: str = ""
    synthesis_api_url: str = "https://api.openai.com/v1"
    synthesis_model: str = "gpt-4o-mini"

    # Rerank: any Voyage-compatible /rerank endpoint.
    rerank_api_key: str
    rerank_api_url: str = "https://api.voyageai.com/v1"
    rerank_model: str = "rerank-2.5"

    service_api_key: str

    procedural_max_source_chars: int = 24_000

    fact_activation_confidence: float = 0.8
    # Identity auto-merge policy (intentional, do not loosen casually):
    # 1. Embeddings only propose a small candidate set.
    # 2. An LLM adjudication may say "same" with a confidence score.
    # 3. Auto-merge only if confidence >= identity_merge_confidence AND
    #    corroboration includes both a shared normalized name and a shared
    #    email address (see has_sufficient_corroboration).
    # Name similarity or LLM confidence alone must never auto-merge. That
    # blocks false merges of different people with the same display name.
    # Unsure / same-without-email cases stay as decisions for review, not merges.
    identity_candidate_similarity: float = 0.88
    identity_candidate_limit: int = 3
    identity_merge_confidence: float = 0.95

    retention_days: int

    # When monthly spend tokens exceed this value, /metrics and admin health
    # flag an alert and operators get an error log.
    spend_alert_tokens_monthly: int

    # Hard stop for new spend-incurring work when monthly tokens reach this value.
    # Must be >= spend_alert_tokens_monthly. Operators set the number explicitly.
    spend_hard_limit_tokens_monthly: int

    rrf_k: int = 60
    rerank_candidates: int = 100
    collaboration_rebuild_debounce_seconds: int = 60

    # Closed taxonomy schema (YAML directory). Boot fails if missing/invalid.
    taxonomy_registry_dir: str = "config/taxonomy_registry"

    # taxonomy_proposals delivery: blank = caller pulls
    # GET /v1/taxonomy-proposals; non-blank = also POST each pending row here.
    taxonomy_proposal_webhook_url: str = ""
    # Optional HMAC-SHA256 secret for X-Org-Memory-Signature: sha256=<hex>.
    taxonomy_proposal_webhook_secret: str = ""

    @field_validator("database_url")
    @classmethod
    def _require_psycopg_scheme(cls, v: str) -> str:
        if v and not v.startswith("postgresql+psycopg://"):
            raise ValueError(
                "DATABASE_URL must start with 'postgresql+psycopg://' "
                "(psycopg3 driver). If your host gives postgresql://..., "
                "replace the scheme with postgresql+psycopg://."
            )
        return v

    @field_validator(
        "workspace_id",
        "embedding_api_key",
        "rerank_api_key",
        "rerank_model",
        "service_api_key",
    )
    @classmethod
    def _require_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("required setting must not be empty")
        return value

    @field_validator(
        "fact_activation_confidence",
        "identity_candidate_similarity",
        "identity_merge_confidence",
    )
    @classmethod
    def _require_unit_interval(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("confidence and similarity settings must be between 0 and 1")
        return value

    @field_validator(
        "procedural_max_source_chars",
        "rerank_candidates",
        "spend_alert_tokens_monthly",
        "spend_hard_limit_tokens_monthly",
        "collaboration_rebuild_debounce_seconds",
    )
    @classmethod
    def _require_positive_integer(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("candidate and size limits must be positive")
        return value

    @field_validator("retention_days")
    @classmethod
    def _require_nonnegative_retention(cls, value: int) -> int:
        if value < 0:
            raise ValueError("RETENTION_DAYS must be >= 0 (0 = no automatic purge)")
        return value

    @model_validator(mode="after")
    def _require_backend_credentials(self) -> Settings:
        """Fail at startup if the chosen backend is missing its credentials."""
        # If synthesis key is omitted, reuse the embedding key (common when
        # both hit the same OpenAI-compatible host).
        if not self.synthesis_api_key:
            self.synthesis_api_key = self.embedding_api_key

        if self.spend_hard_limit_tokens_monthly < self.spend_alert_tokens_monthly:
            raise ValueError(
                "SPEND_HARD_LIMIT_TOKENS_MONTHLY must be >= SPEND_ALERT_TOKENS_MONTHLY"
            )

        if self.object_store_backend == "supabase" and not (
            self.supabase_project_url and self.supabase_service_role_key
        ):
            raise ValueError(
                "OBJECT_STORE_BACKEND=supabase requires SUPABASE_PROJECT_URL and "
                "SUPABASE_SERVICE_ROLE_KEY to be set."
            )
        if self.object_store_backend == "s3" and not (self.s3_bucket_name and self.aws_region):
            raise ValueError(
                "OBJECT_STORE_BACKEND=s3 requires S3_BUCKET_NAME and AWS_REGION to be set."
            )
        if self.object_store_backend == "s3" and self.s3_endpoint_url:
            if not (
                self.s3_endpoint_url.startswith("http://")
                or self.s3_endpoint_url.startswith("https://")
            ):
                raise ValueError(
                    "S3_ENDPOINT_URL must start with http:// or https:// "
                    "(required for MinIO, R2, and other S3-compatible endpoints)."
                )
            # Compat endpoints need path-style; do not leave this as a soft toggle.
            self.s3_force_path_style = True
        if self.object_store_backend == "s3" and self.s3_sse == "aws:kms" and not self.s3_sse_kms_key_id:
            raise ValueError(
                "S3_SSE=aws:kms requires S3_SSE_KMS_KEY_ID (CMK id or ARN)."
            )
        if self.s3_multipart_threshold_bytes <= 0:
            raise ValueError("S3_MULTIPART_THRESHOLD_BYTES must be positive")
        return self


@lru_cache
def get_settings() -> Settings:
    """Load settings once per process."""
    try:
        return Settings()  # type: ignore[call-arg]
    except Exception as exc:
        raise ConfigurationError(f"Configuration is incomplete or invalid. Details: {exc}") from exc
