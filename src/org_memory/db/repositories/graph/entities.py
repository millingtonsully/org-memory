"""Entity CRUD and viewer-scoped entity browse/search."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import text as sql

from org_memory.db.orm import Entity, utcnow
from org_memory.db.repositories.graph.base import (
    VISIBLE_DOCS_CTE,
    GraphRepositoryBase,
    all_visible_sql,
    evidence_lateral_sql,
)
from org_memory.domain.models import Principal

_ENTITY_EVIDENCE_LATERAL = evidence_lateral_sql("e")
_ENTITY_ALL_VISIBLE_SQL = all_visible_sql("e")


class GraphEntitiesMixin(GraphRepositoryBase):
    """Entity mutations and all-visible viewer reads."""

    def find_entity(self, entity_type: str, name: str) -> Entity | None:
        return (
            self._session.query(Entity)
            .filter(
                Entity.workspace_id == self._ws,
                Entity.entity_type == entity_type,
                Entity.normalized_name == self.normalize_name(name),
            )
            .first()
        )

    def get_entity(self, entity_id: str) -> Entity | None:
        entity = self._session.get(Entity, entity_id)
        if entity is not None and entity.workspace_id != self._ws:
            return None
        return entity

    def upsert_entity(
        self,
        entity_type: str,
        name: str,
        description: str = "",
        evidence_doc_id: str | None = None,
    ) -> Entity:
        """Get or create by normalized name. Merge evidence for viewer scoping."""
        existing = self.find_entity(entity_type, name)
        if existing is not None:
            if description and not existing.description:
                existing.description = description
            if evidence_doc_id:
                merged = set(existing.evidence_doc_ids or [])
                merged.add(evidence_doc_id)
                existing.evidence_doc_ids = sorted(merged)
            existing.updated_at = utcnow()
            return existing
        entity = Entity(
            workspace_id=self._ws,
            entity_type=entity_type,
            name=name.strip(),
            normalized_name=self.normalize_name(name),
            description=description,
            evidence_doc_ids=[evidence_doc_id] if evidence_doc_id else [],
        )
        self._session.add(entity)
        self._session.flush()
        return entity

    def search_entities(self, name: str, limit: int = 5, *, entity_type: str | None = None) -> list[Entity]:
        q = self._session.query(Entity).filter(
            Entity.workspace_id == self._ws,
            Entity.name.ilike(f"%{name}%"),
        )
        if entity_type:
            q = q.filter(Entity.entity_type == entity_type.strip().lower())
        return q.limit(limit).all()

    def search_entities_for_viewer(
        self,
        name: str,
        principal: Principal,
        limit: int = 5,
        *,
        entity_type: str | None = None,
    ) -> list[tuple[Entity, list[str]]]:
        """Return entities whose *entire* evidence set is visible to this viewer.

        All-visible (not any-visible): mixed private/public evidence must not
        surface entity name/description/attributes to a viewer who cannot see
        every supporting document. Empty evidence never surfaces. ACL is applied
        in SQL so private rows never consume ``limit``.
        """
        type_filter = ""
        params: dict = {
            "workspace_id": self._ws,
            "viewer_principals": principal.all_principals(),
            "name_pattern": f"%{name}%",
            "limit": max(1, int(limit)),
        }
        if entity_type:
            type_filter = "AND e.entity_type = :entity_type"
            params["entity_type"] = entity_type.strip().lower()
        rows = self._session.execute(
            sql(f"""
                WITH {VISIBLE_DOCS_CTE}
                SELECT e.entity_id, evidence.doc_ids AS evidence_doc_ids
                FROM entities e
                {_ENTITY_EVIDENCE_LATERAL}
                WHERE e.workspace_id = :workspace_id
                  AND e.name ILIKE :name_pattern
                  {type_filter}
                  AND {_ENTITY_ALL_VISIBLE_SQL}
                ORDER BY e.name ASC
                LIMIT :limit
            """),
            params,
        ).mappings().all()
        return self._hydrate_entities_with_evidence(
            [dict(row) for row in rows]
        )

    def list_entities_for_viewer(
        self,
        principal: Principal,
        *,
        entity_type: str,
        limit: int = 50,
    ) -> list[tuple[Entity, list[str]]]:
        """Browse visible entities of one type (bounded; ordered by name).

        All-visible evidence ACL is enforced in SQL so private entities never
        consume the browse limit.
        """
        rows = self._session.execute(
            sql(f"""
                WITH {VISIBLE_DOCS_CTE}
                SELECT e.entity_id, evidence.doc_ids AS evidence_doc_ids
                FROM entities e
                {_ENTITY_EVIDENCE_LATERAL}
                WHERE e.workspace_id = :workspace_id
                  AND e.entity_type = :entity_type
                  AND {_ENTITY_ALL_VISIBLE_SQL}
                ORDER BY e.name ASC
                LIMIT :limit
            """),
            {
                "workspace_id": self._ws,
                "viewer_principals": principal.all_principals(),
                "entity_type": entity_type.strip().lower(),
                "limit": max(1, int(limit)),
            },
        ).mappings().all()
        return self._hydrate_entities_with_evidence(
            [dict(row) for row in rows]
        )

    def _hydrate_entities_with_evidence(
        self, rows: Sequence[Mapping[str, Any]]
    ) -> list[tuple[Entity, list[str]]]:
        if not rows:
            return []
        ids = [row["entity_id"] for row in rows]
        by_id = {
            entity.entity_id: entity
            for entity in self._session.query(Entity)
            .filter(Entity.workspace_id == self._ws, Entity.entity_id.in_(ids))
            .all()
        }
        out: list[tuple[Entity, list[str]]] = []
        for row in rows:
            entity = by_id.get(row["entity_id"])
            if entity is None:
                continue
            evidence = list(row["evidence_doc_ids"] or [])
            out.append((entity, evidence))
        return out

    def get_entity_for_viewer(
        self, entity_id: str, principal: Principal
    ) -> tuple[Entity, list[str]] | None:
        """Return entity only when every evidence document is viewer-visible.

        All-visible ACL is enforced in SQL (same predicate as browse/search).
        """
        rows = self._session.execute(
            sql(f"""
                WITH {VISIBLE_DOCS_CTE}
                SELECT e.entity_id, evidence.doc_ids AS evidence_doc_ids
                FROM entities e
                {_ENTITY_EVIDENCE_LATERAL}
                WHERE e.workspace_id = :workspace_id
                  AND e.entity_id = :entity_id
                  AND {_ENTITY_ALL_VISIBLE_SQL}
                LIMIT 1
            """),
            {
                "workspace_id": self._ws,
                "viewer_principals": principal.all_principals(),
                "entity_id": entity_id,
            },
        ).mappings().all()
        hydrated = self._hydrate_entities_with_evidence([dict(row) for row in rows])
        if not hydrated:
            return None
        return hydrated[0]
