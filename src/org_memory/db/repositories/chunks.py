"""SQL repositories package modules.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from org_memory.db.repositories._common import (
    _SELECT_COLUMNS,
    _common_filters_sql,
    _common_params,
    _row_to_hit,
)
from org_memory.domain.models import Principal
from org_memory.ports.chunk_search import CandidateHit


class ChunkSearchRepository:
    """ChunkSearch port backed by pgvector and Postgres FTS."""

    def __init__(self, session: Session):
        self._session = session

    def vector_candidates(
        self,
        query_embedding: list[float],
        embedding_model: str,
        principal: Principal,
        limit: int,
        source_type: str | None = None,
        author_patterns: list[str] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        updated_from: datetime | None = None,
        updated_to: datetime | None = None,
        doc_id: str | None = None,
        author_person_ids: list[str] | None = None,
    ) -> list[CandidateHit]:
        query = sql(f"""
            SELECT {_SELECT_COLUMNS}
            FROM chunks c
            WHERE {_common_filters_sql()}
              -- Active embedding model only
              AND c.embedding IS NOT NULL
              AND c.embedding_model = :embedding_model
            ORDER BY c.embedding <=> CAST(:query_embedding AS vector)
            LIMIT :limit
        """)
        params = _common_params(
            principal,
            source_type,
            author_patterns,
            date_from,
            date_to,
            updated_from,
            updated_to,
            doc_id,
            author_person_ids,
        )
        params.update(
            {
                "embedding_model": embedding_model,
                # pgvector accepts JSON array literals for CAST(... AS vector)
                "query_embedding": json.dumps(query_embedding),
                "limit": limit,
            }
        )
        rows = self._session.execute(query, params).fetchall()
        return [_row_to_hit(row, rank) for rank, row in enumerate(rows, start=1)]

    def keyword_candidates(
        self,
        query_text: str,
        principal: Principal,
        limit: int,
        source_type: str | None = None,
        author_patterns: list[str] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        updated_from: datetime | None = None,
        updated_to: datetime | None = None,
        doc_id: str | None = None,
        author_person_ids: list[str] | None = None,
    ) -> list[CandidateHit]:
        """Lexical candidate channel for hybrid RRF.
        """
        # websearch_to_tsquery parses user queries safely
        query = sql(f"""
            SELECT {_SELECT_COLUMNS},
                   ts_rank(c.text_search, websearch_to_tsquery('english', :query_text)) AS kw_rank
            FROM chunks c
            WHERE {_common_filters_sql()}
              AND c.text_search @@ websearch_to_tsquery('english', :query_text)
            ORDER BY kw_rank DESC
            LIMIT :limit
        """)
        params = _common_params(
            principal,
            source_type,
            author_patterns,
            date_from,
            date_to,
            updated_from,
            updated_to,
            doc_id,
            author_person_ids,
        )
        params.update({"query_text": query_text, "limit": limit})
        rows = self._session.execute(query, params).fetchall()
        return [_row_to_hit(row, rank) for rank, row in enumerate(rows, start=1)]


