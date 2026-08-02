"""Retract graph evidence when a document is deleted or re-extracted."""

from __future__ import annotations

from org_memory.db.orm import Claim, Document, Entity, Relationship, utcnow
from org_memory.db.repositories.graph.base import GraphRepositoryBase
from org_memory.domain.fact_lifecycle import FactStatus, transition_fact


class GraphEvidenceMixin(GraphRepositoryBase):
    """Document-scoped evidence wipe for claims, relationships, and entities."""

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
