"""Voyage reranker used as the second-stage retrieval scorer.

There is no fallback in place.
"""

from __future__ import annotations

import httpx

from org_memory.adapters._http_retry import post_with_retry
from org_memory.core.errors import VendorAPIError
from org_memory.core.settings import Settings


class VoyageReranker:
    def __init__(self, settings: Settings):
        self._model = settings.rerank_model
        self._url = settings.rerank_api_url.rstrip("/")
        if not self._url.endswith("/rerank"):
            self._url = f"{self._url}/rerank"
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {settings.rerank_api_key}"},
            timeout=30.0,
        )

    @property
    def model_name(self) -> str:
        return self._model

    def rerank(self, query: str, documents: list[str]) -> tuple[list[float], int]:
        if not documents:
            return [], 0
        resp = post_with_retry(
            self._client,
            self._url,
            json={
                "model": self._model,
                "query": query,
                "documents": documents,
                "return_documents": False,
            },
            vendor="voyage-rerank",
        )
        if resp.status_code != 200:
            raise VendorAPIError(
                "voyage-rerank",
                resp.status_code,
                resp.text[:500],
                raw_response=resp.text,
            )
        try:
            payload = resp.json()
            items = payload["data"]
            if not isinstance(items, list) or len(items) != len(documents):
                raise ValueError(
                    f"expected {len(documents)} scores, received "
                    f"{len(items) if isinstance(items, list) else 'non-list data'}"
                )
            scores_by_index: dict[int, float] = {}
            for item in items:
                index = int(item["index"])
                score = float(item["relevance_score"])
                if index < 0 or index >= len(documents) or index in scores_by_index:
                    raise ValueError(f"invalid or duplicate result index: {index}")
                scores_by_index[index] = score
            scores = [scores_by_index[index] for index in range(len(documents))]
            total_tokens = int(payload["usage"]["total_tokens"])
        except (KeyError, TypeError, ValueError) as exc:
            raise VendorAPIError(
                "voyage-rerank",
                resp.status_code,
                f"invalid response schema: {exc}",
                raw_response=resp.text,
            ) from exc
        return scores, total_tokens
