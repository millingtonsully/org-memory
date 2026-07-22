"""Error types used across the service.
"""

from __future__ import annotations


class OrgMemoryError(Exception):
    """Base class so callers can catch any of our errors at once."""


class ConfigurationError(OrgMemoryError):
    """A required setting is missing or invalid. Raised at startup."""


class VendorAPIError(OrgMemoryError):
    """A call to an outside API (embedding, synthesis, rerank, storage) failed."""

    def __init__(
        self,
        vendor: str,
        status_code: int | None,
        detail: str,
        *,
        raw_response: str = "",
    ):
        self.vendor = vendor
        self.status_code = status_code
        self.detail = detail
        # Kept on the exception for internal error capture; callers should not
        # echo an untrusted vendor body directly to end users.
        self.raw_response = raw_response
        super().__init__(f"{vendor} API error (status={status_code}): {detail}")


class NotFoundError(OrgMemoryError):
    """A referenced record (document, person, blob) does not exist."""


class SpendLimitError(OrgMemoryError):
    """Monthly token hard limit reached; new spend-incurring work is refused."""

    def __init__(self, tokens_used: int, hard_limit: int):
        self.tokens_used = tokens_used
        self.hard_limit = hard_limit
        super().__init__(
            f"Monthly spend hard limit reached "
            f"(tokens_used={tokens_used}, hard_limit={hard_limit})."
        )
