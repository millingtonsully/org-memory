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
        believed_as_of: datetime | None = None,
    ) -> dict:
        """Bounded multi-hop paths; every edge's evidence must be viewer-visible.

        Returns a dict with ``paths`` plus limit metadata:
        ``returned``, ``limit``, ``max_depth``, ``truncated``, ``capped``.

        World-time (``as_of``) and system-time (``believed_as_of``) filters match
        claim reads. When either temporal point is set, superseded edges whose
        windows contain the point are eligible; otherwise only active edges are.
        """
        requested_max_depth = int(max_depth)
        requested_limit = int(limit)
        effective_depth = max(1, min(requested_max_depth, 3))
        effective_limit = max(1, min(requested_limit, 200))
        capped = (
            effective_depth != requested_max_depth or effective_limit != requested_limit
        )
        rel_types = [r.strip().lower() for r in (relationship_types or []) if r.strip()]
        statuses = (
            ["active", "superseded"]
            if as_of is not None or believed_as_of is not None
            else ["active"]
        )
        rows = list(
            self._session.execute(
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
                          AND r.status = ANY(CAST(:statuses AS text[]))
                          AND r.from_type = :start_type
                          AND r.from_id = :start_id
                          AND (
                              CAST(:rel_types AS text[]) IS NULL
                              OR r.relationship_type = ANY(CAST(:rel_types AS text[]))
                          )
                          AND (CAST(:as_of AS timestamptz) IS NULL
                               OR ((r.valid_from IS NULL OR r.valid_from <= :as_of)
                                   AND (r.valid_to IS NULL OR r.valid_to > :as_of)))
                          AND (CAST(:believed_as_of AS timestamptz) IS NULL
                               OR (r.recorded_at <= :believed_as_of
                                   AND (r.invalidated_at IS NULL
                                        OR r.invalidated_at > :believed_as_of)))
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
                         AND (CAST(:as_of AS timestamptz) IS NULL
                              OR ((r.valid_from IS NULL OR r.valid_from <= :as_of)
                                  AND (r.valid_to IS NULL OR r.valid_to > :as_of)))
                         AND (CAST(:believed_as_of AS timestamptz) IS NULL
                              OR (r.recorded_at <= :believed_as_of
                                  AND (r.invalidated_at IS NULL
                                       OR r.invalidated_at > :believed_as_of)))
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
                    "statuses": statuses,
                    "as_of": as_of,
                    "believed_as_of": believed_as_of,
                    "max_depth": effective_depth,
                    "limit": effective_limit * 5,
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

        all_evidence: list[str] = []
        seen_docs: set[str] = set()
        for rel in rel_by_id.values():
            for doc_id in rel.evidence_doc_ids or []:
                if doc_id not in seen_docs:
                    seen_docs.add(doc_id)
                    all_evidence.append(doc_id)
        visible_set = set(self.visible_evidence_doc_ids(all_evidence, principal))

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
                if not edge_evidence or not set(edge_evidence) <= visible_set:
                    ok = False
                    break
                edges_out.append(
                    {
                        "relationship_id": edge_rel.relationship_id,
                        "from": {"type": edge_rel.from_type, "id": edge_rel.from_id},
                        "to": {"type": edge_rel.to_type, "id": edge_rel.to_id},
                        "relationship_type": edge_rel.relationship_type,
                        "evidence_doc_ids": [
                            doc_id for doc_id in edge_evidence if doc_id in visible_set
                        ],
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
