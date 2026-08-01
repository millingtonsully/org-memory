"""Channel and filter diagnostics for hybrid search responses.

Safe to return to agents: counts and active filters only — never denied doc ids.
"""

from __future__ import annotations

from datetime import datetime


def filter_diagnostics(
    *,
    source_type: str | None,
    source_system: str | None,
    author: str | None,
    author_canonical_entity_id: str | None,
    about_person_ids: list[str] | None,
    about_doc_ids: list[str] | None,
    date_from: datetime | None,
    date_to: datetime | None,
    updated_from: datetime | None,
    updated_to: datetime | None,
    doc_id: str | None,
) -> dict:
    """Active request filters only — never lists of denied document ids."""
    return {
        "source_type": source_type,
        "source_system": source_system,
        "author": author,
        "author_canonical_entity_id": author_canonical_entity_id,
        "about_person_count": len(about_person_ids or []),
        "about_doc_count": len(about_doc_ids or []),
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "updated_from": updated_from.isoformat() if updated_from else None,
        "updated_to": updated_to.isoformat() if updated_to else None,
        "doc_id": doc_id,
    }


def build_search_diagnostics(
    *,
    mode: str,
    limit: int,
    candidate_pool: int,
    rrf_k: int,
    vector_count: int,
    keyword_count: int,
    fact_count: int,
    fused_count: int,
    shortlist_count: int,
    returned_passages: int,
    returned_facts: int,
    parent_dedupe_removed: int,
    reranked: bool,
    rerank_skipped_reason: str | None,
    filters: dict,
) -> dict:
    """Assemble channel/ranking diagnostics safe to return to agents."""
    return {
        "mode": mode,
        "limit": limit,
        "candidate_pool": candidate_pool,
        "rrf_k": rrf_k,
        "channels": {
            "vector_candidates": vector_count,
            "keyword_candidates": keyword_count,
            "fact_candidates": fact_count,
        },
        "fusion": {
            "fused_candidates": fused_count,
            "shortlist": shortlist_count,
        },
        "rerank": {
            "applied": reranked,
            "skipped_reason": rerank_skipped_reason,
        },
        "returned": {
            "passages": returned_passages,
            "facts": returned_facts,
            "parent_dedupe_removed": parent_dedupe_removed,
        },
        "filters": filters,
    }
