"""Interface for candidate search over the indexed corpus.

The current implementation uses pgvector plus Postgres full-text search
(ts_rank, not BM25). A future BM25 path (ParadeDB pg_search in-DB, or
OpenSearch) should implement this same ChunkSearch protocol so retrieval
does not change.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from org_memory.domain.models import Principal


class CandidateHit(dict):
    """Keys: chunk_id, doc_id, source_type, title, text, parent_chunk_id,
    author_display_name, event_time, deep_link, rank. text is parent section
    text when parent_chunk_id is set."""


class ChunkSearch(Protocol):
    def vector_candidates(
        self,
        query_embedding: list[float],
        embedding_model: str,
        principal: Principal,
        limit: int,
        source_type: str | None,
        author_patterns: list[str] | None,
        date_from: datetime | None,
        date_to: datetime | None,
        updated_from: datetime | None,
        updated_to: datetime | None,
        doc_id: str | None,
        author_person_ids: list[str] | None = None,
        about_person_ids: list[str] | None = None,
    ) -> list[CandidateHit]: ...

    def keyword_candidates(
        self,
        query_text: str,
        principal: Principal,
        limit: int,
        source_type: str | None,
        author_patterns: list[str] | None,
        date_from: datetime | None,
        date_to: datetime | None,
        updated_from: datetime | None,
        updated_to: datetime | None,
        doc_id: str | None,
        author_person_ids: list[str] | None = None,
        about_person_ids: list[str] | None = None,
    ) -> list[CandidateHit]: ...
