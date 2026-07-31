"""Keyword fact candidates for hybrid retrieval (claims + relationships)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text as sql

from org_memory.db.repositories._common import _document_visibility_filters_sql
from org_memory.db.repositories.graph.base import GraphRepositoryBase
from org_memory.domain.models import Principal


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
    ) -> list[dict]:
        """Keyword candidates filtered by current evidence ACL in SQL.
        """
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
                      AND c.status = 'active'
                      AND (c.valid_from IS NULL OR c.valid_from <= now())
                      AND (c.valid_to IS NULL OR c.valid_to > now())
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
                      AND r.status = 'active'
                      AND (r.valid_from IS NULL OR r.valid_from <= now())
                      AND (r.valid_to IS NULL OR r.valid_to > now())
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
                    "valid_from": row["valid_from"],
                    "recorded_at": row["recorded_at"],
                    "evidence_doc_ids": list(row["evidence_doc_ids"]),
                    "evidence_quotes": quotes,
                    "keyword_score": float(row["keyword_score"]),
                    "rank": rank,
                }
            )
        return candidates
