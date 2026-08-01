"""Entity and relationship writes plus viewer-scoped edge/entity reads.

Claim lifecycle lives in `claims.py` (`GraphClaimsMixin`).
"""

from __future__ import annotations

from datetime import datetime

from org_memory.db.orm import Claim, Document, Entity, Relationship, utcnow
from org_memory.db.repositories.graph.base import GraphRepositoryBase
from org_memory.domain.fact_lifecycle import FactStatus, transition_fact
from org_memory.domain.models import Principal


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
        every supporting document. Empty evidence never surfaces.
        """
        visible: list[tuple[Entity, list[str]]] = []
        for entity in self.search_entities(name, limit=limit * 3, entity_type=entity_type):
            evidence = list(entity.evidence_doc_ids or [])
            if not evidence:
                continue
            doc_ids = self.visible_evidence_doc_ids(evidence, principal)
            if len(doc_ids) == len(set(evidence)):
                visible.append((entity, doc_ids))
            if len(visible) >= limit:
                break
        return visible

    def list_entities_for_viewer(
        self,
        principal: Principal,
        *,
        entity_type: str,
        limit: int = 50,
    ) -> list[tuple[Entity, list[str]]]:
        """Browse visible entities of one type (bounded; ordered by name)."""
        rows = (
            self._session.query(Entity)
            .filter(
                Entity.workspace_id == self._ws,
                Entity.entity_type == entity_type.strip().lower(),
            )
            .order_by(Entity.name.asc())
            .limit(max(limit * 5, limit))
            .all()
        )
        visible: list[tuple[Entity, list[str]]] = []
        for entity in rows:
            evidence = list(entity.evidence_doc_ids or [])
            if not evidence:
                continue
            doc_ids = self.visible_evidence_doc_ids(evidence, principal)
            if len(doc_ids) != len(set(evidence)):
                continue
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
        self,
        node_type: str,
        node_id: str,
        status: str = "active",
        as_of: datetime | None = None,
        believed_as_of: datetime | None = None,
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
                (Relationship.valid_to.is_(None)) | (Relationship.valid_to > as_of),
            )
        if believed_as_of is not None:
            q = q.filter(
                Relationship.recorded_at <= believed_as_of,
                (Relationship.invalidated_at.is_(None))
                | (Relationship.invalidated_at > believed_as_of),
            )
        return q.order_by(Relationship.created_at).all()

    def relationships_for_viewer(
        self,
        node_type: str,
        node_id: str,
        principal: Principal,
        status: str = "active",
        as_of: datetime | None = None,
        believed_as_of: datetime | None = None,
    ) -> list[tuple[Relationship, list[str]]]:
        """Return edges whose entire evidence set is visible to this viewer.
        """
        visible: list[tuple[Relationship, list[str]]] = []
        for rel in self.relationships_for(
            node_type,
            node_id,
            status=status,
            as_of=as_of,
            believed_as_of=believed_as_of,
        ):
            evidence = list(rel.evidence_doc_ids or [])
            if not evidence:
                continue
            doc_ids = self.visible_evidence_doc_ids(evidence, principal)
            if len(doc_ids) == len(set(evidence)):
                visible.append((rel, doc_ids))
        return visible

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
    ) -> list[Claim]:
        q = self._session.query(Claim).filter(
            Claim.workspace_id == self._ws,
            Claim.subject_type == subject_type,
            Claim.subject_id == subject_id,
        )
        if statuses:
            q = q.filter(Claim.status.in_(statuses))
        if as_of is not None:
            q = q.filter(
                (Claim.valid_from.is_(None)) | (Claim.valid_from <= as_of),
                (Claim.valid_to.is_(None)) | (Claim.valid_to > as_of),
            )
        if believed_as_of is not None:
            q = q.filter(
                Claim.recorded_at <= believed_as_of,
                (Claim.invalidated_at.is_(None)) | (Claim.invalidated_at > believed_as_of),
            )
        return q.order_by(Claim.created_at.desc()).all()

    def claims_for_viewer(
        self,
        subject_type: str,
        subject_id: str,
        principal: Principal,
        statuses: list[str] | None = None,
        as_of: datetime | None = None,
        believed_as_of: datetime | None = None,
    ) -> list[tuple[Claim, list[str]]]:
        """Return claims whose *entire* evidence set is visible to this viewer.
        """
        visible: list[tuple[Claim, list[str]]] = []
        for claim in self.claims_for(
            subject_type,
            subject_id,
            statuses=statuses,
            as_of=as_of,
            believed_as_of=believed_as_of,
        ):
            evidence = list(claim.evidence_doc_ids or [])
            if not evidence:
                continue
            doc_ids = self.visible_evidence_doc_ids(evidence, principal)
            if len(doc_ids) == len(set(evidence)):
                visible.append((claim, doc_ids))
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
