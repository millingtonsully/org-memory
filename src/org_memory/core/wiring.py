"""Adapter wiring: process-wide singletons for outbound ports.
"""

from __future__ import annotations

from org_memory.adapters.http_embedder import HttpEmbedder
from org_memory.adapters.http_reranker import VoyageReranker
from org_memory.adapters.http_synthesizer import HttpSynthesizer
from org_memory.core.settings import get_settings
from org_memory.ports.embedder import Embedder
from org_memory.ports.object_store import ObjectStore
from org_memory.ports.reranker import Reranker

_embedder: Embedder | None = None
_reranker: Reranker | None = None
_object_store: ObjectStore | None = None
_synthesizer: HttpSynthesizer | None = None


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = HttpEmbedder(get_settings())
    return _embedder


def get_reranker() -> Reranker:
    global _reranker
    if _reranker is None:
        _reranker = VoyageReranker(get_settings())
    return _reranker


def get_synthesizer() -> HttpSynthesizer:
    global _synthesizer
    if _synthesizer is None:
        _synthesizer = HttpSynthesizer(get_settings())
    return _synthesizer


def make_object_store() -> ObjectStore:
    """Build the object store for OBJECT_STORE_BACKEND (supabase or s3).

    Business code only sees the ObjectStore port. Both providers are peers:
    the selected one must be fully configured, and there is no fallback to the
    other if it fails.
    """
    settings = get_settings()
    if settings.object_store_backend == "s3":
        from org_memory.adapters.s3_storage import S3ObjectStore

        return S3ObjectStore(settings)
    from org_memory.adapters.supabase_storage import SupabaseObjectStore

    store = SupabaseObjectStore(settings)
    store.ensure_bucket()
    return store


def get_object_store() -> ObjectStore:
    global _object_store
    if _object_store is None:
        _object_store = make_object_store()
    return _object_store
