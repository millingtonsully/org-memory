"""HTTP embedder for any OpenAI-compatible embeddings API.

Point EMBEDDING_API_URL and EMBEDDING_API_KEY at whatever host you use.
Default URL is OpenAI's public endpoint. Changing models usually requires
re-embedding the corpus; chunks.embedding_model tracks which model produced
each vector. Voyage is a good candidate should you switch hosts.
"""

from __future__ import annotations

import httpx

from org_memory.adapters._http_retry import post_with_retry
from org_memory.core.errors import VendorAPIError
from org_memory.core.settings import Settings

_MAX_BATCH = 128


class HttpEmbedder:
    def __init__(self, settings: Settings):
        self._model = settings.embedding_model
        self._dimensions = settings.embedding_dimensions
        self._url = settings.embedding_api_url.rstrip("/")
        if not self._url.endswith("/embeddings"):
            self._url = f"{self._url}/embeddings"
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
            timeout=60.0,
        )

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed_texts(self, texts: list[str]) -> tuple[list[list[float]], int]:
        if not texts:
            return [], 0
        vectors: list[list[float]] = []
        total_tokens = 0
        for start in range(0, len(texts), _MAX_BATCH):
            batch = texts[start : start + _MAX_BATCH]
            resp = post_with_retry(
                self._client,
                self._url,
                json={
                    "model": self._model,
                    "input": batch,
                    "dimensions": self._dimensions,
                },
                vendor="embedding",
            )
            if resp.status_code != 200:
                raise VendorAPIError(
                    "embedding",
                    resp.status_code,
                    resp.text[:500],
                    raw_response=resp.text,
                )
            try:
                payload = resp.json()
                items = sorted(payload["data"], key=lambda item: int(item["index"]))
                if len(items) != len(batch):
                    raise ValueError(f"expected {len(batch)} vectors, received {len(items)}")
                for expected_index, item in enumerate(items):
                    if int(item["index"]) != expected_index:
                        raise ValueError(f"missing vector index {expected_index}")
                    vector = item["embedding"]
                    if not isinstance(vector, list) or len(vector) != self._dimensions:
                        raise ValueError(f"vector {expected_index} has invalid dimensions")
                    vectors.append([float(value) for value in vector])
                total_tokens += int(payload["usage"]["total_tokens"])
            except (KeyError, TypeError, ValueError) as exc:
                raise VendorAPIError(
                    "embedding",
                    resp.status_code,
                    f"invalid response schema: {exc}",
                    raw_response=resp.text,
                ) from exc
        return vectors, total_tokens
