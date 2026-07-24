"""Shared SQL helpers for chunk candidate search.
"""

from __future__ import annotations

from datetime import datetime

from org_memory.core.settings import get_settings
from org_memory.domain.models import Principal
from org_memory.ports.chunk_search import CandidateHit


def _common_filters_sql() -> str:
    """Shared WHERE filters for vector and keyword search over child chunks."""
    return """
        c.workspace_id = :workspace_id
        AND c.deleted = false
        AND c.chunk_role = 'child'
        AND (c.org_visible = true OR c.allowed_principals && :viewer_principals)
        AND (CAST(:source_type AS text) IS NULL OR c.source_type = :source_type)
        AND (CAST(:author_patterns AS text[]) IS NULL
             OR c.author_display_name ILIKE ANY(CAST(:author_patterns AS text[])))
        AND (
            CAST(:author_person_ids AS text[]) IS NULL
            OR EXISTS (
                SELECT 1 FROM document_participants dp
                WHERE dp.doc_id = c.doc_id
                  AND dp.person_id = ANY(CAST(:author_person_ids AS text[]))
                  AND dp.role = 'author'
            )
        )
        AND (
            CAST(:about_person_ids AS text[]) IS NULL
            OR EXISTS (
                SELECT 1 FROM document_participants dp
                WHERE dp.doc_id = c.doc_id
                  AND dp.person_id = ANY(CAST(:about_person_ids AS text[]))
            )
        )
        AND (CAST(:doc_id AS text) IS NULL OR c.doc_id = :doc_id)
        AND (CAST(:date_from AS timestamptz) IS NULL OR c.event_time >= :date_from)
        AND (CAST(:date_to AS timestamptz) IS NULL OR c.event_time <= :date_to)
        AND (CAST(:updated_from AS timestamptz) IS NULL OR c.updated_at >= :updated_from)
        AND (CAST(:updated_to AS timestamptz) IS NULL OR c.updated_at <= :updated_to)
    """


def _common_params(
    principal: Principal,
    source_type: str | None,
    author_patterns: list[str] | None,
    date_from: datetime | None,
    date_to: datetime | None,
    updated_from: datetime | None,
    updated_to: datetime | None,
    doc_id: str | None,
    author_person_ids: list[str] | None = None,
    about_person_ids: list[str] | None = None,
) -> dict:
    return {
        "workspace_id": get_settings().workspace_id,
        "viewer_principals": principal.all_principals(),
        "source_type": source_type,
        "author_patterns": author_patterns or None,
        "author_person_ids": author_person_ids or None,
        "about_person_ids": about_person_ids or None,
        "doc_id": doc_id,
        "date_from": date_from,
        "date_to": date_to,
        "updated_from": updated_from,
        "updated_to": updated_to,
    }


_SELECT_COLUMNS = """
    c.chunk_id, c.doc_id, c.source_type, c.title,
    coalesce(p.text, c.text) AS text,
    c.parent_chunk_id,
    c.author_display_name, c.event_time, c.deep_link
"""

_FROM_CHUNKS = """
    FROM chunks c
    LEFT JOIN chunks p
      ON p.chunk_id = c.parent_chunk_id
     AND p.deleted = false
"""


def _row_to_hit(row, rank: int) -> CandidateHit:
    return CandidateHit(
        chunk_id=row.chunk_id,
        doc_id=row.doc_id,
        source_type=row.source_type,
        title=row.title,
        text=row.text,
        parent_chunk_id=getattr(row, "parent_chunk_id", None),
        author_display_name=row.author_display_name,
        event_time=row.event_time,
        deep_link=row.deep_link,
        rank=rank,
    )
