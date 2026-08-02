"""Entity and relationship writes plus SQL viewer-scoped claim/edge reads.

Claim lifecycle mutations live in `claims.py` (`GraphClaimsMixin`). Subject
claim/edge viewer reads live here and enforce validity, belief, and all-visible
evidence ACL in SQL (same predicates as hybrid candidates and path walks).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import text as sql

from org_memory.db.orm import Claim, Document, Entity, Relationship, utcnow
from org_memory.db.repositories.graph.base import GraphRepositoryBase
from org_memory.domain.fact_lifecycle import FactStatus, transition_fact
from org_memory.domain.models import Principal
from org_memory.services.temporality.grain import (
    belief_as_of_sql,
    fact_matches_as_of,
    resolve_validity_query_point,
    validity_as_of_sql,
)
from org_memory.services.temporality.merge import merge_temporal_fields

# All-visible evidence: every cited doc must be in visible_docs (same invariant as
# claims/paths). Applied in SQL so private entities never consume browse/search limits.
_ENTITY_VISIBLE_DOCS_CTE = """
    visible_docs AS (
        SELECT d.doc_id
        FROM documents d
        WHERE d.workspace_id = :workspace_id
          AND d.deleted = false
          AND (
              d.org_visible = true
              OR d.allowed_principals && CAST(:viewer_principals AS text[])
          )
    )
"""
_ENTITY_ALL_VISIBLE_SQL = """
    cardinality(e.evidence_doc_ids) > 0
    AND cardinality(evidence.doc_ids) = (
        SELECT count(DISTINCT x) FROM unnest(e.evidence_doc_ids) AS x
    )
"""
_ENTITY_EVIDENCE_LATERAL = """
    CROSS JOIN LATERAL (
        SELECT array_agg(v.doc_id) AS doc_ids
        FROM visible_docs v
        WHERE v.doc_id = ANY(e.evidence_doc_ids)
    ) evidence
"""
_CLAIM_VALIDITY_AS_OF = validity_as_of_sql("c")
_CLAIM_BELIEF_AS_OF = belief_as_of_sql("c")
_REL_VALIDITY_AS_OF = validity_as_of_sql("r")
_REL_BELIEF_AS_OF = belief_as_of_sql("r")
_CLAIM_ALL_VISIBLE_SQL = """
    cardinality(c.evidence_doc_ids) > 0
    AND cardinality(evidence.doc_ids) = (
        SELECT count(DISTINCT x) FROM unnest(c.evidence_doc_ids) AS x
    )
"""
_REL_ALL_VISIBLE_SQL = """
    cardinality(r.evidence_doc_ids) > 0
    AND cardinality(evidence.doc_ids) = (
        SELECT count(DISTINCT x) FROM unnest(r.evidence_doc_ids) AS x
    )
"""
_FACT_EVIDENCE_LATERAL = """
    CROSS JOIN LATERAL (
        SELECT array_agg(v.doc_id) AS doc_ids
        FROM visible_docs v
        WHERE v.doc_id = ANY({alias}.evidence_doc_ids)
    ) evidence
