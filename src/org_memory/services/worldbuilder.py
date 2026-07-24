"""Derived Worldbuilder profiles over viewer-scoped retrieval and graph facts.

Profiles are synthesized read-only outputs (not host workflow entities).
Categories: person, team, project, glossary. Every evidence path enforces ACL.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from org_memory.core.errors import NotFoundError
from org_memory.core.settings import get_settings
from org_memory.db.engine import session_scope
from org_memory.db.orm import Chunk, Claim, Document, Person
from org_memory.db.repositories import (
    GraphRepository,
    PersonRepository,
    SpendRepository,
    SynthesisTraceRepository,
)
from org_memory.domain.models import Passage, Principal
from org_memory.services.retrieval import RetrievalService

WorldbuilderCategory = Literal["person", "team", "project", "glossary"]
_CATEGORIES: tuple[WorldbuilderCategory, ...] = ("person", "team", "project", "glossary")

_PROFILE_JSON_SCHEMA_HINT = """
Return ONLY valid JSON (no markdown) with this shape:
{
  "subject_descriptions": [{"text": str, "confidence": number 0-1, "evidence_doc_ids": [str], "source_record_ids": [str]}],
  "org_work_context": [{"text": str, "confidence": number, "evidence_doc_ids": [str], "source_record_ids": [str]}],
  "vocabulary": [{"term": str, "note": str, "evidence_doc_ids": [str]}],
  "caveats": [str],
  "team_signals": [{"text": str, "confidence": number, "evidence_doc_ids": [str]}],
  "profile_prose": str
}
Rules:
- Use ONLY GRAPH FACTS and EVIDENCE. Never invent.
- evidence_doc_ids must be doc_ids from the evidence/graph sections.
- source_record_ids may be claim_id or relationship_id values from GRAPH FACTS when used.
- Omit empty arrays. Put uncertainty in caveats.
- profile_prose is a short readable summary of the structured fields only.
""".strip()


def _category_system_prompt(category: WorldbuilderCategory) -> str:
    focus = {
        "person": "Role & team, current projects, collaborators, recent activity.",
        "team": "Purpose, members/signals, owned work, recent activity.",
        "project": "Goal, status signals, participants, risks/blockers from evidence.",
        "glossary": "Org-specific definition of the term, usage context, related teams/projects.",
    }[category]
    return (
        f"You are Worldbuilder. Synthesize a structured {category} profile.\n"
        f"Focus: {focus}\n"
        f"{_PROFILE_JSON_SCHEMA_HINT}"
    )


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
        person_match = self._match_person(principal, name, raise_if_missing=False)
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
        assert cat in _CATEGORIES
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
        person = self._match_person(principal, name, raise_if_missing=True)
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

        retrieval_query = (query or "").strip() or self._evidence_query(person)
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
        # If evidence-scoped retrieval is thin, broaden with name query (still ACL'd).
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

    def _synthesize_profile(
        self,
        *,
        principal: Principal,
        category: WorldbuilderCategory,
        subject_id: str,
        display_name: str,
        resolution_status: str,
        platform_user_id: str | None,
        relationships,
        claims,
        evidence: list[Passage],
        audit_id: str | None,
        query: str,
    ) -> dict:
        graph_block = self._render_graph_facts(principal, relationships, claims)
        input_doc_ids = sorted({p.doc_id for p in evidence})
        settings = get_settings()
        cached = self._traces.latest_reusable(
            tool="worldbuilder_lookup",
            subject=subject_id,
            input_doc_ids=input_doc_ids,
            max_age_seconds=settings.worldbuilder_cache_ttl_seconds,
        )
        if cached is not None:
            try:
                structured = json.loads(cached.output_text)
                if not isinstance(structured, dict):
                    raise ValueError("cached profile is not an object")
            except (json.JSONDecodeError, ValueError):
                structured = None
            if structured is not None:
                # Re-ground and re-seed from current viewer-visible graph so a
                # cache hit cannot revive ids or facts the viewer can no longer see.
                structured = _ground_structured_profile(
                    structured,
                    allowed_doc_ids={p.doc_id for p in evidence},
                    allowed_record_ids=_graph_record_ids(relationships, claims),
                )
                source = structured.get("profile_structure_source")
                model_ok = source in ("model", "model_and_graph")
                structure_source = _ensure_structured_from_graph(
                    structured,
                    claims=claims,
                    relationships=relationships,
                    model_json_ok=bool(model_ok),
                    display_name=display_name,
                )
                structured["profile_structure_source"] = structure_source
                return self._profile_payload(
                    category=category,
                    subject_id=subject_id,
                    display_name=display_name,
                    resolution_status=resolution_status,
                    platform_user_id=platform_user_id,
                    structured=structured,
                    relationships=relationships,
                    claims=claims,
                    evidence=evidence,
                    audit_id=audit_id,
                    synthesized_at=cached.created_at,
                    model=cached.model,
                    trace_id=cached.trace_id,
                    cache_hit=True,
                )

        profile_raw, tokens = self._synth.complete(
            _category_system_prompt(category),
            _build_profile_prompt(category, display_name, graph_block, evidence, query),
            json_object=True,
        )
        with session_scope() as spend_session:
            SpendRepository(spend_session).record(
                "synthesis", "synthesis", self._synth.model_name, tokens
            )

        structured, model_json_ok = _parse_structured_profile(profile_raw)
        structured = _ground_structured_profile(
            structured,
            allowed_doc_ids={p.doc_id for p in evidence},
            allowed_record_ids=_graph_record_ids(relationships, claims),
        )
        structure_source = _ensure_structured_from_graph(
            structured,
            claims=claims,
            relationships=relationships,
            model_json_ok=model_json_ok,
            display_name=display_name,
        )
        graph_claims = [
            {
                "claim_id": c.claim_id,
                "predicate": c.predicate,
                "object": c.object_text,
                "confidence": c.confidence,
                "evidence_doc_ids": visible_doc_ids,
                "valid_from": c.valid_from.isoformat() if c.valid_from else None,
                "valid_to": c.valid_to.isoformat() if c.valid_to else None,
            }
            for c, visible_doc_ids in claims
        ]
        _add_staleness_caveats(structured, graph_claims, evidence)
        structured["profile_structure_source"] = structure_source

        synthesized_at = datetime.now(UTC)
        trace_id = self._traces.record(
            principal_id=principal.principal_id,
            tool="worldbuilder_lookup",
            subject=subject_id,
            model=self._synth.model_name,
            input_doc_ids=input_doc_ids,
            output_text=json.dumps(structured, ensure_ascii=False),
            tokens=tokens,
        )
        return self._profile_payload(
            category=category,
            subject_id=subject_id,
            display_name=display_name,
            resolution_status=resolution_status,
            platform_user_id=platform_user_id,
            structured=structured,
            relationships=relationships,
            claims=claims,
            evidence=evidence,
            audit_id=audit_id,
            synthesized_at=synthesized_at,
            model=self._synth.model_name,
            trace_id=trace_id,
            cache_hit=False,
        )

    def _profile_payload(
        self,
        *,
        category: WorldbuilderCategory,
        subject_id: str,
        display_name: str,
        resolution_status: str,
        platform_user_id: str | None,
        structured: dict[str, Any],
        relationships,
        claims,
        evidence: list[Passage],
        audit_id: str | None,
        synthesized_at: datetime,
        model: str,
        trace_id: str,
        cache_hit: bool,
    ) -> dict:
        graph_claims = [
            {
                "claim_id": c.claim_id,
                "predicate": c.predicate,
                "object": c.object_text,
                "confidence": c.confidence,
                "evidence_doc_ids": visible_doc_ids,
                "valid_from": c.valid_from.isoformat() if c.valid_from else None,
                "valid_to": c.valid_to.isoformat() if c.valid_to else None,
            }
            for c, visible_doc_ids in claims
        ]
        graph_relationships = [
            {
                "relationship_id": r.relationship_id,
                "relationship_type": r.relationship_type,
                "from": {"type": r.from_type, "id": r.from_id},
                "to": {"type": r.to_type, "id": r.to_id},
                "confidence": r.confidence,
                "evidence_doc_ids": visible_doc_ids,
            }
            for r, visible_doc_ids in relationships
        ]
        event_times = [p.event_time for p in evidence if p.event_time is not None]
        created = synthesized_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return {
            "category": category,
            "canonical_id": subject_id,
            "display_name": display_name,
            "resolution_status": resolution_status,
            "platform_user_id": platform_user_id,
            "subject_descriptions": structured.get("subject_descriptions", []),
            "org_work_context": structured.get("org_work_context", []),
            "vocabulary": structured.get("vocabulary", []),
            "caveats": structured.get("caveats", []),
            "team_signals": structured.get("team_signals", []),
            "profile_prose": structured.get("profile_prose")
            or structured.get("profile")
            or "",
            "profile_structure_source": structured.get(
                "profile_structure_source", "prose_only"
            ),
            "relationships": graph_relationships,
            "claims": graph_claims,
            "citations": {
                "source_document_ids": sorted({p.doc_id for p in evidence}),
                "source_record_ids": sorted(
                    {
                        *(c["claim_id"] for c in graph_claims),
                        *(r["relationship_id"] for r in graph_relationships),
                    }
                ),
            },
            "evidence": [p.model_dump(mode="json") for p in evidence],
            "synthesized_at": created.isoformat(),
            "model": model,
            "cache_hit": cache_hit,
            "evidence_time_range": {
                "min": min(event_times).isoformat() if event_times else None,
                "max": max(event_times).isoformat() if event_times else None,
            },
            "audit_id": audit_id,
            "trace_id": trace_id,
            "profile": structured.get("profile_prose") or structured.get("profile") or "",
        }

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
            match = self._match_person(principal, about, raise_if_missing=category == "person")
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

    def resolve_about_person_ids(
        self, principal: Principal, about: str
    ) -> list[str] | dict:
        """Resolve about= name to person ids, or an ambiguity payload."""
        resolved = self.resolve_about_subject(principal, about, category="person")
        if resolved["kind"] == "ambiguous":
            return {
                "disambiguation": resolved["disambiguation"],
                "detail": resolved["detail"],
            }
        return list(resolved["about_person_ids"])

    def _match_person(
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

    def _evidence_query(self, person: Person) -> str:
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

    def _render_graph_facts(self, principal: Principal, relationships, claims) -> str:
        lines: list[str] = []
        for r, visible_doc_ids in relationships:
            from_label = self._node_label(principal, r.from_type, r.from_id)
            to_label = self._node_label(principal, r.to_type, r.to_id)
            evidence = ", ".join(visible_doc_ids[:3])
            lines.append(
                f"- relationship_id={r.relationship_id} "
                f"{from_label} {r.relationship_type} {to_label} [{evidence}]"
            )
        for c, visible_doc_ids in claims:
            evidence = ", ".join(visible_doc_ids[:3])
            lines.append(
                f"- claim_id={c.claim_id} {c.predicate}: {c.object_text} [{evidence}]"
            )
        return "\n".join(lines) or "(none)"

    def _node_label(self, principal: Principal, node_type: str, node_id: str) -> str:
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

    def read_source(
        self,
        principal: Principal,
        *,
        document_ids: list[str] | None = None,
        record_ids: list[str] | None = None,
    ) -> dict:
        """Load documents and/or claim/relationship records under viewer ACL."""
        doc_ids = [d for d in (document_ids or []) if d.strip()]
        rec_ids = [r for r in (record_ids or []) if r.strip()]
        if not doc_ids and not rec_ids:
            raise ValueError(
                "read_source requires source_document_ids and/or source_record_ids"
            )

        sources: list[dict] = []
        outcomes: list[dict] = []
        if doc_ids:
            doc_result = self._read_documents(principal, doc_ids)
            sources.extend(doc_result["sources"])
            outcomes.extend(doc_result["outcomes"])
        if rec_ids:
            rec_result = self._read_records(principal, rec_ids)
            sources.extend(rec_result["sources"])
            outcomes.extend(rec_result["outcomes"])
        return {"sources": sources, "outcomes": outcomes}

    def _read_documents(self, principal: Principal, doc_ids: list[str]) -> dict:
        from org_memory.core.settings import get_settings

        results: list[dict] = []
        outcomes: list[dict] = []
        viewer = principal.all_principals()
        workspace_id = get_settings().workspace_id
        for doc_id in doc_ids:
            doc = self._session.get(Document, doc_id)
            if doc is None or doc.deleted or doc.workspace_id != workspace_id:
                outcomes.append({"id": doc_id, "kind": "document", "outcome": "not_found"})
                continue
            if not (doc.org_visible or set(doc.allowed_principals) & set(viewer)):
                outcomes.append({"id": doc_id, "kind": "document", "outcome": "forbidden"})
                continue
            chunks = (
                self._session.query(Chunk)
                .filter(
                    Chunk.doc_id == doc_id,
                    Chunk.deleted == False,  # noqa: E712
                    Chunk.chunk_role == "parent",
                )
                .order_by(Chunk.chunk_index)
                .all()
            )
            if not chunks:
                chunks = (
                    self._session.query(Chunk)
                    .filter(
                        Chunk.doc_id == doc_id,
                        Chunk.deleted == False,  # noqa: E712
                        Chunk.chunk_role == "child",
                    )
                    .order_by(Chunk.chunk_index)
                    .all()
                )
            results.append(
                {
                    "kind": "document",
                    "doc_id": doc.doc_id,
                    "source_type": doc.source_type,
                    "source_system": doc.source_system,
                    "title": doc.title,
                    "author_display_name": doc.author_display_name,
                    "event_time": doc.event_time.isoformat(),
                    "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
                    "deep_link": doc.deep_link,
                    "rendered_text": doc.rendered_text,
                    "chunks": [{"chunk_id": c.chunk_id, "text": c.text} for c in chunks],
                }
            )
            outcomes.append({"id": doc_id, "kind": "document", "outcome": "ok"})
        return {"sources": results, "outcomes": outcomes}

    def _read_records(self, principal: Principal, record_ids: list[str]) -> dict:
        results: list[dict] = []
        outcomes: list[dict] = []
        for record_id in record_ids:
            claim = self._session.get(Claim, record_id)
            if claim is not None:
                visible = self._graph.visible_evidence_doc_ids(
                    list(claim.evidence_doc_ids or []), principal
                )
                if not visible or len(visible) != len(set(claim.evidence_doc_ids or [])):
                    outcomes.append(
                        {"id": record_id, "kind": "claim", "outcome": "forbidden"}
                    )
                    continue
                results.append(
                    {
                        "kind": "claim",
                        "source_record_id": claim.claim_id,
                        "subject_type": claim.subject_type,
                        "subject_id": claim.subject_id,
                        "predicate": claim.predicate,
                        "object": claim.object_text,
                        "confidence": claim.confidence,
                        "status": claim.status,
                        "evidence_doc_ids": visible,
                        "evidence_quotes": [
                            q
                            for q in (claim.evidence_quotes or [])
                            if q.get("doc_id") in set(visible)
                        ],
                    }
                )
                outcomes.append({"id": record_id, "kind": "claim", "outcome": "ok"})
                continue

            from org_memory.db.orm import Relationship

            rel = self._session.get(Relationship, record_id)
            if rel is None:
                outcomes.append(
                    {"id": record_id, "kind": "record", "outcome": "not_found"}
                )
                continue
            visible = self._graph.visible_evidence_doc_ids(
                list(rel.evidence_doc_ids or []), principal
            )
            if not visible or len(visible) != len(set(rel.evidence_doc_ids or [])):
                outcomes.append(
                    {"id": record_id, "kind": "relationship", "outcome": "forbidden"}
                )
                continue
            results.append(
                {
                    "kind": "relationship",
                    "source_record_id": rel.relationship_id,
                    "relationship_type": rel.relationship_type,
                    "from": {"type": rel.from_type, "id": rel.from_id},
                    "to": {"type": rel.to_type, "id": rel.to_id},
                    "confidence": rel.confidence,
                    "status": rel.status,
                    "evidence_doc_ids": visible,
                }
            )
            outcomes.append({"id": record_id, "kind": "relationship", "outcome": "ok"})
        return {"sources": results, "outcomes": outcomes}


def _graph_record_ids(relationships, claims) -> set[str]:
    ids: set[str] = set()
    for r, _ in relationships:
        ids.add(r.relationship_id)
    for c, _ in claims:
        ids.add(c.claim_id)
    return ids


def _parse_structured_profile(raw: str) -> tuple[dict[str, Any], bool]:
    """Parse model JSON. Returns (structured, model_json_ok).

    On parse failure, returns prose-only scaffolding with empty arrays and
    model_json_ok=False. Callers must run _ensure_structured_from_graph so
    agents never see empty structured fields when graph facts exist.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed, True
    except json.JSONDecodeError:
        pass
    return {
        "subject_descriptions": [],
        "org_work_context": [],
        "vocabulary": [],
        "caveats": [],
        "team_signals": [],
        "profile_prose": raw.strip(),
    }, False


