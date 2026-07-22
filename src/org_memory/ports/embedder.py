"""Interface for turning text into vectors.

Implementations live in adapters/. model_name is stored on every chunk so
vectors from different embedding models aren't mixed in one search.
"""

from __future__ import annotations

from typing import Protocol


class Embedder(Protocol):
    @property
    def model_name(self) -> str:
        """Model id stamped on each chunk (text-embedding-3-small)."""
        ...

    @property
    def dimensions(self) -> int: ...

    def embed_texts(self, texts: list[str]) -> tuple[list[list[float]], int]:
        """Embed a batch. Returns (vectors, tokens_used). Raises on failure;
        never invent zero or random vectors."""
        ...
