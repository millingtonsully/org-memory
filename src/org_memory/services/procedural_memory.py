"""Create and retrieve access control list-scoped procedural memories from real agent events."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from org_memory.core.errors import VendorAPIError
from org_memory.core.settings import get_settings
from org_memory.db.engine import session_scope
from org_memory.db.orm import ProceduralMemory, utcnow
from org_memory.db.repositories import (
    AuditRepository,
    GraphRepository,
    ProceduralMemoryRepository,
    SpendRepository,
)
from org_memory.domain.models import Principal
from org_memory.ports.embedder import Embedder
from org_memory.ports.reranker import Reranker

_PROCEDURAL_SYSTEM_PROMPT = """You summarize a completed agent episode for later recall.
Use only the supplied objective and numbered source events. Do not invent an
action, result, URL, identifier, error, or outcome. Return ONLY JSON:
{"summary": "concise account of what was done, what worked, and what failed",
 "referenced_event_indices": [0, 1]}
Every material statement in summary must be traceable to a referenced event.
The original events are stored separately and remain the authoritative record."""


class ProceduralMemoryService:
    def __init__(
        self,
        session: Session,
        synthesizer,
        embedder: Embedder,
        reranker: Reranker,
    ):
        self._session = session
        self._repo = ProceduralMemoryRepository(session)
        self._graph = GraphRepository(session)
        self._audit = AuditRepository(session)
        self._synthesizer = synthesizer
        self._embedder = embedder
        self._reranker = reranker

    def create(
        self,
        *,
        principal: Principal,
        agent_id: str,
        run_id: str,
        procedure_key: str,
        objective: str,
        events: list[dict],
        evidence_doc_ids: list[str],
        org_visible: bool,
        allowed_principals: list[str],
    ) -> ProceduralMemory:
        if not org_visible and not allowed_principals:
            raise ValueError("Procedural memory requires org_visible=true or allowed_principals.")
        if not org_visible and not (set(allowed_principals) & set(principal.all_principals())):
            raise ValueError("The creating viewer must be included in the procedural memory ACL.")
        visible_evidence = self._graph.visible_evidence_doc_ids(evidence_doc_ids, principal)
        if len(set(visible_evidence)) != len(set(evidence_doc_ids)):
            raise ValueError("Every evidence_doc_id must exist and be visible to the creating viewer.")

        event_block = "\n\n".join(
            f"EVENT {index}\n"
            f"actor: {event['actor']}\n"
            f"action: {event['action']}\n"
            f"result: {event['result']}\n"
            f"metadata: {json.dumps(event.get('metadata', {}), sort_keys=True)}"
            for index, event in enumerate(events)
        )
        prompt = f"OBJECTIVE:\n{objective}\n\nSOURCE EVENTS:\n{event_block}"
        max_chars = get_settings().procedural_max_source_chars
        if len(prompt) > max_chars:
            raise ValueError(
                f"Procedural source is {len(prompt):,} characters; maximum is "
                f"{max_chars:,}. Split the episode into smaller procedures."
            )
        raw, synthesis_tokens = self._synthesizer.complete(_PROCEDURAL_SYSTEM_PROMPT, prompt)
        # Record paid vendor work independently so a later validation or
        # database failure cannot erase real spend.
        with session_scope() as spend_session:
            SpendRepository(spend_session).record(
                "procedural",
                "synthesis",
                self._synthesizer.model_name,
                synthesis_tokens,
            )
        try:
            parsed = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
            summary = str(parsed["summary"]).strip()
            referenced = parsed["referenced_event_indices"]
            if not summary:
                raise ValueError("summary is empty")
            if (
                not isinstance(referenced, list)
                or not referenced
                or any(
                    isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= len(events)
                    for index in referenced
                )
            ):
                raise ValueError("referenced_event_indices are invalid")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise VendorAPIError(
                "procedural-synthesis",
                200,
                f"invalid grounded JSON: {exc}",
                raw_response=raw,
            ) from exc

        embedding_text = f"Objective: {objective}\nSummary: {summary}\n" + "\n".join(
            f"Action: {event['action']}\nResult: {event['result']}" for event in events
        )
        if len(embedding_text) > max_chars:
            raise VendorAPIError(
                "procedural-synthesis",
                200,
                "summary made the embedding payload exceed the configured source limit",
                raw_response=raw,
            )
        vectors, embedding_tokens = self._embedder.embed_texts([embedding_text])
        with session_scope() as spend_session:
            SpendRepository(spend_session).record(
                "procedural_embed",
                "embedding",
                self._embedder.model_name,
                embedding_tokens,
            )

        memory = ProceduralMemory(
            workspace_id=get_settings().workspace_id,
            agent_id=agent_id,
            run_id=run_id,
            procedure_key=procedure_key,
            objective=objective,
            summary=summary,
            events=events,
            raw_synthesis=raw,
            evidence_doc_ids=sorted(set(evidence_doc_ids)),
            org_visible=org_visible,
            allowed_principals=sorted(set(allowed_principals)),
            synthesis_model=self._synthesizer.model_name,
            embedding=vectors[0],
            embedding_model=self._embedder.model_name,
            created_by_principal=principal.principal_id,
        )
        self._repo.add(memory)
        self._session.flush()
        for old in self._repo.active_for_key(agent_id, procedure_key, principal.principal_id):
            if old.memory_id == memory.memory_id:
                continue
            old.status = "superseded"
            old.superseded_by_memory_id = memory.memory_id
            old.updated_at = utcnow()
        return memory

    def search(
        self,
        *,
        principal: Principal,
        query: str,
        agent_id: str | None,
        limit: int,
    ) -> dict:
        settings = get_settings()
        vectors, embedding_tokens = self._embedder.embed_texts([query])
        with session_scope() as spend_session:
            SpendRepository(spend_session).record(
                "procedural_search",
                "embedding",
                self._embedder.model_name,
                embedding_tokens,
            )
        candidates = self._repo.search_candidates(
            query_text=query,
            query_embedding=vectors[0],
            embedding_model=self._embedder.model_name,
            principal=principal,
            agent_id=agent_id,
            limit=settings.rerank_candidates,
            rrf_k=settings.rrf_k,
        )
        if not candidates:
            audit_id = self._audit.record_retrieval(
                principal,
                "search_procedural_memory",
                query,
                {"limit": limit, "agent_id": agent_id},
                [],
                memory_ids=[],
            )
            return {"memories": [], "audit_id": audit_id}

        if len(candidates) <= limit:
            ordered = list(zip(candidates[:limit], range(len(candidates[:limit]), 0, -1), strict=True))
            did_rerank = False
        else:
            documents = [
                f"Objective: {memory.objective}\nSummary: {memory.summary}" for memory in candidates
            ]
            scores, rerank_tokens = self._reranker.rerank(query, documents)
            with session_scope() as spend_session:
                SpendRepository(spend_session).record(
                    "procedural_rerank",
                    "rerank",
                    self._reranker.model_name,
                    rerank_tokens,
                )
            ordered = sorted(
                zip(candidates, scores, strict=True),
                key=lambda item: item[1],
                reverse=True,
            )[:limit]
            did_rerank = True
        memory_ids = [memory.memory_id for memory, _ in ordered]
        audit_id = self._audit.record_retrieval(
            principal,
            "search_procedural_memory",
            query,
            {"limit": limit, "agent_id": agent_id, "reranked": did_rerank},
            [],
            memory_ids=memory_ids,
        )
        return {
            "memories": [
                {
                    "memory_id": memory.memory_id,
                    "agent_id": memory.agent_id,
                    "run_id": memory.run_id,
                    "procedure_key": memory.procedure_key,
                    "objective": memory.objective,
                    "summary": memory.summary,
                    "events": memory.events,
                    "evidence_doc_ids": self._graph.visible_evidence_doc_ids(
                        memory.evidence_doc_ids, principal
                    ),
                    "score": score,
                    "created_at": memory.created_at.isoformat(),
                }
                for memory, score in ordered
            ],
            "audit_id": audit_id,
        }