def _structured_buckets_nonempty(structured: dict[str, Any]) -> bool:
    for key in ("subject_descriptions", "org_work_context", "vocabulary", "team_signals"):
        items = structured.get(key)
        if isinstance(items, list) and items:
            return True
    return False


def _ensure_structured_from_graph(
    structured: dict[str, Any],
    *,
    claims,
    relationships,
    model_json_ok: bool,
    display_name: str = "",
) -> str:
    """Fill empty structured buckets from ACL'd graph facts. Never invents text.

    Returns profile_structure_source: model | graph | model_and_graph | prose_only.
    """
    had_model_fields = model_json_ok and _structured_buckets_nonempty(structured)
    seeded = False

    if not structured.get("subject_descriptions"):
        descriptions = []
        for claim, visible_doc_ids in claims:
            text = f"{claim.predicate}: {claim.object_text}".strip()
            if not text:
                continue
            descriptions.append(
                {
                    "text": text,
                    "confidence": float(claim.confidence or 0.0),
                    "evidence_doc_ids": list(visible_doc_ids),
                    "source_record_ids": [claim.claim_id],
                }
            )
        if descriptions:
            structured["subject_descriptions"] = descriptions
            seeded = True

    if not structured.get("vocabulary"):
        vocab = []
        for claim, visible_doc_ids in claims:
            if str(claim.predicate).strip().lower() != "definition":
                continue
            note = str(claim.object_text or "").strip()
            if not note:
                continue
            vocab.append(
                {
                    "term": (display_name or "definition").strip(),
                    "note": note,
                    "evidence_doc_ids": list(visible_doc_ids),
                }
            )
        if vocab:
            structured["vocabulary"] = vocab
            seeded = True

    if not structured.get("team_signals"):
        signals = []
        for rel, visible_doc_ids in relationships:
            text = (
                f"{rel.from_type}:{rel.from_id} {rel.relationship_type} "
                f"{rel.to_type}:{rel.to_id}"
            )
            signals.append(
                {
                    "text": text,
                    "confidence": float(rel.confidence or 0.0),
                    "evidence_doc_ids": list(visible_doc_ids),
                    "source_record_ids": [rel.relationship_id],
                }
            )
        if signals:
            structured["team_signals"] = signals
            seeded = True

    if not str(structured.get("profile_prose") or "").strip():
        bits = [
            item["text"]
            for item in (structured.get("subject_descriptions") or [])
            if isinstance(item, dict) and item.get("text")
        ]
        if bits:
            structured["profile_prose"] = "; ".join(bits[:8])
            seeded = True

    caveats = structured.setdefault("caveats", [])
    if not isinstance(caveats, list):
        structured["caveats"] = []
        caveats = structured["caveats"]

    if not model_json_ok:
        msg = (
            "Model did not return valid structured JSON; "
            "structured fields were filled from graph facts when available."
            if seeded
            else (
                "Model did not return valid structured JSON and no graph facts "
                "were available; profile_prose contains raw synthesis only."
            )
        )
        if msg not in caveats:
            caveats.append(msg)
    elif seeded and not had_model_fields:
        msg = (
            "Model returned no usable structured fields; "
            "structured fields were filled from graph facts."
        )
        if msg not in caveats:
            caveats.append(msg)
    elif seeded and had_model_fields:
        msg = "Empty structured buckets were filled from graph facts."
        if msg not in caveats:
            caveats.append(msg)

    if had_model_fields and seeded:
        return "model_and_graph"
    if had_model_fields:
        return "model"
    if seeded or _structured_buckets_nonempty(structured):
        return "graph"
    return "prose_only"


