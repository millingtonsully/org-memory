"""Bounded multi-hop relationship traversal under viewer ACL."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text as sql

from org_memory.db.orm import Relationship
from org_memory.db.repositories.graph.base import GraphRepositoryBase
from org_memory.domain.models import Principal


class GraphTraversalMixin(GraphRepositoryBase):
    """Recursive path walks with depth caps and cycle guards."""

    def paths_from(
        self,
        *,
        start_type: str,
        start_id: str,
        principal: Principal,
        relationship_types: list[str] | None = None,
        max_depth: int = 2,
        limit: int = 50,
        as_of: datetime | None = None,
    ) -> list[dict]:
        """Bounded multi-hop paths; every edge's evidence must be viewer-visible."""
        max_depth = max(1, min(int(max_depth), 3))
        limit = max(1, min(int(limit), 200))
        rel_types = [r.strip().lower() for r in (relationship_types or []) if r.strip()]
        rows = self._session.execute(
            sql("""
                WITH RECURSIVE walk AS (
                    SELECT
                        r.relationship_id,
                        r.from_type AS start_type,
                        r.from_id AS start_id,
                        r.to_type AS end_type,
                        r.to_id AS end_id,
                        r.relationship_type,
                        r.evidence_doc_ids,
                        1 AS depth,
                        ARRAY[
                            r.from_type || ':' || r.from_id,
                            r.to_type || ':' || r.to_id
                        ]::text[] AS node_path,
                        ARRAY[r.relationship_id]::text[] AS edge_path
                    FROM relationships r
                    WHERE r.workspace_id = :workspace_id
                      AND r.status = 'active'
                      AND r.from_type = :start_type
                      AND r.from_id = :start_id
                      AND (
                          CAST(:rel_types AS text[]) IS NULL
                          OR r.relationship_type = ANY(CAST(:rel_types AS text[]))
                      )
                      AND (CAST(:as_of AS timestamptz) IS NULL
                           OR ((r.valid_from IS NULL OR r.valid_from <= :as_of)
                               AND (r.valid_to IS NULL OR r.valid_to > :as_of)))
                    UNION ALL
                    SELECT
                        r.relationship_id,
                        w.start_type,
                        w.start_id,
                        r.to_type,
                        r.to_id,
                        r.relationship_type,
                        r.evidence_doc_ids,
                        w.depth + 1,
                        w.node_path || (r.to_type || ':' || r.to_id),
                        w.edge_path || r.relationship_id
                    FROM walk w
                    JOIN relationships r
                      ON r.workspace_id = :workspace_id
                     AND r.status = 'active'
                     AND r.from_type = w.end_type
                     AND r.from_id = w.end_id
                     AND (
                          CAST(:rel_types AS text[]) IS NULL
                          OR r.relationship_type = ANY(CAST(:rel_types AS text[]))
                     )
                     AND (CAST(:as_of AS timestamptz) IS NULL
                          OR ((r.valid_from IS NULL OR r.valid_from <= :as_of)
                              AND (r.valid_to IS NULL OR r.valid_to > :as_of)))
                     AND NOT (r.to_type || ':' || r.to_id = ANY(w.node_path))
                    WHERE w.depth < :max_depth
                )
                SELECT *
                FROM walk
                ORDER BY depth, relationship_id
                LIMIT :limit
            """),
            {
                "workspace_id": self._ws,
                "start_type": start_type,
                "start_id": start_id,
                "rel_types": rel_types or None,
                "as_of": as_of,
                "max_depth": max_depth,
                "limit": limit * 5,
            },
        ).mappings()

        paths: list[dict] = []
        for row in rows:
            evidence = list(row["evidence_doc_ids"] or [])
            if not evidence:
                continue
            visible = self.visible_evidence_doc_ids(evidence, principal)
            if len(visible) != len(set(evidence)):
                continue
            # Re-check every edge on the path for ACL.
            edge_ids = list(row["edge_path"] or [])
            ok = True
            edges_out: list[dict] = []
            for edge_id in edge_ids:
                rel = self._session.get(Relationship, edge_id)
                if rel is None:
                    ok = False
                    break
                edge_evidence = list(rel.evidence_doc_ids or [])
                edge_visible = self.visible_evidence_doc_ids(edge_evidence, principal)
                if len(edge_visible) != len(set(edge_evidence)):
                    ok = False
                    break
                edges_out.append(
                    {
                        "relationship_id": rel.relationship_id,
                        "from": {"type": rel.from_type, "id": rel.from_id},
                        "to": {"type": rel.to_type, "id": rel.to_id},
                        "relationship_type": rel.relationship_type,
                        "evidence_doc_ids": edge_visible,
                    }
                )
            if not ok:
                continue
            paths.append(
                {
                    "nodes": list(row["node_path"] or []),
                    "edges": edges_out,
                    "depth": int(row["depth"]),
                }
            )
            if len(paths) >= limit:
                break
        return paths
