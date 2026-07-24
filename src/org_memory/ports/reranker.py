"""Interface for scoring how well each document matches a query.

Reranking runs for agent search when the shortlist is larger than the final
limit, always via a hosted API (no local fallback scorer).
"""

from __future__ import annotations

from typing import Protocol


class Reranker(Protocol):
    @property
    def model_name(self) -> str: ...

    def rerank(self, query: str, documents: list[str]) -> tuple[list[float], int]:
        """Score every document against the query. Returns
        (scores_in_input_order, tokens_used). Higher score means more relevant.
        Raises on failure; the caller does not fall back to un-reranked order."""
        ...
