"""Worldbuilder orchestration: resolve a subject, gather evidence, synthesize.

The service composes three collaborators. ``SubjectResolver`` maps names to
viewer-visible people and entities, ``SourceReader`` loads cited material, and
the pure functions in ``profile_structure`` shape and ground the model output.
Synthesis results are cached by exact evidence set and re-grounded on every
cache hit, so a hit can never show ids the viewer has since lost.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from org_memory.core.errors import NotFoundError
from org_memory.core.settings import get_settings
from org_memory.db.engine import session_scope
from org_memory.db.orm import Person
from org_memory.db.repositories import (
    GraphRepository,
    PersonRepository,
    SpendRepository,
    SynthesisTraceRepository,
)
from org_memory.domain.models import Passage, Principal
from org_memory.services.retrieval import RetrievalService
from org_memory.services.worldbuilder.profile_structure import (
    CATEGORIES,
    WorldbuilderCategory,
    add_staleness_caveats,
    build_profile_prompt,
    category_system_prompt,
    ensure_structured_from_graph,
    graph_record_ids,
    ground_structured_profile,
    parse_structured_profile,
)
from org_memory.services.worldbuilder.read_source import SourceReader
from org_memory.services.worldbuilder.resolution import SubjectResolver

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
                # Re-ground and re-seed from the current viewer-visible graph so
                # a cache hit cannot revive ids or facts the viewer lost access to.
                structured = ground_structured_profile(
                    structured,
                    allowed_doc_ids={p.doc_id for p in evidence},
                    allowed_record_ids=graph_record_ids(relationships, claims),
                )
                source = structured.get("profile_structure_source")
                model_ok = source in ("model", "model_and_graph")
                structure_source = ensure_structured_from_graph(
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
            category_system_prompt(category),
            build_profile_prompt(category, display_name, graph_block, evidence, query),
            json_object=True,
        )
        with session_scope() as spend_session:
            SpendRepository(spend_session).record(
                "synthesis", "synthesis", self._synth.model_name, tokens
            )

        structured, model_json_ok = parse_structured_profile(profile_raw)
        structured = ground_structured_profile(
            structured,
            allowed_doc_ids={p.doc_id for p in evidence},
            allowed_record_ids=graph_record_ids(relationships, claims),
        )
        structure_source = ensure_structured_from_graph(
            structured,
            claims=claims,
            relationships=relationships,
            model_json_ok=model_json_ok,
            display_name=display_name,
        )
        add_staleness_caveats(structured, _claims_payload(claims), evidence)
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
        graph_claims = _claims_payload(claims)
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

    def _render_graph_facts(self, principal: Principal, relationships, claims) -> str:
        lines: list[str] = []
        for r, visible_doc_ids in relationships:
            from_label = self._resolver.node_label(principal, r.from_type, r.from_id)
            to_label = self._resolver.node_label(principal, r.to_type, r.to_id)
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


def _claims_payload(claims) -> list[dict]:
    return [
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
