"""Entity-centric answers on viewer-scoped retrieval.

Person lookup synthesizes profiles from participant-scoped evidence and graph
facts. Ambiguous names return disambiguation. read_source loads documents by
doc_id with the same ACL checks as search.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from org_memory.core.errors import NotFoundError
from org_memory.db.engine import session_scope
from org_memory.db.orm import Chunk, Document, Person
from org_memory.db.repositories import (
    GraphRepository,
    PersonRepository,
    SpendRepository,
    SynthesisTraceRepository,
)
from org_memory.domain.models import Passage, Principal
from org_memory.services.retrieval import RetrievalService

_PROFILE_SYSTEM_PROMPT = """You are Worldbuilder, an organizational-memory profile writer.
Write a concise factual profile of the requested person using ONLY:
1. The GRAPH FACTS section (active automatic facts with visible evidence).
2. The EVIDENCE passages (each tagged with a doc_id).
Cite doc_ids inline like [source_system:external_id] after each claim drawn from evidence.
If the evidence does not support a claim, omit the claim. Never invent facts.
Structure: Role & team, Current projects, Collaborators, Recent activity."""


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

    def lookup_person(self, principal: Principal, name: str) -> dict:
        """Resolve a person and synthesize a profile from scoped evidence."""
        person = self._match_person(principal, name)
        if isinstance(person, list):
            return {
                "disambiguation": [
                    {
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

        evidence = self._retrieval.search(
            principal=principal,
            query=self._evidence_query(person),
            limit=12,
            about_person_ids=[person.canonical_id],
            tool_name="worldbuilder_lookup",
        )

        relationships = self._graph.relationships_for_viewer("person", person.canonical_id, principal)
        claims = self._graph.claims_for_viewer("person", person.canonical_id, principal, statuses=["active"])
        graph_block = self._render_graph_facts(principal, relationships, claims)

        profile_text, tokens = self._synth.complete(
            _PROFILE_SYSTEM_PROMPT,
            _build_profile_prompt(person.display_name, graph_block, evidence.passages),
        )
        with session_scope() as spend_session:
            SpendRepository(spend_session).record("synthesis", "synthesis", self._synth.model_name, tokens)

        trace_id = self._traces.record(
            principal_id=principal.principal_id,
            tool="worldbuilder_lookup",
            subject=person.canonical_id,
            model=self._synth.model_name,
            input_doc_ids=sorted({p.doc_id for p in evidence.passages}),
            output_text=profile_text,
            tokens=tokens,
        )

        return {
            "canonical_id": person.canonical_id,
            "display_name": person.display_name,
            "resolution_status": person.resolution_status,
            "profile": profile_text,
            "relationships": [
                {
                    "relationship_type": r.relationship_type,
                    "from": {"type": r.from_type, "id": r.from_id},
                    "to": {"type": r.to_type, "id": r.to_id},
                    "confidence": r.confidence,
                    "evidence_doc_ids": visible_doc_ids,
                }
                for r, visible_doc_ids in relationships
            ],
            "claims": [
                {
                    "predicate": c.predicate,
                    "object": c.object_text,
                    "confidence": c.confidence,
                    "evidence_doc_ids": visible_doc_ids,
                }
                for c, visible_doc_ids in claims
            ],
            "evidence": [p.model_dump(mode="json") for p in evidence.passages],
            "audit_id": evidence.audit_id,
            "trace_id": trace_id,
        }

    def _match_person(self, principal: Principal, name: str) -> Person | list[Person]:
        """Return one person, or a candidate list when the name is ambiguous."""
        matches = [
            p
            for p in self._persons.search_by_name(name, limit=10)
            if p.merged_into_id is None and self._persons.visible_evidence_doc_ids(p.canonical_id, principal)
        ]
        if not matches:
            raise NotFoundError(
                f"No person matching '{name}' in the entity spine. "
                "(Entities appear after their first authored content is ingested.)"
            )
        if len(matches) == 1:
            return matches[0]
        exact = [p for p in matches if p.display_name.lower() == name.strip().lower()]
        if len(exact) == 1:
            return exact[0]
        return matches

    def _evidence_query(self, person: Person) -> str:
        names = [person.display_name, *(person.name_aliases or [])]
        if self._persons is not None:
            for alias in self._persons.aliases_for(person.canonical_id):
                if alias.display_name:
                    names.append(alias.display_name)
        # Dedupe while preserving order for a stable retrieval query.
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
            lines.append(f"- {from_label} {r.relationship_type} {to_label} [{evidence}]")
        for c, visible_doc_ids in claims:
            evidence = ", ".join(visible_doc_ids[:3])
            lines.append(f"- {c.predicate}: {c.object_text} [{evidence}]")
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

    def read_source(self, principal: Principal, doc_ids: list[str]) -> dict:
        """Load full source units by doc_id with per-id outcomes under viewer ACL."""
        from org_memory.core.settings import get_settings

        results: list[dict] = []
        outcomes: list[dict] = []
        viewer = principal.all_principals()
        workspace_id = get_settings().workspace_id
        for doc_id in doc_ids:
            doc = self._session.get(Document, doc_id)
            if doc is None or doc.deleted or doc.workspace_id != workspace_id:
                outcomes.append({"doc_id": doc_id, "outcome": "not_found"})
                continue
            if not (doc.org_visible or set(doc.allowed_principals) & set(viewer)):
                outcomes.append({"doc_id": doc_id, "outcome": "forbidden"})
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
            outcomes.append({"doc_id": doc_id, "outcome": "ok"})
        return {"sources": results, "outcomes": outcomes}


def _build_profile_prompt(name: str, graph_block: str, passages: list[Passage]) -> str:
    evidence_block = (
        "\n\n".join(
            f"[doc_id={p.doc_id}] ({p.source_type}, {p.event_time.date().isoformat()}, "
            f"by {p.author_display_name})\n{p.text}"
            for p in passages
        )
        or "(no evidence visible to this viewer)"
    )
    return (
        f"PERSON: {name}\n\n"
        f"GRAPH FACTS (active automatic relationships and claims):\n{graph_block}\n\n"
        f"EVIDENCE:\n{evidence_block}"
    )
