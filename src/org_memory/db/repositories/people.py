"""Person and alias queries scoped to the configured workspace."""

from __future__ import annotations

import json

from sqlalchemy import func, or_
from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from org_memory.core.settings import get_settings
from org_memory.db.orm import (
    Document,
    DocumentParticipant,
    Person,
    PersonAlias,
)
from org_memory.domain.emails import normalize_email
from org_memory.domain.models import Principal


class PersonRepository:
    """Person queries scoped to the current workspace."""

    def __init__(self, session: Session):
        self._session = session
        self._ws = get_settings().workspace_id

    def get(self, person_id: str) -> Person | None:
        person = self._session.get(Person, person_id)
        if person is None or person.workspace_id != self._ws:
            return None
        return person

    def find_by_verified_email(self, email: str) -> Person | None:
        if not email:
            return None
        normalized = normalize_email(email)
        alias = (
            self._session.query(PersonAlias)
            .filter(
                PersonAlias.workspace_id == self._ws,
                PersonAlias.email == normalized,
                PersonAlias.email_verified == True,  # noqa: E712
            )
            .first()
        )
        return self._session.get(Person, alias.person_id) if alias else None

    def find_by_source_id(self, source_system: str, external_id: str) -> Person | None:
        if not external_id:
            return None
        alias = (
            self._session.query(PersonAlias)
            .filter(
                PersonAlias.workspace_id == self._ws,
                PersonAlias.source_system == source_system,
                PersonAlias.external_id == external_id,
            )
            .first()
        )
        return self._session.get(Person, alias.person_id) if alias else None

    def find_by_platform_user_id(self, platform_user_id: str) -> Person | None:
        """Resolve canonical person linked to a host User UUID via identity:platform_user."""
        from org_memory.domain.identity_namespaces import PLATFORM_USER_SOURCE_SYSTEM

        value = platform_user_id.strip()
        if not value:
            return None
        person = self.find_by_source_id(PLATFORM_USER_SOURCE_SYSTEM, value)
        if person is None:
            return None
        if person.merged_into_id:
            return self.get(person.merged_into_id)
        return person

    def platform_user_id_for(self, person_id: str) -> str | None:
        """Return host User UUID when exactly one identity:platform_user alias exists."""
        from org_memory.domain.identity_namespaces import PLATFORM_USER_SOURCE_SYSTEM

        aliases = [
            a
            for a in self.aliases_for(person_id)
            if a.source_system == PLATFORM_USER_SOURCE_SYSTEM and a.external_id.strip()
        ]
        if len(aliases) != 1:
            return None
        return aliases[0].external_id.strip()

    def search_by_name(self, name: str, limit: int = 5) -> list[Person]:
        # ILIKE match for name search tools
        return (
            self._session.query(Person)
            .filter(
                Person.workspace_id == self._ws,
                or_(
                    Person.display_name.ilike(f"%{name}%"),
                    func.array_to_string(Person.name_aliases, " ").ilike(f"%{name}%"),
                ),
            )
            .limit(limit)
            .all()
        )

    def semantic_identity_candidates(
        self,
        embedding: list[float],
        embedding_model: str,
        *,
        exclude_person_id: str = "",
        min_similarity: float,
        limit: int,
    ) -> list[tuple[Person, float]]:
        """Find possible duplicate people for structured adjudication."""
        rows = self._session.execute(
            sql("""
                SELECT canonical_id,
                       1 - (identity_embedding <=> CAST(:embedding AS vector)) AS similarity
                FROM persons
                WHERE workspace_id = :workspace_id
                  AND (:exclude_person_id = '' OR canonical_id <> :exclude_person_id)
                  AND identity_embedding IS NOT NULL
                  AND identity_embedding_model = :embedding_model
                  AND merged_into_id IS NULL
                  AND 1 - (identity_embedding <=> CAST(:embedding AS vector))
                      >= :min_similarity
                ORDER BY identity_embedding <=> CAST(:embedding AS vector)
                LIMIT :limit
            """),
            {
                "workspace_id": self._ws,
                "exclude_person_id": exclude_person_id,
                "embedding_model": embedding_model,
                "embedding": json.dumps(embedding),
                "min_similarity": min_similarity,
                "limit": limit,
            },
        ).fetchall()
        result: list[tuple[Person, float]] = []
        for row in rows:
            person = self.get(row.canonical_id)
            if person is not None:
                result.append((person, float(row.similarity)))
        return result

    def visible_evidence_doc_ids(self, person_id: str, principal: Principal) -> list[str]:
        """Documents that both identify this person and are visible now."""
        rows = (
            self._session.query(DocumentParticipant.doc_id)
            .join(Document, Document.doc_id == DocumentParticipant.doc_id)
            .filter(
                DocumentParticipant.workspace_id == self._ws,
                DocumentParticipant.person_id == person_id,
                Document.deleted == False,  # noqa: E712
                (
                    (Document.org_visible == True)  # noqa: E712
                    | Document.allowed_principals.overlap(principal.all_principals())
                ),
            )
            .distinct()
            .all()
        )
        return [row.doc_id for row in rows]

    def aliases_for(self, person_id: str) -> list[PersonAlias]:
        return (
            self._session.query(PersonAlias)
            .filter(
                PersonAlias.workspace_id == self._ws,
                PersonAlias.person_id == person_id,
            )
            .all()
        )

    def aliases_observed_for(self, person_id: str) -> list[PersonAlias]:
        return (
            self._session.query(PersonAlias)
            .filter(
                PersonAlias.workspace_id == self._ws,
                PersonAlias.observed_person_id == person_id,
            )
            .all()
        )

    def merged_children(self, canonical_id: str) -> list[Person]:
        return (
            self._session.query(Person)
            .filter(
                Person.workspace_id == self._ws,
                Person.merged_into_id == canonical_id,
            )
            .all()
        )

    def add(self, person: Person) -> None:
        self._session.add(person)

    def add_alias(self, alias: PersonAlias) -> PersonAlias:
        """Upsert a source identity while preserving historical names/emails."""
        if not alias.observed_person_id:
            alias.observed_person_id = alias.person_id
        q = self._session.query(PersonAlias).filter(
            PersonAlias.workspace_id == alias.workspace_id,
            PersonAlias.person_id == alias.person_id,
            PersonAlias.source_system == alias.source_system,
        )
        if alias.external_id:
            # Matches the database's unique source-identity constraint.
            existing = q.filter(PersonAlias.external_id == alias.external_id).first()
        else:
            existing = q.filter(
                PersonAlias.external_id == "",
                PersonAlias.email == alias.email,
            ).first()
        if existing is not None:
            # Keep an old address as an explicit email-only alias before the
            # source's current identity record moves to a new address.
            if alias.external_id and existing.email and alias.email and existing.email != alias.email:
                historical = q.filter(
                    PersonAlias.external_id == "",
                    PersonAlias.email == existing.email,
                ).first()
                if historical is None:
                    self._session.add(
                        PersonAlias(
                            person_id=alias.person_id,
                            observed_person_id=alias.observed_person_id,
                            workspace_id=alias.workspace_id,
                            source_system=alias.source_system,
                            external_id="",
                            display_name=existing.display_name,
                            email=existing.email,
                            email_verified=existing.email_verified,
                            confidence=existing.confidence,
                        )
                    )
            if alias.display_name:
                existing.display_name = alias.display_name
            if alias.email:
                existing.email = alias.email
            existing.email_verified = existing.email_verified or alias.email_verified
            return existing
        self._session.add(alias)
        return alias


