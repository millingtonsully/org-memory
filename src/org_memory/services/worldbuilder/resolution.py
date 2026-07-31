"""Subject resolution: map a name the caller typed to people or entities.

Every lookup is viewer-scoped. A person or entity only resolves when the
viewer can see at least one supporting document, so resolution itself cannot
leak the existence of subjects the viewer has no evidence for.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from org_memory.core.errors import NotFoundError
from org_memory.core.settings import get_settings
from org_memory.db.orm import Person
from org_memory.db.repositories import GraphRepository, PersonRepository
from org_memory.domain.models import Principal
from org_memory.services.worldbuilder.profile_structure import WorldbuilderCategory


class SubjectResolver:
    def __init__(self, session: Session):
        self._session = session
        self._persons = PersonRepository(session)
        self._graph = GraphRepository(session)

    def match_person(
        self,
        principal: Principal,
        name: str,
        *,
        raise_if_missing: bool,
    ) -> Person | list[Person] | None:
        """Return one person, a candidate list, or None when missing."""
        matches = [
            p
            for p in self._persons.search_by_name(name, limit=10)
            if p.merged_into_id is None
            and self._persons.visible_evidence_doc_ids(p.canonical_id, principal)
        ]
        if not matches:
            if raise_if_missing:
                raise NotFoundError(
                    f"No person matching '{name}' in the entity spine. "
                    "(Entities appear after their first authored content is ingested.)"
                )
            return None
        if len(matches) == 1:
            return matches[0]
        exact = [p for p in matches if p.display_name.lower() == name.strip().lower()]
        if len(exact) == 1:
            return exact[0]
        return matches

    def resolve_about_subject(
        self,
        principal: Principal,
        about: str,
        *,
        category: WorldbuilderCategory | None = None,
    ) -> dict:
        """Resolve about= to person ids or entity evidence doc ids."""
        about = about.strip()
        if not about:
            raise ValueError("about must be nonempty")
        if category in (None, "person"):
            match = self.match_person(principal, about, raise_if_missing=category == "person")
            if isinstance(match, list):
                return {
                    "kind": "ambiguous",
                    "disambiguation": [
                        {
                            "category": "person",
                            "canonical_id": p.canonical_id,
                            "display_name": p.display_name,
                        }
                        for p in match
                    ],
                    "detail": f"Multiple people match about={about!r}.",
                }
            if isinstance(match, Person):
                return {
                    "kind": "person",
                    "about_person_ids": [match.canonical_id],
                    "canonical_id": match.canonical_id,
                    "display_name": match.display_name,
                }
            if category == "person":
                raise NotFoundError(f"No person matching about={about!r}")

        for cat in (("team", "project", "glossary") if category is None else (category,)):
            if cat == "person":
                continue
            matches = self._graph.search_entities_for_viewer(
                about, principal, limit=5, entity_type=cat
            )
            exact = [
                (e, d)
                for e, d in matches
                if e.name.strip().casefold() == about.casefold()
            ]
            pool = exact or matches
            if len(pool) > 1:
                return {
                    "kind": "ambiguous",
                    "disambiguation": [
                        {
                            "category": cat,
                            "canonical_id": e.entity_id,
                            "display_name": e.name,
                        }
                        for e, _ in pool
                    ],
                    "detail": f"Multiple {cat} entities match about={about!r}.",
                }
            if len(pool) == 1:
                entity, docs = pool[0]
                return {
                    "kind": "entity",
                    "category": cat,
                    "canonical_id": entity.entity_id,
                    "display_name": entity.name,
                    "about_doc_ids": docs,
                }
        raise NotFoundError(f"No subject matching about={about!r} visible to this viewer.")

    def resolve_about_person_ids(self, principal: Principal, about: str) -> list[str] | dict:
        """Resolve about= name to person ids, or an ambiguity payload."""
        resolved = self.resolve_about_subject(principal, about, category="person")
        if resolved["kind"] == "ambiguous":
            return {
                "disambiguation": resolved["disambiguation"],
                "detail": resolved["detail"],
            }
        return list(resolved["about_person_ids"])

    def list_category(
        self,
        principal: Principal,
        *,
        category: WorldbuilderCategory,
        limit: int = 50,
    ) -> dict:
        """Browse visible subjects in a category (no synthesis)."""
        limit = max(1, min(int(limit), 100))
        if category == "person":
            rows = (
                self._session.query(Person)
                .filter(
                    Person.workspace_id == get_settings().workspace_id,
                    Person.merged_into_id.is_(None),
                )
                .order_by(Person.display_name.asc())
                .limit(limit * 5)
                .all()
            )
            items = []
            for person in rows:
                evidence = self._persons.visible_evidence_doc_ids(
                    person.canonical_id, principal
                )
                if not evidence:
                    continue
                items.append(
                    {
                        "category": "person",
                        "canonical_id": person.canonical_id,
                        "display_name": person.display_name,
                        "resolution_status": person.resolution_status,
                        "platform_user_id": self._persons.platform_user_id_for(
                            person.canonical_id
                        ),
                        "evidence_doc_ids": evidence,
                    }
                )
                if len(items) >= limit:
                    break
            return {"category": category, "items": items, "returned": len(items)}

        entities = self._graph.list_entities_for_viewer(
            principal, entity_type=category, limit=limit
        )
        items = [
            {
                "category": category,
                "canonical_id": entity.entity_id,
                "display_name": entity.name,
                "resolution_status": entity.resolution_status,
                "evidence_doc_ids": docs,
            }
            for entity, docs in entities
        ]
        return {"category": category, "items": items, "returned": len(items)}

    def evidence_query(self, person: Person) -> str:
        """Build a name-based retrieval query from the person's known aliases."""
        names = [person.display_name, *(person.name_aliases or [])]
        for alias in self._persons.aliases_for(person.canonical_id):
            if alias.display_name:
                names.append(alias.display_name)
        seen: set[str] = set()
        ordered: list[str] = []
        for name in names:
            key = name.strip().casefold()
            if not key or key in seen:
                continue
            seen.add(key)
            ordered.append(name.strip())
        return " ".join(ordered) if ordered else person.display_name

    def node_label(self, principal: Principal, node_type: str, node_id: str) -> str:
        """Return a display label only when the node is viewer-visible; else opaque id."""
        if node_type == "person":
            if not self._persons.visible_evidence_doc_ids(node_id, principal):
                return f"person:{node_id}"
            person = self._persons.get(node_id)
            return person.display_name if person else f"person:{node_id}"
        scoped = self._graph.get_entity_for_viewer(node_id, principal)
        if scoped is None:
            return f"{node_type}:{node_id}"
        entity, _ = scoped
        return entity.name
