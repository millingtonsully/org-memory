"""Bounded multi-hop relationship traversal under viewer ACL."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text as sql

from org_memory.db.orm import Relationship, utcnow
from org_memory.db.repositories.graph.base import GraphRepositoryBase
from org_memory.domain.models import Principal
from org_memory.services.temporality.grain import (
    belief_as_of_sql,
    resolve_validity_query_point,
    temporal_read_statuses,
    validity_as_of_sql,
)

_REL_VALIDITY_AS_OF = validity_as_of_sql("r")
_REL_BELIEF_AS_OF = belief_as_of_sql("r")

# All-visible evidence: every evidence doc must be in visible_docs.
_EDGE_ACL_SQL = """
    cardinality(r.evidence_doc_ids) > 0
    AND (
        SELECT count(DISTINCT e)
        FROM unnest(r.evidence_doc_ids) AS e
    ) = (
        SELECT count(*)
        FROM visible_docs v
        WHERE v.doc_id = ANY(r.evidence_doc_ids)
    )
"""


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
        believed_as_of: datetime | None = None,
        as_of_grain: str | None = None,
    ) -> dict:
        """Bounded multi-hop paths; every edge's evidence must be viewer-visible.

        Returns a dict with ``paths`` plus limit metadata:
        ``returned``, ``limit``, ``max_depth``, ``truncated``, ``capped``.

        World-time (``as_of``) and system-time (``believed_as_of``) filters match
        claim reads. When either temporal point is set, superseded edges whose
        windows contain the point are eligible; otherwise only active edges are.

        All-visible evidence ACL is enforced inside the recursive walk so private
        edges never consume the path budget.
        """
        requested_max_depth = int(max_depth)
        requested_limit = int(limit)
        effective_depth = max(1, min(requested_max_depth, 3))
        effective_limit = max(1, min(requested_limit, 200))
        capped = (
            effective_depth != requested_max_depth or effective_limit != requested_limit
        )
        rel_types = [r.strip().lower() for r in (relationship_types or []) if r.strip()]
        statuses = temporal_read_statuses(as_of, believed_as_of)
        # Current / belief-only: resolve_validity_query_point binds the world
        # clock (now, or believed_as_of when as_of is omitted) with day grain.
        effective_as_of, effective_grain = resolve_validity_query_point(
            as_of=as_of,
            believed_as_of=believed_as_of,
            as_of_grain=as_of_grain,
            now=utcnow(),
        )
        # Fetch one extra row so truncated is accurate without a second count query.
        fetch_limit = effective_limit + 1
        rows = list(
            self._session.execute(
                sql(f"""
                    WITH RECURSIVE visible_docs AS (
                        SELECT d.doc_id
                        FROM documents d
                        WHERE d.workspace_id = :workspace_id
                          AND d.deleted = false
                          AND (
                              d.org_visible = true
                              OR d.allowed_principals
                                 && CAST(:viewer_principals AS text[])
                          )
                    ),
                    walk AS (
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
                          AND r.status = ANY(CAST(:statuses AS text[]))
                          AND r.from_type = :start_type
                          AND r.from_id = :start_id
                          AND (
                              CAST(:rel_types AS text[]) IS NULL
                              OR r.relationship_type = ANY(CAST(:rel_types AS text[]))
                          )
                          AND {_REL_VALIDITY_AS_OF}
                          AND {_REL_BELIEF_AS_OF}
                          AND {_EDGE_ACL_SQL}
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
                         AND r.status = ANY(CAST(:statuses AS text[]))
                         AND r.from_type = w.end_type
                         AND r.from_id = w.end_id
                         AND (
                              CAST(:rel_types AS text[]) IS NULL
                              OR r.relationship_type = ANY(CAST(:rel_types AS text[]))
                         )
                         AND {_REL_VALIDITY_AS_OF}
                         AND {_REL_BELIEF_AS_OF}
                         AND NOT (r.to_type || ':' || r.to_id = ANY(w.node_path))
                         AND {_EDGE_ACL_SQL}
                        WHERE w.depth < :max_depth
                    )
                    SELECT *
                    FROM walk
                    ORDER BY depth, relationship_id
                    LIMIT :limit
                """),
                {
                    "workspace_id": self._ws,
                    "viewer_principals": principal.all_principals(),
                    "start_type": start_type,
                    "start_id": start_id,
                    "rel_types": rel_types or None,
                    "statuses": statuses,
                    "as_of": effective_as_of,
                    "as_of_grain": effective_grain,
                    "believed_as_of": believed_as_of,
                    "max_depth": effective_depth,
                    "limit": fetch_limit,
                },
            ).mappings()
        )

        edge_ids: list[str] = []
        seen_edges: set[str] = set()
        for row in rows:
            for edge_id in row["edge_path"] or []:
                if edge_id not in seen_edges:
                    seen_edges.add(edge_id)
                    edge_ids.append(edge_id)

        rel_by_id: dict[str, Relationship] = {}
        if edge_ids:
            loaded = (
                self._session.query(Relationship)
                .filter(Relationship.relationship_id.in_(edge_ids))
                .all()
            )
            rel_by_id = {rel.relationship_id: rel for rel in loaded}

        accepted: list[dict] = []
        for row in rows:
            edge_ids_for_path = list(row["edge_path"] or [])
            if not edge_ids_for_path:
                continue
            edges_out: list[dict] = []
            ok = True
            for edge_id in edge_ids_for_path:
                edge_rel = rel_by_id.get(edge_id)
                if edge_rel is None:
                    ok = False
                    break
                edge_evidence = list(edge_rel.evidence_doc_ids or [])
                edges_out.append(
                    {
                        "relationship_id": edge_rel.relationship_id,
                        "from": {"type": edge_rel.from_type, "id": edge_rel.from_id},
                        "to": {"type": edge_rel.to_type, "id": edge_rel.to_id},
                        "relationship_type": edge_rel.relationship_type,
                        "evidence_doc_ids": edge_evidence,
                    }
                )
            if not ok:
                continue
            accepted.append(
                {
                    "nodes": list(row["node_path"] or []),
                    "edges": edges_out,
                    "depth": int(row["depth"]),
                }
            )

        truncated = len(accepted) > effective_limit
        paths = accepted[:effective_limit]
        return {
            "paths": paths,
            "returned": len(paths),
            "limit": effective_limit,
            "max_depth": effective_depth,
            "truncated": truncated,
            "capped": capped,
        }
