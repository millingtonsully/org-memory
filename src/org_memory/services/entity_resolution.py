"""Resolve source-observed identities into canonical people.

Verified keys join deterministically. Embeddings and names only form candidate
pairs; a spend-governed structured LLM decision, hard conflict rules, and
corroborating signals control reversible automatic merges.
"""

from __future__ import annotations

import structlog
from sqlalchemy.orm import Session

from org_memory.core.settings import get_settings
from org_memory.db.engine import session_scope
from org_memory.db.orm import Person, PersonAlias, utcnow
from org_memory.db.repositories import JobRepository, PersonRepository, SpendRepository
from org_memory.domain.jobs import JobType
from org_memory.domain.models import IdentityEmail, IdentityKind, SourceIdentity
from org_memory.ports.embedder import Embedder
from org_memory.services.identity_merge import (
    corroborating_signals,
    normalize_email,
    reconcile_merged_identity_conflicts,
)

logger = structlog.get_logger(__name__)


class EntityResolutionService:
    def __init__(self, session: Session, embedder: Embedder):
        self._session = session
        self._persons = PersonRepository(session)
        self._jobs = JobRepository(session)
        self._embedder = embedder

    def observe_identity(self, source_system: str, identity: SourceIdentity) -> str | None:
        """Observe one actor and return a Person id only for classified people."""
        if identity.identity_kind != IdentityKind.person:
            return None

        person = self._deterministic_match(source_system, identity)
        if person is None:
            person = Person(
                workspace_id=get_settings().workspace_id,
                display_name=self._display_name(identity),
                name_aliases=[self._display_name(identity)],
                primary_email=self._primary_verified_email(identity),
                resolution_status="provisional",
            )
            self._persons.add(person)
            self._session.flush()

        if identity.display_name.strip():
            names = set(person.name_aliases or [])
            names.add(identity.display_name.strip())
            person.name_aliases = sorted(names)
        verified_email = self._primary_verified_email(identity)
        if verified_email and not person.primary_email:
            person.primary_email = verified_email
        self._add_aliases(person, source_system, identity)
        self._session.flush()
        self._refresh_identity_metadata(person)
        reconcile_merged_identity_conflicts(self._session, person)
        self._jobs.enqueue(
            JobType.refresh_identity_embedding,
            {"person_id": person.canonical_id},
        )
        person.updated_at = utcnow()
        return person.canonical_id

    def _deterministic_match(self, source_system: str, identity: SourceIdentity) -> Person | None:
        # Source ids are stable only inside their own connector namespace.
        person = self._persons.find_by_source_id(source_system, identity.external_id)
        if person is not None:
            return person

        # Verified global identifiers are the strongest cross-source bridge.
        for key in identity.identifiers:
            if not key.verified or not key.namespace.strip() or not key.value.strip():
                continue
            person = self._persons.find_by_source_id(
                f"identity:{key.namespace.strip().casefold()}", key.value.strip()
            )
            if person is not None:
                return person

        # Email is deterministic only when the connector explicitly verifies
        # ownership and classifies the actor as a person.
        for email in identity.emails:
            if email.verified:
                person = self._persons.find_by_verified_email(self._normalize_email(email.value))
                if person is not None:
                    return person
        return None

    def _add_aliases(self, person: Person, source_system: str, identity: SourceIdentity) -> None:
        primary = identity.emails[0] if identity.emails else IdentityEmail(value="")
        self._persons.add_alias(
            PersonAlias(
                person_id=person.canonical_id,
                observed_person_id=person.canonical_id,
                workspace_id=get_settings().workspace_id,
                source_system=source_system,
                external_id=identity.external_id,
                display_name=identity.display_name,
                email=normalize_email(primary.value),
                email_verified=primary.verified,
                confidence=1.0,
            )
        )
        for email in identity.emails[1:]:
            self._persons.add_alias(
                PersonAlias(
                    person_id=person.canonical_id,
                    observed_person_id=person.canonical_id,
                    workspace_id=get_settings().workspace_id,
                    source_system=source_system,
                    external_id="",
                    display_name=identity.display_name,
                    email=normalize_email(email.value),
                    email_verified=email.verified,
                    confidence=1.0,
                )
            )
        for key in identity.identifiers:
            if not key.verified or not key.namespace.strip() or not key.value.strip():
                continue
            self._persons.add_alias(
                PersonAlias(
                    person_id=person.canonical_id,
                    observed_person_id=person.canonical_id,
                    workspace_id=get_settings().workspace_id,
                    source_system=f"identity:{key.namespace.strip().casefold()}",
                    external_id=key.value.strip(),
                    display_name=identity.display_name,
                    email="",
                    email_verified=False,
                    confidence=1.0,
                )
            )

    def refresh_identity_embedding(self, person: Person) -> None:
        descriptor = self._identity_descriptor(person)
        vectors, tokens = self._embedder.embed_texts([descriptor])
        with session_scope() as spend_session:
            SpendRepository(spend_session).record("identity", "embedding", self._embedder.model_name, tokens)
        embedding = vectors[0]

        settings = get_settings()
        candidates = self._persons.semantic_identity_candidates(
            embedding,
            self._embedder.model_name,
            exclude_person_id=person.canonical_id,
            min_similarity=settings.identity_candidate_similarity,
            limit=settings.identity_candidate_limit,
        )
        by_id = {candidate.canonical_id: (candidate, score) for candidate, score in candidates}
        for candidate in self._persons.search_by_name(person.display_name, limit=5):
            if candidate.canonical_id != person.canonical_id:
                by_id.setdefault(candidate.canonical_id, (candidate, 0.0))

        person.identity_embedding = embedding
        person.identity_embedding_model = self._embedder.model_name
        for candidate, similarity in by_id.values():
            signals = corroborating_signals(
                self._persons.aliases_for(candidate.canonical_id),
                self._persons.aliases_for(person.canonical_id),
                candidate,
                person,
                similarity,
            )
            self._jobs.enqueue(
                JobType.adjudicate_persons,
                {
                    "person_a": candidate.canonical_id,
                    "person_b": person.canonical_id,
                    "candidate_similarity": similarity,
                    "signals": signals,
                },
            )

    def _identity_descriptor(self, person: Person) -> str:
        aliases = self._persons.aliases_for(person.canonical_id)
        lines = [f"name: {person.display_name}"]
        for alias in aliases:
            lines.append(
                f"source: {alias.source_system}; external_id: {alias.external_id}; "
                f"name: {alias.display_name}; email: {alias.email}; "
                f"email_verified: {alias.email_verified}"
            )
        return "\n".join(lines)

    def _refresh_identity_metadata(self, person: Person) -> None:
        """Store only deterministic metadata derived from source aliases."""
        aliases = self._persons.aliases_for(person.canonical_id)
        person.identity_metadata = {
            "sources": sorted({alias.source_system for alias in aliases}),
            "alias_count": len(aliases),
            "verified_email_count": sum(alias.email_verified for alias in aliases),
            "verified_identifier_namespaces": sorted(
                {
                    alias.source_system.removeprefix("identity:")
                    for alias in aliases
                    if alias.source_system.startswith("identity:") and alias.external_id
                }
            ),
        }

    @staticmethod
    def _normalize_email(value: str) -> str:
        return normalize_email(value)

    @classmethod
    def _primary_verified_email(cls, identity: SourceIdentity) -> str:
        return next(
            (cls._normalize_email(email.value) for email in identity.emails if email.verified),
            "",
        )

    @classmethod
    def _display_name(cls, identity: SourceIdentity) -> str:
        return (
            identity.display_name.strip()
            or cls._primary_verified_email(identity)
            or identity.external_id.strip()
            or "Unnamed person"
        )
