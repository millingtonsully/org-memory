"""SQL repositories package modules.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from org_memory.core.settings import get_settings
from org_memory.db.orm import (
    Claim,
    Document,
    Entity,
    Relationship,
    utcnow,
)
from org_memory.domain.fact_lifecycle import FactStatus, transition_fact
from org_memory.domain.models import Principal


class GraphRepository:
    """Entities, relationships, and claims. Workspace-scoped."""

    def __init__(self, session: Session):
        self._session = session
        self._ws = get_settings().workspace_id

    @staticmethod
    def normalize_name(name: str) -> str:
        return " ".join(name.lower().split())

    def fact_candidates(
        self,
        query_text: str,
        principal: Principal,
        limit: int,
        *,
        source_type: str | None = None,
        author_patterns: list[str] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        updated_from: datetime | None = None,
        updated_to: datetime | None = None,
        doc_id: str | None = None,
        author_person_ids: list[str] | None = None,
        about_person_ids: list[str] | None = None,
    ) -> list[dict]:
        """Keyword candidates filtered by current evidence ACL in SQL.
        """
        rows = self._session.execute(
            sql("""
                WITH query AS (
                    SELECT websearch_to_tsquery('english', :query_text) AS q
                ),
                visible_docs AS (
                    SELECT d.doc_id
                    FROM documents d
                    WHERE d.workspace_id = :workspace_id
                      AND d.deleted = false
                      AND (
                          d.org_visible = true
                          OR d.allowed_principals
                             && CAST(:viewer_principals AS text[])
                      )
                      AND (
                          CAST(:source_type AS text) IS NULL
                          OR d.source_type = :source_type
                      )
                      AND (
                          CAST(:author_patterns AS text[]) IS NULL
                          OR d.author_display_name
                             ILIKE ANY(CAST(:author_patterns AS text[]))
                      )
                      AND (
                          CAST(:author_person_ids AS text[]) IS NULL
                          OR EXISTS (
                              SELECT 1 FROM document_participants dp
                              WHERE dp.doc_id = d.doc_id
                                AND dp.person_id = ANY(CAST(:author_person_ids AS text[]))
                                AND dp.role = 'author'
                          )
                      )
                      AND (
                          CAST(:about_person_ids AS text[]) IS NULL
                          OR EXISTS (
                              SELECT 1 FROM document_participants dp
                              WHERE dp.doc_id = d.doc_id
                                AND dp.person_id = ANY(CAST(:about_person_ids AS text[]))
                          )
                      )
                      AND (
                          CAST(:date_from AS timestamptz) IS NULL
                          OR d.event_time >= :date_from
                      )
                      AND (
                          CAST(:date_to AS timestamptz) IS NULL
                          OR d.event_time <= :date_to
                      )
                      AND (
                          CAST(:updated_from AS timestamptz) IS NULL
                          OR d.updated_at >= :updated_from
                      )
                      AND (
                          CAST(:updated_to AS timestamptz) IS NULL
                          OR d.updated_at <= :updated_to
                      )
                      AND (
                          CAST(:doc_id AS text) IS NULL
                          OR d.doc_id = :doc_id
                      )
                ),
                candidates AS (
                    SELECT
                        c.claim_id AS fact_id,
                        'claim'::text AS fact_type,
                        c.predicate || ': ' || c.object_text AS fact_text,
                        c.confidence,
                        evidence.doc_ids AS evidence_doc_ids,
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
                            r.from_label || ' ' || r.relationship_type || ' ' || r.to_label
                        ) AS fact_text,
                        r.confidence,
                        evidence.doc_ids AS evidence_doc_ids,
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
                "author_patterns": author_patterns,
                "author_person_ids": author_person_ids,
                "about_person_ids": about_person_ids,
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
            candidates.append(
                {
                    "fact_id": row["fact_id"],
                    "fact_type": row["fact_type"],
                    "text": row["fact_text"],
                    "confidence": float(row["confidence"]),
                    "evidence_doc_ids": list(row["evidence_doc_ids"]),
                    "keyword_score": float(row["keyword_score"]),
                    "rank": rank,
                }
            )
        return candidates

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

    def search_entities(self, name: str, limit: int = 5) -> list[Entity]:
        return (
            self._session.query(Entity)
            .filter(
                Entity.workspace_id == self._ws,
                Entity.name.ilike(f"%{name}%"),
            )
            .limit(limit)
            .all()
        )

    def search_entities_for_viewer(
        self, name: str, principal: Principal, limit: int = 5
    ) -> list[tuple[Entity, list[str]]]:
        """Return entities whose *entire* evidence set is visible to this viewer.

        All-visible (not any-visible): mixed private/public evidence must not
        surface entity name/description/attributes to a viewer who cannot see
        every supporting document. Empty evidence never surfaces.
        """
        visible: list[tuple[Entity, list[str]]] = []
        for entity in self.search_entities(name, limit=limit * 3):
            evidence = list(entity.evidence_doc_ids or [])
            if not evidence:
                continue
            doc_ids = self.visible_evidence_doc_ids(evidence, principal)
            if len(doc_ids) == len(set(evidence)):
                visible.append((entity, doc_ids))
            if len(visible) >= limit:
                break
        return visible

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
            if rel.status == FactStatus.active.value and existing.status != FactStatus.active.value:
                transition_fact(
                    existing,
                    FactStatus.active,
                    rel.decided_by or "automatic:confidence_gate",
                )
            elif existing.status == FactStatus.retracted.value and rel.status == FactStatus.proposed.value:
                transition_fact(existing, FactStatus.proposed, "")
            existing.updated_at = utcnow()
            return existing
        rel.workspace_id = self._ws
        self._session.add(rel)
        self._session.flush()
        return rel

    def relationships_for(
        self, node_type: str, node_id: str, status: str = "active", as_of: datetime | None = None
    ) -> list[Relationship]:
        """Edges attached to a node. When as_of is provided, filter by validity window."""
        q = self._session.query(Relationship).filter(
            Relationship.workspace_id == self._ws,
            Relationship.status == status,
            (
                (Relationship.from_type == node_type) & (Relationship.from_id == node_id)
                | (Relationship.to_type == node_type) & (Relationship.to_id == node_id)
            ),
        )
        if as_of is not None:
            q = q.filter(
                (Relationship.valid_from.is_(None)) | (Relationship.valid_from <= as_of),
                (Relationship.valid_to.is_(None)) | (Relationship.valid_to >= as_of),
            )
        return q.order_by(Relationship.created_at).all()

    def relationships_for_viewer(
        self,
        node_type: str,
        node_id: str,
        principal: Principal,
        status: str = "active",
        as_of: datetime | None = None,
    ) -> list[tuple[Relationship, list[str]]]:
        """Return edges whose entire evidence set is visible to this viewer.
        """
        visible: list[tuple[Relationship, list[str]]] = []
        for rel in self.relationships_for(node_type, node_id, status=status, as_of=as_of):
            evidence = list(rel.evidence_doc_ids or [])
            if not evidence:
                continue
            doc_ids = self.visible_evidence_doc_ids(evidence, principal)
            if len(doc_ids) == len(set(evidence)):
                visible.append((rel, doc_ids))
        return visible

    def add_claim(self, claim: Claim) -> Claim:
        """Dedupe facts and advance a proposal when stronger evidence arrives."""
        existing = (
            self._session.query(Claim)
            .filter(
                Claim.workspace_id == self._ws,
                Claim.subject_type == claim.subject_type,
                Claim.subject_id == claim.subject_id,
                Claim.predicate == claim.predicate,
                Claim.object_text == claim.object_text,
                Claim.status.in_(["proposed", "active", "retracted"]),
            )
            .first()
        )
        if existing is not None:
            merged = set(existing.evidence_doc_ids) | set(claim.evidence_doc_ids)
            existing.evidence_doc_ids = sorted(merged)
            quotes = {
                (str(item.get("doc_id", "")), str(item.get("quote", ""))): item
                for item in [*(existing.evidence_quotes or []), *(claim.evidence_quotes or [])]
            }
            existing.evidence_quotes = list(quotes.values())
            existing.confidence = max(existing.confidence, claim.confidence)
            if claim.status == FactStatus.active.value and existing.status != FactStatus.active.value:
                transition_fact(
                    existing,
                    FactStatus.active,
                    claim.decided_by or "automatic:confidence_gate",
                )
            elif existing.status == FactStatus.retracted.value and claim.status == FactStatus.proposed.value:
                transition_fact(existing, FactStatus.proposed, "")
            existing.updated_at = utcnow()
            return existing
        claim.workspace_id = self._ws
        self._session.add(claim)
        self._session.flush()
        return claim

    def active_object_texts(self, subject_type: str, subject_id: str, predicate: str) -> list[str]:
        """Distinct object values currently active for one (subject, predicate)."""
        rows = (
            self._session.query(Claim.object_text)
            .filter(
                Claim.workspace_id == self._ws,
                Claim.subject_type == subject_type,
                Claim.subject_id == subject_id,
                Claim.predicate == predicate,
                Claim.status == FactStatus.active.value,
            )
            .distinct()
            .all()
        )
        return [row.object_text for row in rows]

    def active_claims_for_slot_locked(
        self, subject_type: str, subject_id: str, predicate: str
    ) -> list[Claim]:
        """Lock and return active claims for one slot for conflict resolution."""
        return (
            self._session.query(Claim)
            .filter(
                Claim.workspace_id == self._ws,
                Claim.subject_type == subject_type,
                Claim.subject_id == subject_id,
                Claim.predicate == predicate,
                Claim.status == FactStatus.active.value,
            )
            .order_by(Claim.claim_id)
            .with_for_update()
            .all()
        )

    def latest_evidence_time(self, evidence_doc_ids: list[str]) -> datetime | None:
        """Newest event_time among a claim's still-live evidence documents."""
        if not evidence_doc_ids:
            return None
        row = (
            self._session.query(func.max(Document.event_time))
            .filter(
                Document.workspace_id == self._ws,
                Document.doc_id.in_(evidence_doc_ids),
                Document.deleted == False,  # noqa: E712
            )
            .first()
        )
        return row[0] if row is not None else None

    def supersede_claim(self, claim: Claim, superseded_by_claim_id: str, decided_by: str) -> None:
        """Retire a losing claim in a mutually-exclusive slot (retained, not deleted)."""
        transition_fact(claim, FactStatus.superseded, decided_by)
        claim.superseded_by_claim_id = superseded_by_claim_id

    def claims_for(
        self, subject_type: str, subject_id: str, statuses: list[str] | None = None
    ) -> list[Claim]:
        q = self._session.query(Claim).filter(
            Claim.workspace_id == self._ws,
            Claim.subject_type == subject_type,
            Claim.subject_id == subject_id,
        )
        if statuses:
            q = q.filter(Claim.status.in_(statuses))
        return q.order_by(Claim.created_at.desc()).all()

    def claims_for_viewer(
        self,
        subject_type: str,
        subject_id: str,
        principal: Principal,
        statuses: list[str] | None = None,
    ) -> list[tuple[Claim, list[str]]]:
        """Return claims whose *entire* evidence set is visible to this viewer.
        """
        visible: list[tuple[Claim, list[str]]] = []
        for claim in self.claims_for(subject_type, subject_id, statuses=statuses):
            evidence = list(claim.evidence_doc_ids or [])
            if not evidence:
                continue
            doc_ids = self.visible_evidence_doc_ids(evidence, principal)
            if len(doc_ids) == len(set(evidence)):
                visible.append((claim, doc_ids))
        return visible

    def visible_evidence_doc_ids(self, evidence_doc_ids: list[str], principal: Principal) -> list[str]:
        """Intersect evidence with current document ACLs.
        """
        if not evidence_doc_ids:
            return []
        rows = (
            self._session.query(Document.doc_id)
            .filter(
                Document.workspace_id == self._ws,
                Document.doc_id.in_(evidence_doc_ids),
                Document.deleted == False,  # noqa: E712
                (
                    (Document.org_visible == True)  # noqa: E712
                    | Document.allowed_principals.overlap(principal.all_principals())
                ),
            )
            .all()
        )
        allowed = {row.doc_id for row in rows}
        return [doc_id for doc_id in evidence_doc_ids if doc_id in allowed]

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
        """
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
            relationship.evidence_doc_ids = remaining
            relationship.evidence_quotes = [
                quote for quote in (relationship.evidence_quotes or []) if quote.get("doc_id") != doc_id
            ]
            if not remaining:
                transition_fact(
                    relationship,
                    FactStatus.retracted,
                    "automatic:source_retraction",
                )
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
            claim.evidence_doc_ids = remaining
            claim.evidence_quotes = [
                quote for quote in (claim.evidence_quotes or []) if quote.get("doc_id") != doc_id
            ]
            if not remaining:
                transition_fact(
                    claim,
                    FactStatus.retracted,
                    "automatic:source_retraction",
                )
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