def _ground_structured_profile(
    structured: dict[str, Any],
    *,
    allowed_doc_ids: set[str],
    allowed_record_ids: set[str],
) -> dict[str, Any]:
    def _filter_docs(ids: Any) -> list[str]:
        if not isinstance(ids, list):
            return []
        return [str(i) for i in ids if str(i) in allowed_doc_ids]

    def _filter_records(ids: Any) -> list[str]:
        if not isinstance(ids, list):
            return []
        return [str(i) for i in ids if str(i) in allowed_record_ids]

    for key in ("subject_descriptions", "org_work_context", "team_signals"):
        items = structured.get(key)
        if not isinstance(items, list):
            structured[key] = []
            continue
        cleaned = []
        for item in items:
            if not isinstance(item, dict) or not str(item.get("text") or "").strip():
                continue
            cleaned.append(
                {
                    "text": str(item["text"]).strip(),
                    "confidence": float(item.get("confidence") or 0.0),
                    "evidence_doc_ids": _filter_docs(item.get("evidence_doc_ids")),
                    "source_record_ids": _filter_records(item.get("source_record_ids")),
                }
            )
        structured[key] = cleaned

    vocab = structured.get("vocabulary")
    if not isinstance(vocab, list):
        structured["vocabulary"] = []
    else:
        structured["vocabulary"] = [
            {
                "term": str(item.get("term") or "").strip(),
                "note": str(item.get("note") or "").strip(),
                "evidence_doc_ids": _filter_docs(item.get("evidence_doc_ids")),
            }
            for item in vocab
            if isinstance(item, dict) and str(item.get("term") or "").strip()
        ]

    caveats = structured.get("caveats")
    if not isinstance(caveats, list):
        structured["caveats"] = []
    else:
        structured["caveats"] = [str(c).strip() for c in caveats if str(c).strip()]

    prose = structured.get("profile_prose") or structured.get("profile") or ""
    structured["profile_prose"] = str(prose).strip()
    return structured


