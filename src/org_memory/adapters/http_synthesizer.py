"""HTTP chat client for synthesis, adjudication, and extraction.

Point SYNTHESIS_API_URL and SYNTHESIS_API_KEY at any OpenAI-compatible
chat-completions host.
"""

from __future__ import annotations

import httpx

from org_memory.adapters._http_retry import post_with_retry
from org_memory.core.errors import VendorAPIError
from org_memory.core.settings import Settings


class HttpSynthesizer:
    def __init__(self, settings: Settings):
        self._model = settings.synthesis_model
        self._url = settings.synthesis_api_url.rstrip("/")
        if not self._url.endswith("/chat/completions"):
            self._url = f"{self._url}/chat/completions"
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {settings.synthesis_api_key}"},
            timeout=90.0,
        )

    @property
    def model_name(self) -> str:
        return self._model

    def complete(self, system_prompt: str, user_prompt: str) -> tuple[str, int]:
        """Return (text, tokens_used). Raises VendorAPIError on failure."""
        resp = post_with_retry(
            self._client,
            self._url,
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
            },
            vendor="synthesis",
        )
        if resp.status_code != 200:
            raise VendorAPIError(
                "synthesis",
                resp.status_code,
                resp.text[:500],
                raw_response=resp.text,
            )
        try:
            payload = resp.json()
            text = payload["choices"][0]["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("message content is empty or non-text")
            total_tokens = int(payload["usage"]["total_tokens"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise VendorAPIError(
                "synthesis",
                resp.status_code,
                f"invalid response schema: {exc}",
                raw_response=resp.text,
            ) from exc
        return text, total_tokens
