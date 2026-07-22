"""Object storage interface: put, get, and delete bytes by key.
"""

from __future__ import annotations

from typing import Protocol


class ObjectStore(Protocol):
    def put(self, key: str, content: bytes, content_type: str) -> None:
        """Store bytes under a key. Overwrites are allowed."""
        ...

    def get(self, key: str) -> bytes:
        """Fetch bytes. Raises NotFoundError if the key is missing."""
        ...

    def delete(self, key: str) -> None:
        """Remove an object. Used by retention cleanup."""
        ...

    def ping(self) -> None:
        """Lightweight readiness check (bucket reachable). Raises on failure."""
        ...