def _add_staleness_caveats(
    structured: dict[str, Any],
    graph_claims: list[dict],
    evidence: list[Passage],
) -> None:
    if not graph_claims or not evidence:
        return
    newest_evidence = max((p.event_time for p in evidence if p.event_time), default=None)
    if newest_evidence is None:
        return
    stale = []
    for claim in graph_claims:
        valid_from = claim.get("valid_from")
        if not valid_from:
            continue
        try:
            claim_time = datetime.fromisoformat(valid_from)
        except ValueError:
            continue
        if claim_time.tzinfo is None:
            claim_time = claim_time.replace(tzinfo=UTC)
        age_days = (newest_evidence - claim_time).total_seconds() / 86400.0
        if age_days > 180:
            stale.append(
                f"Graph claim '{claim['predicate']}' may be stale relative to newer evidence "
                f"({int(age_days)} days older than newest passage)."
            )
    if stale:
        structured.setdefault("caveats", [])
        structured["caveats"].extend(stale)


def _build_profile_prompt(
    category: str,
    name: str,
    graph_block: str,
    passages: list[Passage],
    query: str,
) -> str:
    evidence_block = (
        "\n\n".join(
            f"[doc_id={p.doc_id}] ({p.source_type}, {p.event_time.date().isoformat()}, "
            f"by {p.author_display_name})\n{p.text}"
            for p in passages
        )
        or "(no evidence visible to this viewer)"
    )
    return (
        f"CATEGORY: {category}\n"
        f"SUBJECT: {name}\n"
        f"FOCUS_QUERY: {query or name}\n\n"
        f"GRAPH FACTS (active automatic relationships and claims):\n{graph_block}\n\n"
        f"EVIDENCE:\n{evidence_block}"
    )
