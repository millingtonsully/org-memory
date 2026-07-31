"""Procedural memory rows: stored how-to knowledge with embeddings and ACL."""

from __future__ import annotations

import json

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from org_memory.core.settings import get_settings
from org_memory.db.orm import (
    ProceduralMemory,
)
from org_memory.domain.models import Principal


class ProceduralMemoryRepository:
    def __init__(self, session: Session):
        self._session = session
        self._ws = get_settings().workspace_id

    def add(self, memory: ProceduralMemory) -> None:
        memory.workspace_id = self._ws
        self._session.add(memory)

    def active_for_key(
        self, agent_id: str, procedure_key: str, created_by_principal: str
    ) -> list[ProceduralMemory]:
        return (
            self._session.query(ProceduralMemory)
            .filter(
                ProceduralMemory.workspace_id == self._ws,
                ProceduralMemory.agent_id == agent_id,
                ProceduralMemory.procedure_key == procedure_key,
                ProceduralMemory.created_by_principal == created_by_principal,
                ProceduralMemory.status == "active",
            )
            .all()
        )

    def search_candidates(
        self,
        *,
        query_text: str,
        query_embedding: list[float],
        embedding_model: str,
        principal: Principal,
        agent_id: str | None,
        limit: int,
        rrf_k: int,
    ) -> list[ProceduralMemory]:
        rows = self._session.execute(
            sql("""
                WITH vector_ranked AS (
                    SELECT
                        memory_id,
                        row_number() OVER (
                            ORDER BY embedding <=> CAST(:embedding AS vector)
                        ) AS rank
                    FROM procedural_memories
                    WHERE workspace_id = :workspace_id
                      AND status = 'active'
                      AND embedding IS NOT NULL
                      AND embedding_model = :embedding_model
                      AND (CAST(:agent_id AS text) IS NULL OR agent_id = :agent_id)
                      AND (
                          org_visible = true
                          OR allowed_principals && CAST(:viewer_principals AS text[])
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM unnest(evidence_doc_ids) AS evidence(doc_id)
                          WHERE NOT EXISTS (
                              SELECT 1
                              FROM documents d
                              WHERE d.doc_id = evidence.doc_id
                                AND d.workspace_id = :workspace_id
                                AND d.deleted = false
                                AND (
                                    d.org_visible = true
                                    OR d.allowed_principals
                                       && CAST(:viewer_principals AS text[])
                                )
                          )
                      )
                    ORDER BY embedding <=> CAST(:embedding AS vector)
                    LIMIT :limit
                ),
                keyword_ranked AS (
                    SELECT
                        memory_id,
                        row_number() OVER (
                            ORDER BY ts_rank(
                                to_tsvector(
                                    'english',
                                    coalesce(objective, '') || ' ' || coalesce(summary, '')
                                ),
                                websearch_to_tsquery('english', :query_text)
                            ) DESC
                        ) AS rank
                    FROM procedural_memories
                    WHERE workspace_id = :workspace_id
                      AND status = 'active'
                      AND (CAST(:agent_id AS text) IS NULL OR agent_id = :agent_id)
                      AND (
                          org_visible = true
                          OR allowed_principals && CAST(:viewer_principals AS text[])
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM unnest(evidence_doc_ids) AS evidence(doc_id)
                          WHERE NOT EXISTS (
                              SELECT 1
                              FROM documents d
                              WHERE d.doc_id = evidence.doc_id
                                AND d.workspace_id = :workspace_id
                                AND d.deleted = false
                                AND (
                                    d.org_visible = true
                                    OR d.allowed_principals
                                       && CAST(:viewer_principals AS text[])
                                )
                          )
                      )
                      AND to_tsvector(
                            'english',
                            coalesce(objective, '') || ' ' || coalesce(summary, '')
                          ) @@ websearch_to_tsquery('english', :query_text)
                    ORDER BY ts_rank(
                        to_tsvector(
                            'english',
                            coalesce(objective, '') || ' ' || coalesce(summary, '')
                        ),
                        websearch_to_tsquery('english', :query_text)
                    ) DESC
                    LIMIT :limit
                ),
                fused AS (
                    SELECT memory_id, sum(score) AS score
                    FROM (
                        SELECT memory_id, 1.0 / (:rrf_k + rank) AS score
                        FROM vector_ranked
                        UNION ALL
                        SELECT memory_id, 1.0 / (:rrf_k + rank) AS score
                        FROM keyword_ranked
                    ) ranked
                    GROUP BY memory_id
                    ORDER BY score DESC
                    LIMIT :limit
                )
                SELECT memory_id FROM fused ORDER BY score DESC
            """),
            {
                "embedding": json.dumps(query_embedding),
                "embedding_model": embedding_model,
                "query_text": query_text,
                "workspace_id": self._ws,
                "agent_id": agent_id,
                "viewer_principals": principal.all_principals(),
                "limit": limit,
                "rrf_k": rrf_k,
            },
        ).fetchall()
        result: list[ProceduralMemory] = []
        for row in rows:
            memory = self._session.get(ProceduralMemory, row.memory_id)
            if memory is not None and memory.workspace_id == self._ws:
                result.append(memory)
        return result


