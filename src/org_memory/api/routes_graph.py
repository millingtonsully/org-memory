"""Viewer-scoped graph read API for person and entity cards.

Extracted graph rows (entities, claims, relationships) are returned
only when they are active and every current evidence document is visible to
the requesting principal (all-visible).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from org_memory.api.deps import bind_principal, get_session, require_api_key
from org_memory.core.errors import NotFoundError
from org_memory.db.repositories import (
    GraphRepository,
    PersonRepository,
)
from org_memory.domain.models import Principal

router = APIRouter(prefix="/v1/graph", dependencies=[Depends(require_api_key)])


def _relationship_dict(r, visible_doc_ids: list[str]) -> dict:
    return {
        "relationship_id": r.relationship_id,
        "from": {"type": r.from_type, "id": r.from_id},
        "to": {"type": r.to_type, "id": r.to_id},
        "relationship_type": r.relationship_type,
        "valid_from": r.valid_from.isoformat() if r.valid_from else None,
        "valid_to": r.valid_to.isoformat() if r.valid_to else None,
        "confidence": r.confidence,
        "evidence_doc_ids": visible_doc_ids,
    }


@router.get("/persons/by-platform-user/{platform_user_id}")
def person_by_platform_user(
    platform_user_id: str,
    principal: Principal = Depends(bind_principal),
    session: Session = Depends(get_session),
) -> dict:
    """Resolve OM person by host User UUID (identity:platform_user alias)."""
    persons = PersonRepository(session)
    person = persons.find_by_platform_user_id(platform_user_id)
    if person is None:
        raise NotFoundError(f"unknown platform_user: {platform_user_id}")
    evidence = persons.visible_evidence_doc_ids(person.canonical_id, principal)
    if not evidence:
        raise NotFoundError(f"unknown platform_user: {platform_user_id}")
    return {
        "canonical_id": person.canonical_id,
        "display_name": person.display_name,
        "resolution_status": person.resolution_status,
        "platform_user_id": platform_user_id.strip(),
        "identity_metadata": person.identity_metadata,
        "evidence_doc_ids": evidence,
    }


@router.get("/persons/{canonical_id}")
def person_card(
    canonical_id: str,
    as_of: datetime | None = Query(default=None),
    principal: Principal = Depends(bind_principal),
    session: Session = Depends(get_session),
) -> dict:
    persons = PersonRepository(session)
    person = persons.get(canonical_id)
    if person is None:
        raise NotFoundError(f"unknown person: {canonical_id}")
    if person.merged_into_id:
        person = persons.get(person.merged_into_id)
        if person is None:
            raise NotFoundError(f"merged person has no canonical root: {canonical_id}")
        canonical_id = person.canonical_id
    person_evidence_doc_ids = persons.visible_evidence_doc_ids(canonical_id, principal)
    if not person_evidence_doc_ids:
        raise NotFoundError(f"unknown person: {canonical_id}")
    graph = GraphRepository(session)
    return {
        "canonical_id": person.canonical_id,
        "display_name": person.display_name,
        "resolution_status": person.resolution_status,
        "platform_user_id": persons.platform_user_id_for(canonical_id),
        "identity_metadata": person.identity_metadata,
        "evidence_doc_ids": person_evidence_doc_ids,
        # Emails/aliases are withheld: they lack independent viewer ACL provenance.
        "relationships": [
            _relationship_dict(r, visible_doc_ids)
            for r, visible_doc_ids in graph.relationships_for_viewer(
                "person", canonical_id, principal, as_of=as_of
            )
        ],
        "claims": [
            {
                "predicate": c.predicate,
                "object": c.object_text,
                "confidence": c.confidence,
                "status": c.status,
                "valid_from": c.valid_from.isoformat() if c.valid_from else None,
                "valid_to": c.valid_to.isoformat() if c.valid_to else None,
                "evidence_doc_ids": visible_doc_ids,
                "evidence_quotes": [
                    q for q in (c.evidence_quotes or []) if q.get("doc_id") in set(visible_doc_ids)
                ],
            }
            for c, visible_doc_ids in graph.claims_for_viewer(
                "person", canonical_id, principal, statuses=["active"], as_of=as_of
            )
        ],
    }


@router.get("/entities")
def search_entities(
    name: str = Query(min_length=1),
    principal: Principal = Depends(bind_principal),
    session: Session = Depends(get_session),
) -> dict:
    entities = GraphRepository(session).search_entities_for_viewer(name, principal, limit=10)
    return {
        "entities": [
            {
                "entity_id": e.entity_id,
                "entity_type": e.entity_type,
                "name": e.name,
                "description": e.description,
                "resolution_status": e.resolution_status,
                "evidence_doc_ids": visible_doc_ids,
            }
            for e, visible_doc_ids in entities
        ]
    }


@router.get("/entities/{entity_id}")
def entity_card(
    entity_id: str,
    as_of: datetime | None = Query(default=None),
    principal: Principal = Depends(bind_principal),
    session: Session = Depends(get_session),
) -> dict:
    graph = GraphRepository(session)
    scoped = graph.get_entity_for_viewer(entity_id, principal)
    if scoped is None:
        raise NotFoundError(f"unknown entity: {entity_id}")
    entity, visible_doc_ids = scoped
    return {
        "entity_id": entity.entity_id,
        "entity_type": entity.entity_type,
        "name": entity.name,
        "description": entity.description,
        "attributes": entity.attributes,
        "resolution_status": entity.resolution_status,
        "evidence_doc_ids": visible_doc_ids,
        "relationships": [
            _relationship_dict(r, evidence_ids)
            for r, evidence_ids in graph.relationships_for_viewer(
                entity.entity_type, entity_id, principal, as_of=as_of
            )
        ],
        "claims": [
            {
                "predicate": c.predicate,
                "object": c.object_text,
                "confidence": c.confidence,
                "status": c.status,
                "valid_from": c.valid_from.isoformat() if c.valid_from else None,
                "valid_to": c.valid_to.isoformat() if c.valid_to else None,
                "evidence_doc_ids": evidence_ids,
                "evidence_quotes": [
                    q for q in (c.evidence_quotes or []) if q.get("doc_id") in set(evidence_ids)
                ],
            }
            for c, evidence_ids in graph.claims_for_viewer(
                entity.entity_type, entity_id, principal, statuses=["active"], as_of=as_of
            )
        ],
    }
