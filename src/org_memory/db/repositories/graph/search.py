"""Keyword fact candidates for hybrid retrieval (claims + relationships)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text as sql

from org_memory.db.orm import utcnow
from org_memory.db.repositories._common import _document_visibility_filters_sql
from org_memory.db.repositories.graph.base import GraphRepositoryBase
from org_memory.domain.models import Principal
from org_memory.services.temporality.grain import (
    resolve_validity_query_point,
    validity_as_of_sql,
)

# Shared temporal predicates for claim and relationship legs.
_VALIDITY_AS_OF = validity_as_of_sql("c")
_BELIEF_AS_OF = """
    (CAST(:believed_as_of AS timestamptz) IS NULL
     OR (c.recorded_at <= :believed_as_of
         AND (c.invalidated_at IS NULL
              OR c.invalidated_at > :believed_as_of)))
"""
_REL_VALIDITY_AS_OF = validity_as_of_sql("r")
_REL_BELIEF_AS_OF = """
    (CAST(:believed_as_of AS timestamptz) IS NULL
     OR (r.recorded_at <= :believed_as_of
         AND (r.invalidated_at IS NULL
              OR r.invalidated_at > :believed_as_of)))
"""


class GraphSearchMixin(GraphRepositoryBase):
    """FTS candidates whose entire evidence set is viewer-visible in SQL."""

    def fact_candidates(
        self,
        query_text: str,
        principal: Principal,
        limit: int,
        *,
        source_type: str | None = None,
        source_system: str | None = None,
        author_patterns: list[str] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        updated_from: datetime | None = None,
        updated_to: datetime | None = None,
        doc_id: str | None = None,
        author_person_ids: list[str] | None = None,
        about_person_ids: list[str] | None = None,
        about_doc_ids: list[str] | None = None,
        as_of: datetime | None = None,
        believed_as_of: datetime | None = None,
        as_of_grain: str | None = None,
    ) -> list[dict]:
        """Keyword candidates filtered by evidence ACL and temporal axes in SQL.

        When ``as_of`` / ``believed_as_of`` are unset, only currently active and
        currently valid facts participate (hybrid "true now"), using the same
        grain-expanded validity predicate as ``query_facts`` / ``paths_from``.
        Belief-only binds world validity at ``believed_as_of`` (day grain unless
        the host sets ``as_of_grain``); host ``as_of`` wins when both are set.
        When either axis is set, active and superseded rows whose windows contain
        the point are eligible. ``as_of_grain`` selects month/quarter/year bucket
        overlap when the query is coarse.
        """
        temporal = as_of is not None or believed_as_of is not None
        statuses = ["active", "superseded"] if temporal else ["active"]
        claim_temporal = f"{_VALIDITY_AS_OF} AND {_BELIEF_AS_OF}"
        rel_temporal = f"{_REL_VALIDITY_AS_OF} AND {_REL_BELIEF_AS_OF}"
        effective_as_of, effective_grain = resolve_validity_query_point(
            as_of=as_of,
            believed_as_of=believed_as_of,
            as_of_grain=as_of_grain,
            now=utcnow(),
        )

        rows = self._session.execute(
            sql(f"""
                WITH query AS (
                    SELECT websearch_to_tsquery('english', :query_text) AS q
                ),
                visible_docs AS (
                    SELECT d.doc_id
                    FROM documents d
                    WHERE {_document_visibility_filters_sql("d")}
                ),
                candidates AS (
                    SELECT
                        c.claim_id AS fact_id,
                        'claim'::text AS fact_type,
                        c.predicate || ': ' || c.object_text AS fact_text,
                        c.predicate AS predicate,
                        c.confidence,
                        c.status AS status,
                        c.valid_from AS valid_from,
                        c.recorded_at AS recorded_at,
                        evidence.doc_ids AS evidence_doc_ids,
                        c.evidence_quotes AS evidence_quotes,
                        ts_rank(
                            to_tsvector(
                                'english',
                                coalesce(c.predicate, '') || ' ' ||
                                coalesce(c.object_text, '')
                            ),
                            query.q
                        ) AS keyword_score
                    FROM claims c
                    CROSS JOIN query
                    CROSS JOIN LATERAL (
                        SELECT array_agg(v.doc_id) AS doc_ids
                        FROM visible_docs v
                        WHERE v.doc_id = ANY(c.evidence_doc_ids)
                    ) evidence
                    WHERE c.workspace_id = :workspace_id
                      AND c.status = ANY(CAST(:statuses AS text[]))
                      AND {claim_temporal}
                      AND cardinality(c.evidence_doc_ids) > 0
                      AND cardinality(evidence.doc_ids) = (
                          SELECT count(DISTINCT e) FROM unnest(c.evidence_doc_ids) AS e
                      )
                      AND to_tsvector(
                            'english',
                            coalesce(c.predicate, '') || ' ' ||
                            coalesce(c.object_text, '')
                          ) @@ query.q
                    UNION ALL
                    SELECT
                        r.relationship_id AS fact_id,
                        'relationship'::text AS fact_type,
                        trim(
                            r.from_type || ':' || r.from_id || ' ' ||
                            r.relationship_type || ' ' ||
                            r.to_type || ':' || r.to_id
                        ) AS fact_text,
                        r.relationship_type AS predicate,
                        r.confidence,
                        r.status AS status,
                        r.valid_from AS valid_from,
                        r.recorded_at AS recorded_at,
                        evidence.doc_ids AS evidence_doc_ids,
                        r.evidence_quotes AS evidence_quotes,
                        ts_rank(
                            to_tsvector(
                                'english',
                                coalesce(r.from_label, '') || ' ' ||
                                coalesce(r.relationship_type, '') || ' ' ||
                                coalesce(r.to_label, '')
                            ),
                            query.q
                        ) AS keyword_score
                    FROM relationships r
                    CROSS JOIN query
                    CROSS JOIN LATERAL (
                        SELECT array_agg(v.doc_id) AS doc_ids
                        FROM visible_docs v
                        WHERE v.doc_id = ANY(r.evidence_doc_ids)
                    ) evidence
                    WHERE r.workspace_id = :workspace_id
                      AND r.status = ANY(CAST(:statuses AS text[]))
                      AND {rel_temporal}
                      AND cardinality(r.evidence_doc_ids) > 0
                      AND cardinality(evidence.doc_ids) = (
                          SELECT count(DISTINCT e) FROM unnest(r.evidence_doc_ids) AS e
                      )
                      AND to_tsvector(
                            'english',
                            coalesce(r.from_label, '') || ' ' ||
                            coalesce(r.relationship_type, '') || ' ' ||
                            coalesce(r.to_label, '')
                          ) @@ query.q
                )
                SELECT *
                FROM candidates
                ORDER BY keyword_score DESC
                LIMIT :limit
            """),
            {
                "query_text": query_text,
                "workspace_id": self._ws,
                "viewer_principals": principal.all_principals(),
                "source_type": source_type,
                "source_system": source_system,
                "author_patterns": author_patterns,
                "author_person_ids": author_person_ids,
                "about_person_ids": about_person_ids,
                "about_doc_ids": about_doc_ids,
                "date_from": date_from,
                "date_to": date_to,
                "updated_from": updated_from,
                "updated_to": updated_to,
                "doc_id": doc_id,
                "statuses": statuses,
                "as_of": effective_as_of,
                "as_of_grain": effective_grain,
                "believed_as_of": believed_as_of,
                "limit": limit,
            },
        ).mappings()
        candidates: list[dict] = []
        for rank, row in enumerate(rows, start=1):
            visible_ids = set(row["evidence_doc_ids"] or [])
            quotes = [
                quote
                for quote in (row["evidence_quotes"] or [])
                if quote.get("doc_id") in visible_ids
            ]
            candidates.append(
                {
                    "fact_id": row["fact_id"],
                    "fact_type": row["fact_type"],
                    "text": row["fact_text"],
                    "predicate": row["predicate"],
                    "confidence": float(row["confidence"]),
                    "status": row["status"] or "active",
                    "valid_from": row["valid_from"],
                    "recorded_at": row["recorded_at"],
                    "evidence_doc_ids": list(row["evidence_doc_ids"]),
                    "evidence_quotes": quotes,
                    "keyword_score": float(row["keyword_score"]),
                    "rank": rank,
                }
            )
        return candidates
