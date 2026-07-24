"""Shared SQL helpers for chunk candidate search and document visibility filters.
"""

from __future__ import annotations

from datetime import datetime

from org_memory.core.settings import get_settings
from org_memory.domain.models import Principal
from org_memory.ports.chunk_search import CandidateHit


def _document_visibility_filters_sql(alias: str = "d") -> str:
    """ACL + optional metadata filters over a documents table alias.

    Used by fact evidence visibility (documents only). Chunk search builds on
    this for document ACL/meta, then adds chunk-role and chunk-column filters.
    """
    a = alias
    return f"""
        {a}.workspace_id = :workspace_id
        AND {a}.deleted = false
        AND (
            {a}.org_visible = true
            OR {a}.allowed_principals && CAST(:viewer_principals AS text[])
        )
        AND (CAST(:source_type AS text) IS NULL OR {a}.source_type = :source_type)
        AND (CAST(:source_system AS text) IS NULL OR {a}.source_system = :source_system)
        AND (
            CAST(:author_patterns AS text[]) IS NULL
            OR {a}.author_display_name ILIKE ANY(CAST(:author_patterns AS text[]))
        )
        AND (
            CAST(:author_person_ids AS text[]) IS NULL
            OR EXISTS (
                SELECT 1 FROM document_participants dp
                WHERE dp.doc_id = {a}.doc_id
                  AND dp.person_id = ANY(CAST(:author_person_ids AS text[]))
                  AND dp.role = 'author'
            )
        )
        AND (
            CAST(:about_person_ids AS text[]) IS NULL
            OR EXISTS (
                SELECT 1 FROM document_participants dp
                WHERE dp.doc_id = {a}.doc_id
                  AND dp.person_id = ANY(CAST(:about_person_ids AS text[]))
            )
        )
        AND (CAST(:doc_id AS text) IS NULL OR {a}.doc_id = :doc_id)
        AND (CAST(:date_from AS timestamptz) IS NULL OR {a}.event_time >= :date_from)
        AND (CAST(:date_to AS timestamptz) IS NULL OR {a}.event_time <= :date_to)
        AND (CAST(:updated_from AS timestamptz) IS NULL OR {a}.updated_at >= :updated_from)
        AND (CAST(:updated_to AS timestamptz) IS NULL OR {a}.updated_at <= :updated_to)
    """


def _common_filters_sql() -> str:
    """Shared WHERE filters for vector and keyword search over child chunks.

    Live document ACL is authoritative; denormalized chunk ACL alone is not trusted.
    Chunk columns own source_type / author / event_time / doc_id for ranking filters;
    document columns own ACL, source_system, and updated_at.
    """
    return f"""
        c.workspace_id = :workspace_id
        AND c.deleted = false
        AND c.chunk_role = 'child'
        AND d.deleted = false
        AND d.workspace_id = :workspace_id
        AND (d.org_visible = true OR d.allowed_principals && :viewer_principals)
        AND (CAST(:source_type AS text) IS NULL OR c.source_type = :source_type)
        AND (CAST(:source_system AS text) IS NULL OR d.source_system = :source_system)
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
        AND (CAST(:updated_from AS timestamptz) IS NULL OR d.updated_at >= :updated_from)
        AND (CAST(:updated_to AS timestamptz) IS NULL OR d.updated_at <= :updated_to)
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
    source_system: str | None = None,
) -> dict:
    return {
        "workspace_id": get_settings().workspace_id,
        "viewer_principals": principal.all_principals(),
        "source_type": source_type,
        "source_system": source_system,
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
    c.author_display_name, c.event_time, c.deep_link,
    d.source_system AS source_system,
    d.updated_at AS updated_at
"""

_FROM_CHUNKS = """
    FROM chunks c
    INNER JOIN documents d
      ON d.doc_id = c.doc_id
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
        source_system=getattr(row, "source_system", "") or "",
        updated_at=getattr(row, "updated_at", None),
        rank=rank,
    )
