"""Worldbuilder orchestration: resolve a subject, gather evidence, synthesize.

The service composes four collaborators. ``SubjectResolver`` maps names to
viewer-visible people and entities, ``SourceReader`` loads cited material,
``ProfileSynthesizer`` caches and shapes model output, and the pure functions in
``profile_structure`` parse and ground that output. Synthesis results are cached
by exact evidence set and re-grounded on every cache hit, so a hit can never
show ids the viewer has since lost.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from org_memory.core.errors import NotFoundError
from org_memory.db.orm import Person
from org_memory.db.repositories import (
    GraphRepository,
    PersonRepository,
    SynthesisTraceRepository,
)
from org_memory.domain.models import Principal
from org_memory.services.retrieval import RetrievalService
from org_memory.services.worldbuilder.profile_structure import (
    CATEGORIES,
    WorldbuilderCategory,
)
from org_memory.services.worldbuilder.read_source import SourceReader
from org_memory.services.worldbuilder.resolution import SubjectResolver
from org_memory.services.worldbuilder.synthesis import ProfileSynthesizer

__all__ = ["WorldbuilderCategory", "WorldbuilderService"]


class WorldbuilderService:
    def __init__(
        self,
        session: Session,
        retrieval: RetrievalService,
        synthesizer,
    ):
        self._session = session
        self._persons = PersonRepository(session)
        self._graph = GraphRepository(session)
        self._traces = SynthesisTraceRepository(session)
        self._retrieval = retrieval
        self._synth = synthesizer
        self._resolver = SubjectResolver(session)
        self._reader = SourceReader(session)
        self._profiles = ProfileSynthesizer(self._traces, synthesizer, self._resolver)

    def lookup(
        self,
        principal: Principal,
        *,
        name: str,
        category: WorldbuilderCategory | None = None,
        query: str | None = None,
        half_life_days: float = 90.0,
        min_decay: float = 0.3,
    ) -> dict:
        """Resolve a subject and synthesize a structured profile under viewer ACL."""
        name = name.strip()
        if not name:
            raise ValueError("name is required")

        if category is None:
            return self._lookup_auto_category(
                principal,
                name=name,
                query=query,
                half_life_days=half_life_days,
                min_decay=min_decay,
            )

        if category == "person":
            return self._lookup_person(
                principal,
                name=name,
                query=query,
                half_life_days=half_life_days,
                min_decay=min_decay,
            )
        return self._lookup_entity(
            principal,
            name=name,
            category=category,
            query=query,
            half_life_days=half_life_days,
            min_decay=min_decay,
        )

    def lookup_person(self, principal: Principal, name: str) -> dict:
        """Backward-compatible person profile lookup."""
        return self._lookup_person(principal, name=name)

    def list_category(
        self,
        principal: Principal,
        *,
        category: WorldbuilderCategory,
        limit: int = 50,
    ) -> dict:
        return self._resolver.list_category(principal, category=category, limit=limit)

    def resolve_about_subject(
        self,
        principal: Principal,
        about: str,
        *,
        category: WorldbuilderCategory | None = None,
    ) -> dict:
        return self._resolver.resolve_about_subject(principal, about, category=category)

    def resolve_about_person_ids(self, principal: Principal, about: str) -> list[str] | dict:
        return self._resolver.resolve_about_person_ids(principal, about)

    def read_source(
        self,
        principal: Principal,
        *,
        document_ids: list[str] | None = None,
        record_ids: list[str] | None = None,
    ) -> dict:
        return self._reader.read(
            principal, document_ids=document_ids, record_ids=record_ids
        )

    def _lookup_auto_category(
        self,
        principal: Principal,
        *,
        name: str,
        query: str | None,
        half_life_days: float,
        min_decay: float,
    ) -> dict:
        candidates: list[dict[str, str]] = []
        person_match = self._resolver.match_person(principal, name, raise_if_missing=False)
        if isinstance(person_match, list):
            for p in person_match:
                candidates.append(
                    {
                        "category": "person",
                        "canonical_id": p.canonical_id,
                        "display_name": p.display_name,
                    }
                )
        elif isinstance(person_match, Person):
            candidates.append(
                {
                    "category": "person",
                    "canonical_id": person_match.canonical_id,
                    "display_name": person_match.display_name,
                }
            )
        for cat in ("team", "project", "glossary"):
            for entity, _ in self._graph.search_entities_for_viewer(
                name, principal, limit=5, entity_type=cat
            ):
                candidates.append(
                    {
                        "category": cat,
                        "canonical_id": entity.entity_id,
                        "display_name": entity.name,
                    }
                )
        if not candidates:
            raise NotFoundError(
                f"No person/team/project/glossary matching '{name}' visible to this viewer."
            )
        if len(candidates) > 1:
            return {
                "disambiguation": candidates,
                "detail": (
                    f"Multiple subjects match '{name}'. Re-query with category and "
                    "exact name, or use canonical_id via the graph API."
                ),
            }
        only = candidates[0]
        cat = only["category"]  # type: ignore[assignment]
        assert cat in CATEGORIES
        return self.lookup(
            principal,
            name=only["display_name"],
            category=cat,  # type: ignore[arg-type]
            query=query,
            half_life_days=half_life_days,
            min_decay=min_decay,
        )

    def _lookup_person(
        self,
        principal: Principal,
        *,
        name: str,
        query: str | None = None,
        half_life_days: float = 90.0,
        min_decay: float = 0.3,
    ) -> dict:
        person = self._resolver.match_person(principal, name, raise_if_missing=True)
        if isinstance(person, list):
            return {
                "disambiguation": [
                    {
                        "category": "person",
                        "canonical_id": p.canonical_id,
                        "display_name": p.display_name,
                        "resolution_status": p.resolution_status,
                    }
                    for p in person
                ],
                "detail": (
                    f"Multiple people match '{name}'. Re-query with the exact "
                    "display_name or use the canonical_id via the graph API."
                ),
            }
        # raise_if_missing=True: never None here; narrow for the type checker.
        assert person is not None

        retrieval_query = (query or "").strip() or self._resolver.evidence_query(person)
        evidence = self._retrieval.search(
            principal=principal,
            query=retrieval_query,
            limit=12,
            about_person_ids=[person.canonical_id],
            half_life_days=half_life_days,
            min_decay=min_decay,
            tool_name="worldbuilder_lookup",
        )
        relationships = self._graph.relationships_for_viewer(
            "person", person.canonical_id, principal
        )
        claims = self._graph.claims_for_viewer(
            "person", person.canonical_id, principal, statuses=["active"]
        )
        return self._synthesize_profile(
            principal=principal,
            category="person",
            subject_id=person.canonical_id,
            display_name=person.display_name,
            resolution_status=person.resolution_status,
            platform_user_id=self._persons.platform_user_id_for(person.canonical_id),
            relationships=relationships,
            claims=claims,
            evidence=evidence.passages,
            audit_id=evidence.audit_id,
            query=retrieval_query,
        )

    def _lookup_entity(
        self,
        principal: Principal,
        *,
        name: str,
        category: WorldbuilderCategory,
        query: str | None,
        half_life_days: float,
        min_decay: float,
    ) -> dict:
        matches = self._graph.search_entities_for_viewer(
            name, principal, limit=10, entity_type=category
        )
        if not matches:
            raise NotFoundError(
                f"No {category} matching '{name}' visible to this viewer."
            )
        exact = [
            (e, docs)
            for e, docs in matches
            if e.name.strip().casefold() == name.strip().casefold()
        ]
        pool = exact or matches
        if len(pool) > 1:
            return {
                "disambiguation": [
                    {
                        "category": category,
                        "canonical_id": e.entity_id,
                        "display_name": e.name,
                        "resolution_status": e.resolution_status,
                    }
                    for e, _ in pool
                ],
                "detail": (
                    f"Multiple {category} entities match '{name}'. "
                    "Re-query with the exact name."
                ),
            }
        entity, _visible = pool[0]
        retrieval_query = (query or "").strip() or entity.name
        evidence_doc_ids = list(entity.evidence_doc_ids or [])
        evidence = self._retrieval.search(
            principal=principal,
            query=retrieval_query,
            limit=12,
            about_doc_ids=evidence_doc_ids or None,
            half_life_days=half_life_days,
            min_decay=min_decay,
            tool_name="worldbuilder_lookup",
        )
        # When evidence-scoped retrieval is thin, broaden with a name query
        # (still viewer-scoped) and prefer passages that mention the entity.
        if len(evidence.passages) < 3:
            broader = self._retrieval.search(
                principal=principal,
                query=retrieval_query,
                limit=12,
                half_life_days=half_life_days,
                min_decay=min_decay,
                tool_name="worldbuilder_lookup",
            )
            name_l = entity.name.casefold()
            ranked = sorted(
                broader.passages,
                key=lambda p: (
                    name_l not in (p.text or "").casefold(),
                    -float(p.score or 0),
                ),
            )[:12]
            evidence_passages = ranked
            audit_id = broader.audit_id
        else:
            evidence_passages = evidence.passages
            audit_id = evidence.audit_id
        relationships = self._graph.relationships_for_viewer(
            entity.entity_type, entity.entity_id, principal
        )
        claims = self._graph.claims_for_viewer(
            entity.entity_type, entity.entity_id, principal, statuses=["active"]
        )
        return self._synthesize_profile(
            principal=principal,
            category=category,
            subject_id=entity.entity_id,
            display_name=entity.name,
            resolution_status=entity.resolution_status,
            platform_user_id=None,
            relationships=relationships,
            claims=claims,
            evidence=evidence_passages,
            audit_id=audit_id,
            query=retrieval_query,
        )

    def _synthesize_profile(self, **kwargs) -> dict:
        """Delegate to ProfileSynthesizer (kept for existing call sites/tests)."""
        return self._profiles.synthesize(**kwargs)
