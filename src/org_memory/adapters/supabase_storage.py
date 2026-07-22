"""Supabase Storage object store.

Required settings: SUPABASE_PROJECT_URL and SUPABASE_SERVICE_ROLE_KEY. Stores
raw ingested payloads so every indexed row can be traced back to the exact
input that produced it. Talks to Supabase's Storage REST API directly with the
service_role key.
"""

from __future__ import annotations

import httpx

from org_memory.core.errors import ConfigurationError, NotFoundError, VendorAPIError
from org_memory.core.settings import Settings


class SupabaseObjectStore:
    def __init__(self, settings: Settings):
        if not settings.supabase_project_url or not settings.supabase_service_role_key:
            raise ConfigurationError(
                "SUPABASE_PROJECT_URL and SUPABASE_SERVICE_ROLE_KEY are required "
                "when OBJECT_STORE_BACKEND=supabase. Set them in .env or switch backends."
            )
        self._base = settings.supabase_project_url.rstrip("/")
        self._bucket = settings.supabase_storage_bucket
        # The Storage gateway expects both the bearer token and the apikey header.
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {settings.supabase_service_role_key}",
                "apikey": settings.supabase_service_role_key,
            },
            timeout=30.0,
        )

    def ensure_bucket(self) -> None:
        """Create the private bucket if it does not exist. Runs at startup so a
        bad bucket name fails now instead of on the first write."""
        resp = self._client.get(f"{self._base}/storage/v1/bucket/{self._bucket}")
        if resp.status_code == 200:
            return
        create = self._client.post(
            f"{self._base}/storage/v1/bucket",
            json={"id": self._bucket, "name": self._bucket, "public": False},
        )
        if create.status_code not in (200, 201):
            raise VendorAPIError("supabase-storage", create.status_code, create.text)

    def put(self, key: str, content: bytes, content_type: str) -> None:
        # x-upsert lets a repeated write overwrite instead of failing with 409.
        resp = self._client.post(
            f"{self._base}/storage/v1/object/{self._bucket}/{key}",
            content=content,
            headers={"Content-Type": content_type, "x-upsert": "true"},
        )
        if resp.status_code not in (200, 201):
            raise VendorAPIError("supabase-storage", resp.status_code, resp.text)

    def get(self, key: str) -> bytes:
        resp = self._client.get(f"{self._base}/storage/v1/object/{self._bucket}/{key}")
        if resp.status_code == 404 or resp.status_code == 400:
            raise NotFoundError(f"object not found: {key}")
        if resp.status_code != 200:
            raise VendorAPIError("supabase-storage", resp.status_code, resp.text)
        return resp.content

    def delete(self, key: str) -> None:
        resp = self._client.delete(f"{self._base}/storage/v1/object/{self._bucket}/{key}")
        if resp.status_code not in (200, 204):
            raise VendorAPIError("supabase-storage", resp.status_code, resp.text)

    def ping(self) -> None:
        """Confirm the configured bucket is reachable (readiness)."""
        resp = self._client.get(f"{self._base}/storage/v1/bucket/{self._bucket}")
        if resp.status_code != 200:
            raise VendorAPIError(
                "supabase-storage",
                resp.status_code,
                f"bucket ping failed for {self._bucket}: {resp.text}",
            )
