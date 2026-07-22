"""Worldbuilder tools: person lookup and source read (caller wire envelope)."""

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
        if action.value == "profile":
            name = (body.name or body.query or "").strip()
            if not name:
                raise HTTPException(status_code=422, detail="action=profile requires `name`.")
            profile = service.lookup_person(principal, name)
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
                    "category": body.category,
                    "canonical_id": profile.get("canonical_id"),
                    "audit_id": profile.get("audit_id"),
                    "trace_id": profile.get("trace_id"),
                },
            )

        doc_ids = body.document_ids()
        if not doc_ids:
            raise HTTPException(
                status_code=422,
                detail="action=read_source requires source_document_ids (or read_source).",
            )
        sources = service.read_source(principal, doc_ids)
        return worldbuilder_envelope(
            status="ok",
            items=sources,
            summary=f"{len(sources)} sources readable",
            metadata={"action": "read_source", "requested": doc_ids},
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
