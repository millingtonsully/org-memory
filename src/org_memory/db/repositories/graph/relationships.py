"""Relationship mutations and viewer-scoped edge reads."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text as sql

from org_memory.db.orm import Relationship, utcnow
from org_memory.db.repositories.graph.base import (
    VISIBLE_DOCS_CTE,
    GraphRepositoryBase,
    all_visible_sql,
    evidence_lateral_sql,
)
from org_memory.domain.fact_lifecycle import FactStatus, transition_fact
from org_memory.domain.models import Principal
from org_memory.services.temporality.grain import (
    belief_as_of_sql,
    resolve_validity_query_point,
    validity_as_of_sql,
)
from org_memory.services.temporality.merge import merge_temporal_fields

_REL_VALIDITY_AS_OF = validity_as_of_sql("r")
_REL_BELIEF_AS_OF = belief_as_of_sql("r")
_REL_ALL_VISIBLE_SQL = all_visible_sql("r")
_REL_EVIDENCE_LATERAL = evidence_lateral_sql("r")


class GraphRelationshipsMixin(GraphRepositoryBase):
    """Relationship lifecycle and SQL viewer-scoped edge reads."""

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
        rows = self._session.execute(
            sql(f"""
                WITH {VISIBLE_DOCS_CTE}
                SELECT r.relationship_id, evidence.doc_ids AS evidence_doc_ids
                FROM relationships r
                {_REL_EVIDENCE_LATERAL}
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
