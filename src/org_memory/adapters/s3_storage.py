"""Amazon S3 object store.

Required: S3_BUCKET_NAME, AWS_REGION, and boto3 (`pip install '.[s3]'` or
`.[dev]`). Every put uses server-side encryption (default AES256; aws:kms
requires S3_SSE_KMS_KEY_ID). 

"""

from __future__ import annotations

from io import BytesIO

from org_memory.core.errors import ConfigurationError, NotFoundError, VendorAPIError
from org_memory.core.settings import Settings

_MULTIPART_DEFAULT = 8_388_608


class S3ObjectStore:
    def __init__(self, settings: Settings):
        try:
            import boto3
            from boto3.s3.transfer import TransferConfig
            from botocore.config import Config
            from botocore.exceptions import BotoCoreError, ClientError
        except ImportError as exc:
            raise ConfigurationError(
                "OBJECT_STORE_BACKEND=s3 requires boto3. Install it with: pip install 'org-memory[s3]'"
            ) from exc
        self._ClientError = ClientError
        self._BotoCoreError = BotoCoreError
        self._TransferConfig = TransferConfig
        self._bucket = settings.s3_bucket_name
        self._sse = settings.s3_sse
        self._sse_kms_key_id = settings.s3_sse_kms_key_id
        self._multipart_threshold = settings.s3_multipart_threshold_bytes or _MULTIPART_DEFAULT

        client_kwargs: dict = {
            "region_name": settings.aws_region,
            "config": Config(
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=60,
                s3={
                    "addressing_style": "path" if settings.s3_force_path_style else "virtual"
                },
            ),
        }
        if settings.s3_endpoint_url:
            client_kwargs["endpoint_url"] = settings.s3_endpoint_url
        self._client = boto3.client("s3", **client_kwargs)
        self._verify_bucket()

    def _error_code(self, exc: BaseException) -> str:
        if not isinstance(exc, self._ClientError):
            return ""
        return str(exc.response.get("Error", {}).get("Code", "") or "")

    def _put_extra_args(self, content_type: str) -> dict:
        extra: dict = {
            "ContentType": content_type,
            "ServerSideEncryption": self._sse,
        }
        if self._sse == "aws:kms":
            extra["SSEKMSKeyId"] = self._sse_kms_key_id
        return extra

    def _verify_bucket(self) -> None:
        """Fail fast at startup if the bucket is missing or unreachable."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except self._ClientError as exc:
            code = self._error_code(exc)
            if code in ("404", "NoSuchBucket", "NotFound"):
                raise ConfigurationError(
                    f"S3 bucket '{self._bucket}' does not exist (S3_BUCKET_NAME)."
                ) from exc
            if code in ("403", "AccessDenied"):
                raise ConfigurationError(
                    f"Access denied to S3 bucket '{self._bucket}'. Check AWS "
                    "credentials, endpoint/path-style settings, and the bucket "
                    "policy for the running identity."
                ) from exc
            raise VendorAPIError(
                "s3",
                0,
                f"head_bucket failed for {self._bucket}: [{code}] {exc}",
                raw_response=str(exc),
            ) from exc
        except self._BotoCoreError as exc:
            raise VendorAPIError(
                "s3",
                0,
                f"head_bucket failed for {self._bucket}: {exc}",
                raw_response=str(exc),
            ) from exc

    def put(self, key: str, content: bytes, content_type: str) -> None:
        """Store bytes with SSE. Multipart at/above the configured threshold."""
        try:
            transfer = self._TransferConfig(
                multipart_threshold=self._multipart_threshold,
                multipart_chunksize=min(self._multipart_threshold, 8_388_608),
                max_concurrency=4,
            )
            self._client.upload_fileobj(
                BytesIO(content),
                self._bucket,
                key,
                ExtraArgs=self._put_extra_args(content_type),
                Config=transfer,
            )
        except (self._ClientError, self._BotoCoreError) as exc:
            code = self._error_code(exc)
            raise VendorAPIError(
                "s3",
                0,
                f"put_object failed for {key}: [{code}] {exc}",
                raw_response=str(exc),
            ) from exc

    def get(self, key: str) -> bytes:
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
            return resp["Body"].read()
        except self._ClientError as exc:
            code = self._error_code(exc)
            if code in ("NoSuchKey", "404", "NotFound"):
                raise NotFoundError(f"object not found: {key}") from exc
            raise VendorAPIError(
                "s3",
                0,
                f"get_object failed for {key}: [{code}] {exc}",
                raw_response=str(exc),
            ) from exc
        except self._BotoCoreError as exc:
            raise VendorAPIError(
                "s3",
                0,
                f"get_object failed for {key}: {exc}",
                raw_response=str(exc),
            ) from exc

    def delete(self, key: str) -> None:
        """Remove an object. Missing keys succeed (idempotent for retention)."""
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except self._ClientError as exc:
            code = self._error_code(exc)
            if code in ("NoSuchKey", "404", "NotFound"):
                return
            raise VendorAPIError(
                "s3",
                0,
                f"delete_object failed for {key}: [{code}] {exc}",
                raw_response=str(exc),
            ) from exc
        except self._BotoCoreError as exc:
            raise VendorAPIError(
                "s3",
                0,
                f"delete_object failed for {key}: {exc}",
                raw_response=str(exc),
            ) from exc

    def ping(self) -> None:
        """Re-check bucket reachability for readiness probes."""
        self._verify_bucket()