"""


class GraphWritesMixin(GraphRepositoryBase):
    """Mutations and subject-scoped reads for entities and edges."""

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
                WITH {_ENTITY_VISIBLE_DOCS_CTE}
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
                WITH {_ENTITY_VISIBLE_DOCS_CTE}
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

    def get_entity_for_viewer(self, entity_id: str, principal: Principal) -> tuple[Entity, list[str]] | None:
        """Return entity only when every evidence document is viewer-visible."""
        entity = self.get_entity(entity_id)
        if entity is None:
            return None
        evidence = list(entity.evidence_doc_ids or [])
        if not evidence:
            return None
        doc_ids = self.visible_evidence_doc_ids(evidence, principal)
        if len(doc_ids) != len(set(evidence)):
            return None
        return entity, doc_ids

    def find_relationship(
        self, from_type: str, from_id: str, to_type: str, to_id: str, relationship_type: str
    ) -> Relationship | None:
        return (
            self._session.query(Relationship)
            .filter(
                Relationship.workspace_id == self._ws,
                Relationship.from_type == from_type,
                Relationship.from_id == from_id,
                Relationship.to_type == to_type,
                Relationship.to_id == to_id,
                Relationship.relationship_type == relationship_type,
                Relationship.status != FactStatus.superseded.value,
            )
            .first()
        )

    def add_relationship(self, rel: Relationship) -> Relationship:
        """Merge duplicate edges and advance proposed facts to active."""
        existing = self.find_relationship(
            rel.from_type, rel.from_id, rel.to_type, rel.to_id, rel.relationship_type
        )
        if existing is not None:
            merged = set(existing.evidence_doc_ids) | set(rel.evidence_doc_ids)
            existing.evidence_doc_ids = sorted(merged)
            quotes = {
                (str(item.get("doc_id", "")), str(item.get("quote", ""))): item
                for item in [*(existing.evidence_quotes or []), *(rel.evidence_quotes or [])]
            }
            existing.evidence_quotes = list(quotes.values())
            existing.confidence = max(existing.confidence, rel.confidence)
            merge_temporal_fields(existing, rel)
            if rel.status == FactStatus.active.value and existing.status != FactStatus.active.value:
                transition_fact(
                    existing,
                    FactStatus.active,
                    rel.decided_by or "automatic:confidence_gate",
                )
                existing.invalidated_at = None
            elif existing.status == FactStatus.retracted.value and rel.status == FactStatus.proposed.value:
                transition_fact(existing, FactStatus.proposed, "")
                existing.invalidated_at = None
            existing.updated_at = utcnow()
            return existing
        rel.workspace_id = self._ws
        self._session.add(rel)
        self._session.flush()
        return rel

    def relationships_for(
        self,
        node_type: str,
        node_id: str,
        status: str = "active",
        as_of: datetime | None = None,
        believed_as_of: datetime | None = None,
        as_of_grain: str | None = None,
    ) -> list[Relationship]:
        """Edges attached to a node; world validity via resolve_validity_query_point."""
        q = self._session.query(Relationship).filter(
            Relationship.workspace_id == self._ws,
            Relationship.status == status,
            (
                (Relationship.from_type == node_type) & (Relationship.from_id == node_id)
                | (Relationship.to_type == node_type) & (Relationship.to_id == node_id)
            ),
        )
        if believed_as_of is not None:
            q = q.filter(
                Relationship.recorded_at <= believed_as_of,
                (Relationship.invalidated_at.is_(None))
                | (Relationship.invalidated_at > believed_as_of),
            )
        rows = q.order_by(Relationship.created_at).all()
        point, grain = resolve_validity_query_point(
            as_of=as_of,
            believed_as_of=believed_as_of,
            as_of_grain=as_of_grain,
            now=utcnow(),
        )
        return [
            rel
            for rel in rows
            if fact_matches_as_of(
                valid_from=rel.valid_from,
                valid_to=rel.valid_to,
                fact_grain=rel.time_grain,
                as_of=point,
                query_grain=grain,
            )
        ]

    def relationships_for_viewer(
        self,
        node_type: str,
        node_id: str,
        principal: Principal,
        status: str = "active",
        as_of: datetime | None = None,
        believed_as_of: datetime | None = None,
        as_of_grain: str | None = None,
        statuses: list[str] | None = None,
    ) -> list[tuple[Relationship, list[str]]]:
        """Return edges whose entire evidence set is visible to this viewer.

        All-visible ACL and grain-aware validity run in SQL so private edges never
        enter the result set (same predicates as ``paths_from`` / hybrid).
        """
        status_list = statuses if statuses is not None else [status]
        effective_as_of, effective_grain = resolve_validity_query_point(
            as_of=as_of,
            believed_as_of=believed_as_of,
            as_of_grain=as_of_grain,
            now=utcnow(),
        )
        evidence_lateral = _FACT_EVIDENCE_LATERAL.format(alias="r")
        rows = self._session.execute(
            sql(f"""
                WITH {_ENTITY_VISIBLE_DOCS_CTE}
                SELECT r.relationship_id, evidence.doc_ids AS evidence_doc_ids
                FROM relationships r
                {evidence_lateral}
                WHERE r.workspace_id = :workspace_id
                  AND r.status = ANY(CAST(:statuses AS text[]))
                  AND (
                      (r.from_type = :node_type AND r.from_id = :node_id)
                      OR (r.to_type = :node_type AND r.to_id = :node_id)
                  )
                  AND {_REL_VALIDITY_AS_OF}
                  AND {_REL_BELIEF_AS_OF}
                  AND {_REL_ALL_VISIBLE_SQL}
                ORDER BY r.created_at
            """),
            {
                "workspace_id": self._ws,
                "viewer_principals": principal.all_principals(),
                "node_type": node_type,
                "node_id": node_id,
                "statuses": status_list,
                "as_of": effective_as_of,
                "as_of_grain": effective_grain,
                "believed_as_of": believed_as_of,
            },
        ).mappings().all()
        if not rows:
            return []
        by_id = {
            rel.relationship_id: rel
            for rel in self._session.query(Relationship)
            .filter(
                Relationship.workspace_id == self._ws,
                Relationship.relationship_id.in_([row["relationship_id"] for row in rows]),
            )
            .all()
        }
        out: list[tuple[Relationship, list[str]]] = []
        for row in rows:
            rel = by_id.get(row["relationship_id"])
            if rel is None:
                continue
            out.append((rel, list(row["evidence_doc_ids"] or [])))
        return out

    def supersede_relationship(
        self,
        relationship: Relationship,
        superseded_by_relationship_id: str,
        decided_by: str,
        *,
        valid_to: datetime | None = None,
    ) -> None:
        """Retire a losing relationship in a mutually-exclusive slot."""
        winner = self._session.get(Relationship, superseded_by_relationship_id)
        close_at = valid_to
        if close_at is None and winner is not None:
            close_at = winner.valid_from
        if close_at is None:
            close_at = utcnow()
        relationship.valid_to = close_at
        relationship.invalidated_at = utcnow()
        transition_fact(relationship, FactStatus.superseded, decided_by)
        relationship.superseded_by_relationship_id = superseded_by_relationship_id

    def claims_for(
        self,
        subject_type: str,
        subject_id: str,
        statuses: list[str] | None = None,
        as_of: datetime | None = None,
        believed_as_of: datetime | None = None,
        as_of_grain: str | None = None,
    ) -> list[Claim]:
        q = self._session.query(Claim).filter(
            Claim.workspace_id == self._ws,
            Claim.subject_type == subject_type,
            Claim.subject_id == subject_id,
        )
        if statuses:
            q = q.filter(Claim.status.in_(statuses))
        if believed_as_of is not None:
            q = q.filter(
                Claim.recorded_at <= believed_as_of,
                (Claim.invalidated_at.is_(None)) | (Claim.invalidated_at > believed_as_of),
            )
        rows = q.order_by(Claim.created_at.desc()).all()
        point, grain = resolve_validity_query_point(
            as_of=as_of,
            believed_as_of=believed_as_of,
            as_of_grain=as_of_grain,
            now=utcnow(),
        )
        return [
            claim
            for claim in rows
            if fact_matches_as_of(
                valid_from=claim.valid_from,
                valid_to=claim.valid_to,
                fact_grain=claim.time_grain,
                as_of=point,
                query_grain=grain,
            )
        ]

    def claims_for_viewer(
        self,
        subject_type: str,
        subject_id: str,
        principal: Principal,
        statuses: list[str] | None = None,
        as_of: datetime | None = None,
        believed_as_of: datetime | None = None,
        as_of_grain: str | None = None,
    ) -> list[tuple[Claim, list[str]]]:
        """Return claims whose *entire* evidence set is visible to this viewer.

        All-visible ACL and grain-aware validity run in SQL so private claims never
        enter the result set (same predicates as hybrid ``fact_candidates``).
        """
        effective_as_of, effective_grain = resolve_validity_query_point(
            as_of=as_of,
            believed_as_of=believed_as_of,
            as_of_grain=as_of_grain,
            now=utcnow(),
        )
        status_clause = (
            "AND c.status = ANY(CAST(:statuses AS text[]))" if statuses else ""
        )
        evidence_lateral = _FACT_EVIDENCE_LATERAL.format(alias="c")
        rows = self._session.execute(
            sql(f"""
                WITH {_ENTITY_VISIBLE_DOCS_CTE}
                SELECT c.claim_id, evidence.doc_ids AS evidence_doc_ids
                FROM claims c
                {evidence_lateral}
                WHERE c.workspace_id = :workspace_id
                  AND c.subject_type = :subject_type
                  AND c.subject_id = :subject_id
                  {status_clause}
                  AND {_CLAIM_VALIDITY_AS_OF}
                  AND {_CLAIM_BELIEF_AS_OF}
                  AND {_CLAIM_ALL_VISIBLE_SQL}
                ORDER BY c.created_at DESC
            """),
            {
                "workspace_id": self._ws,
                "viewer_principals": principal.all_principals(),
                "subject_type": subject_type,
                "subject_id": subject_id,
                "statuses": statuses or [],
                "as_of": effective_as_of,
                "as_of_grain": effective_grain,
                "believed_as_of": believed_as_of,
            },
        ).mappings().all()
        if not rows:
            return []
        by_id = {
            claim.claim_id: claim
            for claim in self._session.query(Claim)
            .filter(
                Claim.workspace_id == self._ws,
                Claim.claim_id.in_([row["claim_id"] for row in rows]),
            )
            .all()
        }
        visible: list[tuple[Claim, list[str]]] = []
        for row in rows:
            claim = by_id.get(row["claim_id"])
            if claim is None:
                continue
            visible.append((claim, list(row["evidence_doc_ids"] or [])))
        return visible

    def remove_extraction_evidence(self, doc_id: str) -> None:
        """Retract LLM-extraction facts for a doc; keep structured_field ground truth."""
        self.remove_document_evidence(doc_id, created_by_prefixes=("extraction",))

    def remove_document_evidence(
        self,
        doc_id: str,
        *,
        created_by_prefixes: tuple[str, ...] | None = None,
    ) -> None:
        """Retract claims/relationships and strip entity evidence for a deleted doc.

        When created_by_prefixes is set, only facts whose created_by starts with one
        of those prefixes are retracted (used to clear LLM extraction without wiping
        structured_field ground truth). When None, every creator is cleared so a
        delete/tombstone removes all graph evidence keyed to this doc_id.

        If the removed evidence document was private (not org_visible), facts are
        retracted unless remaining quotes still cite remaining evidence docs —
        prevents private-derived text becoming all-visible on public leftovers.
        """
        removed_doc = self._session.get(Document, doc_id)
        removed_was_private = removed_doc is not None and not removed_doc.org_visible

        def _should_retract(remaining: list[str], remaining_quotes: list[dict]) -> bool:
            if not remaining:
                return True
            if not removed_was_private:
                return False
            cited = {
                str(quote.get("doc_id", ""))
                for quote in remaining_quotes
                if quote.get("doc_id")
            }
            return not (cited & set(remaining))

        relationships = (
            self._session.query(Relationship)
            .filter(
                Relationship.workspace_id == self._ws,
                Relationship.evidence_doc_ids.contains([doc_id]),
                Relationship.status.in_(["proposed", "active"]),
            )
            .all()
        )
        for relationship in relationships:
            if created_by_prefixes is not None and not any(
                (relationship.created_by or "").startswith(prefix)
                for prefix in created_by_prefixes
            ):
                continue
            remaining = [evidence for evidence in relationship.evidence_doc_ids if evidence != doc_id]
            remaining_quotes = [
                quote for quote in (relationship.evidence_quotes or []) if quote.get("doc_id") != doc_id
            ]
            relationship.evidence_doc_ids = remaining
            relationship.evidence_quotes = remaining_quotes
            if _should_retract(remaining, remaining_quotes):
                transition_fact(
                    relationship,
                    FactStatus.retracted,
                    "automatic:source_retraction",
                )
                close_at = (
                    removed_doc.event_time
                    if removed_doc is not None and removed_doc.event_time is not None
                    else utcnow()
                )
                if relationship.valid_to is None:
                    relationship.valid_to = close_at
                relationship.invalidated_at = utcnow()
            relationship.updated_at = utcnow()

        claims = (
            self._session.query(Claim)
            .filter(
                Claim.workspace_id == self._ws,
                Claim.evidence_doc_ids.contains([doc_id]),
                Claim.status.in_(["proposed", "active"]),
            )
            .all()
        )
        for claim in claims:
            if created_by_prefixes is not None and not any(
                (claim.created_by or "").startswith(prefix) for prefix in created_by_prefixes
            ):
                continue
            remaining = [evidence for evidence in claim.evidence_doc_ids if evidence != doc_id]
            remaining_quotes = [
                quote for quote in (claim.evidence_quotes or []) if quote.get("doc_id") != doc_id
            ]
            claim.evidence_doc_ids = remaining
            claim.evidence_quotes = remaining_quotes
            if _should_retract(remaining, remaining_quotes):
                transition_fact(
                    claim,
                    FactStatus.retracted,
                    "automatic:source_retraction",
                )
                close_at = (
                    removed_doc.event_time
                    if removed_doc is not None and removed_doc.event_time is not None
                    else utcnow()
                )
                if claim.valid_to is None:
                    claim.valid_to = close_at
                claim.invalidated_at = utcnow()
            claim.updated_at = utcnow()

        entities = (
            self._session.query(Entity)
            .filter(
                Entity.workspace_id == self._ws,
                Entity.evidence_doc_ids.contains([doc_id]),
            )
            .all()
        )
        for entity in entities:
            remaining = [evidence for evidence in (entity.evidence_doc_ids or []) if evidence != doc_id]
            entity.evidence_doc_ids = remaining
            entity.updated_at = utcnow()
