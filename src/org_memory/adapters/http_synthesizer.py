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

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        json_object: bool = False,
    ) -> tuple[str, int]:
        """Return (text, tokens_used). Raises VendorAPIError on failure.

        When json_object=True, request OpenAI-compatible response_format json_object.
        If the vendor rejects that parameter (HTTP 400), retry once without it.
        The retry only drops the format hint; prompt and output pass through unchanged.
        """
        return self._complete(
            system_prompt, user_prompt, json_object=json_object, allow_format_retry=True
        )

    def _complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        json_object: bool,
        allow_format_retry: bool,
    ) -> tuple[str, int]:
        payload: dict = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
        }
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        resp = post_with_retry(
            self._client,
            self._url,
            json=payload,
            vendor="synthesis",
        )
        if (
            resp.status_code == 400
            and json_object
            and allow_format_retry
            and "response_format" in (resp.text or "").lower()
        ):
            return self._complete(
                system_prompt,
                user_prompt,
                json_object=False,
                allow_format_retry=False,
            )
        if resp.status_code != 200:
            raise VendorAPIError(
                "synthesis",
                resp.status_code,
                resp.text[:500],
                raw_response=resp.text,
            )
        try:
            body = resp.json()
            text = body["choices"][0]["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("message content is empty or non-text")
            total_tokens = int(body["usage"]["total_tokens"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise VendorAPIError(
                "synthesis",
                resp.status_code,
                f"invalid response schema: {exc}",
                raw_response=resp.text,
            ) from exc
        return text, total_tokens
