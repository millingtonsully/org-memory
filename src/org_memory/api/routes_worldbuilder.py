"""Worldbuilder tools: category lookup and ACL'd source read."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from org_memory.api.deps import (
    bind_principal,
    get_worldbuilder_service,
    require_api_key,
)
from org_memory.api.tool_wire import WorldbuilderLookupRequest, worldbuilder_envelope
from org_memory.core.errors import NotFoundError
from org_memory.domain.models import Principal
from org_memory.services.worldbuilder import WorldbuilderService

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post("/tools/worldbuilder_lookup")
def worldbuilder_lookup(
    body: WorldbuilderLookupRequest,
    principal: Principal = Depends(bind_principal),
    service: WorldbuilderService = Depends(get_worldbuilder_service),
) -> dict:
    try:
        action = body.resolved_action()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        if action.value == "list":
            if not body.category:
                raise HTTPException(
                    status_code=422, detail="action=list requires `category`."
                )
            payload = service.list_category(
                principal, category=body.category, limit=body.limit
            )
            return worldbuilder_envelope(
                status="ok",
                items=payload["items"],
                summary=f"{payload['returned']} {body.category} subjects",
                metadata={"action": "list", "category": body.category},
            )

        if action.value == "profile":
            name = (body.name or "").strip()
            if not name:
                raise HTTPException(status_code=422, detail="action=profile requires `name`.")
            profile = service.lookup(
                principal,
                name=name,
                category=body.category,
                query=body.query,
                half_life_days=body.half_life_days,
                min_decay=body.min_decay,
            )
            if "disambiguation" in profile:
                return worldbuilder_envelope(
                    status="ambiguous",
                    items=profile["disambiguation"],
                    summary=profile["detail"],
                    metadata={"action": "profile", "category": body.category},
                )
            return worldbuilder_envelope(
                status="ok",
                items=[profile],
                summary=f"Profile for {profile.get('display_name', name)}",
                metadata={
                    "action": "profile",
                    "category": profile.get("category"),
                    "canonical_id": profile.get("canonical_id"),
                    "audit_id": profile.get("audit_id"),
                    "trace_id": profile.get("trace_id"),
                    "synthesized_at": profile.get("synthesized_at"),
                },
            )

        doc_ids = body.document_ids_only()
        record_ids = body.record_ids_only()
        if not doc_ids and not record_ids:
            raise HTTPException(
                status_code=422,
                detail=(
                    "action=read_source requires source_document_ids "
                    "and/or source_record_ids."
                ),
            )
        payload = service.read_source(
            principal,
            document_ids=doc_ids,
            record_ids=record_ids,
        )
        sources = payload["sources"]
        outcomes = payload["outcomes"]
        forbidden = sum(1 for o in outcomes if o["outcome"] == "forbidden")
        missing = sum(1 for o in outcomes if o["outcome"] == "not_found")
        status = "ok" if not forbidden and not missing else "partial"
        return worldbuilder_envelope(
            status=status,
            items=sources,
            summary=(
                f"{len(sources)} sources readable; {forbidden} forbidden; "
                f"{missing} not_found"
            ),
            metadata={
                "action": "read_source",
                "requested_documents": doc_ids,
                "requested_records": record_ids,
                "outcomes": outcomes,
            },
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
