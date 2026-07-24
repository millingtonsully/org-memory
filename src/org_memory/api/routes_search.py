"""Agent retrieval tools: search_knowledge_base and worldbuilder_kb."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from org_memory.api.deps import (
    bind_principal,
    get_retrieval_service,
    get_worldbuilder_service,
    require_api_key,
)
from org_memory.api.tool_wire import (
    McpToolResponse,
    SearchKnowledgeBaseRequest,
    WorldbuilderKbRequest,
    parse_yyyy_mm_dd,
    search_response_to_mcp,
    worldbuilder_envelope,
)
from org_memory.core.errors import NotFoundError
from org_memory.domain.models import Principal
from org_memory.services.retrieval import RetrievalService
from org_memory.services.worldbuilder import WorldbuilderService

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post("/tools/search_knowledge_base", response_model=McpToolResponse)
def search_knowledge_base(
    body: SearchKnowledgeBaseRequest,
    principal: Principal = Depends(bind_principal),
    retrieval: RetrievalService = Depends(get_retrieval_service),
) -> McpToolResponse:
    result = retrieval.search(
        principal=principal,
        query=body.query,
        limit=body.limit,
        source_type=body.source_type,
        source_system=body.source_system,
        author=body.author,
        date_from=parse_yyyy_mm_dd(body.date_from),
        date_to=parse_yyyy_mm_dd(body.date_to, end_of_day=True),
        updated_from=parse_yyyy_mm_dd(body.updated_from),
        updated_to=parse_yyyy_mm_dd(body.updated_to, end_of_day=True),
        doc_id=body.doc_id,
        half_life_days=body.half_life_days,
        min_decay=body.min_decay,
        tool_name="search_knowledge_base",
    )
    return search_response_to_mcp(result)


@router.post("/tools/worldbuilder_kb")
def worldbuilder_kb(
    body: WorldbuilderKbRequest,
    principal: Principal = Depends(bind_principal),
    retrieval: RetrievalService = Depends(get_retrieval_service),
    worldbuilder: WorldbuilderService = Depends(get_worldbuilder_service),
) -> dict:
    about_person_ids: list[str] | None = None
    about_doc_ids: list[str] | None = None
    if body.about:
        try:
            resolved = worldbuilder.resolve_about_subject(
                principal, body.about, category=body.about_category
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if resolved["kind"] == "ambiguous":
            return worldbuilder_envelope(
                status="ambiguous",
                items=resolved["disambiguation"],
                summary=resolved["detail"],
                metadata={"about": body.about, "about_category": body.about_category},
            )
        if resolved["kind"] == "person":
            about_person_ids = list(resolved["about_person_ids"])
        else:
            about_doc_ids = list(resolved["about_doc_ids"])

    result = retrieval.search(
        principal=principal,
        query=body.resolved_query(),
        limit=body.limit,
        source_type=body.source_type,
        source_system=body.source_system,
        author=body.author,
        author_canonical_entity_id=body.author_canonical_entity_id,
        about_person_ids=about_person_ids,
        about_doc_ids=about_doc_ids,
        date_from=parse_yyyy_mm_dd(body.date_from),
        date_to=parse_yyyy_mm_dd(body.date_to, end_of_day=True),
        half_life_days=body.half_life_days,
        min_decay=body.min_decay,
        mode=body.mode.value,
        tool_name="worldbuilder_kb",
    )
    items = [
        {
            "chunk_id": p.chunk_id,
            "doc_id": p.doc_id,
            "text": p.text,
            "source_type": p.source_type,
            "source_system": p.source_system or None,
            "author": p.author_display_name,
            "title": p.title,
            "deep_link": p.deep_link,
            "score": p.score,
            "event_time": p.event_time.isoformat(),
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        }
        for p in result.passages
    ]
    return worldbuilder_envelope(
        status="ok",
        items=items,
        summary=f"{len(items)} passages for {body.resolved_query()!r}",
        metadata={
            "mode": body.mode.value,
            "about": body.about,
            "about_category": body.about_category,
            "about_person_ids": about_person_ids,
            "about_doc_ids": about_doc_ids,
            "source_type": body.source_type,
            "source_system": body.source_system,
            "author": body.author,
            "author_canonical_entity_id": body.author_canonical_entity_id,
            "half_life_days": body.half_life_days,
            "min_decay": body.min_decay,
            "audit_id": result.audit_id,
            "total_candidates": result.total_candidates,
            "reranked": result.reranked,
            "facts": [f.model_dump(mode="json") for f in result.facts],
        },
    )
