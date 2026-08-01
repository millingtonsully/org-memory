"""Evaluation-only embedder that returns planted vectors for known texts.

Used by the live retrieval eval runner so Postgres hybrid search can rank the
seeded gold corpus without calling a vendor. Unknown texts raise — this is not
a production fallback.
"""

from __future__ import annotations

from org_memory.core.errors import VendorAPIError
from org_memory.db.orm import EMBEDDING_DIM


class EvalFixtureEmbedder:
    """Maps exact evaluation texts to planted vectors."""

    def __init__(self, *, model_name: str = "eval-fixture-embedder", dimensions: int = EMBEDDING_DIM):
        self._model_name = model_name
        self._dimensions = dimensions
        self._table: dict[str, list[float]] = {}

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def plant(self, text: str, vector: list[float]) -> None:
        key = text.strip()
        if not key:
            raise ValueError("cannot plant an empty text")
        if len(vector) != self._dimensions:
            raise ValueError(
                f"vector length {len(vector)} != embedder dimensions {self._dimensions}"
            )
        self._table[key] = list(vector)

    def embed_texts(self, texts: list[str]) -> tuple[list[list[float]], int]:
        vectors: list[list[float]] = []
        for text in texts:
            key = text.strip()
            vector = self._table.get(key)
            if vector is None:
                raise VendorAPIError(
                    "eval-fixture-embedder",
                    None,
                    f"no planted vector for text {key[:80]!r}",
                )
            vectors.append(list(vector))
        return vectors, 0


def unit_vector(slot: int, *, dimensions: int = EMBEDDING_DIM) -> list[float]:
    """One-hot-ish unit vector for a gold case index (deterministic, distinct)."""
    if dimensions < 1:
        raise ValueError("dimensions must be >= 1")
    vector = [0.0] * dimensions
    vector[slot % dimensions] = 1.0
    return vector
