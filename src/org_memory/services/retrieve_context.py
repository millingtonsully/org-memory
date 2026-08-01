"""Compose search + structured facts + relationship paths for agents.

One embed (via RetrievalService.search). Graph expands use explicit subject
seeds and optional name resolution — search hits do not carry person ids.
Modes control order of assembly and which channels get priority under a
token budget; they do not invent data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from org_memory.db.repositories import GraphRepository
from org_memory.domain.models import Principal, SearchResponse
from org_memory.services.chunking import count_tokens
from org_memory.services.facts_diff import diff_subject_facts
from org_memory.services.facts_query import query_subject_facts
from org_memory.services.retrieval import RetrievalService
from org_memory.services.temporality import plan_temporal_query
from org_memory.services.worldbuilder.resolution import SubjectResolver

RetrieveMode = Literal["vector_first", "graph_first", "joint"]


@dataclass(frozen=True)
class SubjectRef:
    type: str
    id: str


class RetrieveContextService:
    def __init__(
        self,
        session: Session,
        retrieval: RetrievalService,
        graph: GraphRepository | None = None,
    ):
        self._session = session
        self._retrieval = retrieval
        # Lazy: unit tests can inject a stub graph and never touch Settings.
        self._graph = graph
        self._resolver: SubjectResolver | None = None

    def _require_graph(self) -> GraphRepository:
        if self._graph is None:
            self._graph = GraphRepository(self._session)
        return self._graph

    def _require_resolver(self) -> SubjectResolver:
        if self._resolver is None:
            self._resolver = SubjectResolver(self._session)
        return self._resolver

    def retrieve(
        self,
        *,
        principal: Principal,
        query: str,
        mode: RetrieveMode = "vector_first",
        limit: int = 20,
        subjects: list[SubjectRef] | None = None,
        about: str | None = None,
        as_of: datetime | None = None,
        believed_as_of: datetime | None = None,
        path_max_depth: int = 2,
        path_limit: int = 20,
        relationship_types: list[str] | None = None,
        fact_limit_per_subject: int = 20,
        max_tokens: int | None = None,
        source_type: str | None = None,
        source_system: str | None = None,
        author: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        updated_from: datetime | None = None,
        updated_to: datetime | None = None,
        doc_id: str | None = None,
        half_life_days: float = 90.0,
        min_decay: float = 0.3,
    ) -> dict[str, Any]:
        if mode not in ("vector_first", "graph_first", "joint"):
            raise ValueError(f"unsupported retrieve mode: {mode!r}")

        resolved = self._resolve_subjects(principal, subjects or [], about)
        if resolved.get("status") == "ambiguous":
            return resolved

        subject_list: list[SubjectRef] = resolved["subjects"]
        if mode == "graph_first" and not subject_list:
            raise ValueError(
                "graph_first requires at least one subject "
                "(pass subjects[] or about=)."
            )

        temporal_plan = None
        effective_as_of = as_of
        effective_believed_as_of = believed_as_of
        effective_as_of_grain: str | None = None
        diff_from: datetime | None = None
        diff_to: datetime | None = None
        diff_axis: Literal["world", "belief"] | None = None
        if as_of is None and believed_as_of is None:
            temporal_plan = plan_temporal_query(query)
            if temporal_plan.status == "ambiguous":
                return {
                    "status": "ambiguous",
                    "detail": "temporal_axis_ambiguous",
                    "temporal_plan": temporal_plan.to_diagnostics(),
                    "disambiguation": [],
                    "query": query,
                    "mode": mode,
                    "subjects": [{"type": s.type, "id": s.id} for s in subject_list],
                    "passages": [],
                    "search_facts": [],
                    "structured_facts": [],
                    "paths": [],
                    "fact_diffs": [],
                    "truncated_tokens": False,
                }
            if temporal_plan.axis == "world":
                effective_as_of_grain = temporal_plan.grain
                if temporal_plan.range_end is not None and temporal_plan.as_of is not None:
                    effective_as_of = temporal_plan.range_end
                    diff_from = temporal_plan.as_of
                    diff_to = temporal_plan.range_end
                    diff_axis = "world"
                else:
                    effective_as_of = temporal_plan.as_of
            elif temporal_plan.axis == "belief":
                if (
                    temporal_plan.range_end is not None
                    and temporal_plan.believed_as_of is not None
                ):
                    effective_believed_as_of = temporal_plan.range_end
                    diff_from = temporal_plan.believed_as_of
                    diff_to = temporal_plan.range_end
                    diff_axis = "belief"
                else:
                    effective_believed_as_of = temporal_plan.believed_as_of

        about_person_ids = [
            s.id for s in subject_list if s.type == "person"
        ] or None
        about_doc_ids = resolved.get("about_doc_ids")

        search: SearchResponse | None = None
        structured_facts: list[dict] = []
        path_blocks: list[dict] = []
        fact_diffs: list[dict] = []

        def run_search(*, with_about: bool) -> SearchResponse:
            return self._retrieval.search(
                principal=principal,
                query=query,
                limit=limit,
                source_type=source_type,
                source_system=source_system,
                author=author,
                about_person_ids=about_person_ids if with_about else None,
                about_doc_ids=about_doc_ids if with_about else None,
                date_from=date_from,
                date_to=date_to,
                updated_from=updated_from,
                updated_to=updated_to,
                doc_id=doc_id,
                half_life_days=half_life_days,
                min_decay=min_decay,
                tool_name="retrieve_context",
                as_of=effective_as_of,
                believed_as_of=effective_believed_as_of,
                as_of_grain=effective_as_of_grain,
            )

        def run_graph() -> tuple[list[dict], list[dict], list[dict]]:
            facts_out: list[dict] = []
            paths_out: list[dict] = []
            diffs_out: list[dict] = []
            for subject in subject_list:
                facts_out.append(
                    query_subject_facts(
                        self._require_graph(),
                        subject_type=subject.type,
                        subject_id=subject.id,
                        principal=principal,
                        as_of=effective_as_of,
                        believed_as_of=effective_believed_as_of,
                        as_of_grain=effective_as_of_grain,
                        limit=fact_limit_per_subject,
                    )
                )
                if diff_axis == "world" and diff_from is not None and diff_to is not None:
                    diffs_out.append(
                        diff_subject_facts(
                            self._require_graph(),
                            subject_type=subject.type,
                            subject_id=subject.id,
                            principal=principal,
                            as_of_from=diff_from,
                            as_of_to=diff_to,
                            limit=fact_limit_per_subject,
                        )
                    )
                elif (
                    diff_axis == "belief"
                    and diff_from is not None
                    and diff_to is not None
                ):
                    diffs_out.append(
                        diff_subject_facts(
                            self._require_graph(),
                            subject_type=subject.type,
                            subject_id=subject.id,
                            principal=principal,
                            believed_as_of_from=diff_from,
                            believed_as_of_to=diff_to,
                            limit=fact_limit_per_subject,
                        )
                    )
                path_result = self._require_graph().paths_from(
                    start_type=subject.type,
                    start_id=subject.id,
                    principal=principal,
                    relationship_types=relationship_types,
                    max_depth=path_max_depth,
                    limit=path_limit,
                    as_of=effective_as_of,
                    believed_as_of=effective_believed_as_of,
                    as_of_grain=effective_as_of_grain,
                )
                paths_out.append(
                    {
                        "start": {"type": subject.type, "id": subject.id},
                        "paths": path_result["paths"],
                        "returned": path_result["returned"],
                        "limit": path_result["limit"],
                        "max_depth": path_result["max_depth"],
                        "truncated": path_result["truncated"],
                        "capped": path_result["capped"],
                        "as_of": (
                            effective_as_of.isoformat() if effective_as_of else None
                        ),
                        "believed_as_of": (
                            effective_believed_as_of.isoformat()
                            if effective_believed_as_of
                            else None
                        ),
                    }
                )
            return facts_out, paths_out, diffs_out

        if mode == "vector_first":
            search = run_search(with_about=bool(about_person_ids or about_doc_ids))
            if subject_list:
                structured_facts, path_blocks, fact_diffs = run_graph()
        elif mode == "graph_first":
            structured_facts, path_blocks, fact_diffs = run_graph()
            search = run_search(with_about=True)
        else:  # joint
            if subject_list:
                structured_facts, path_blocks, fact_diffs = run_graph()
            search = run_search(with_about=bool(subject_list))

        assert search is not None
        payload = {
            "query": query,
            "mode": mode,
            "subjects": [{"type": s.type, "id": s.id} for s in subject_list],
            "passages": [p.model_dump(mode="json") for p in search.passages],
            "search_facts": [f.model_dump(mode="json") for f in search.facts],
            "structured_facts": structured_facts,
            "fact_diffs": fact_diffs,
            "paths": path_blocks,
            "total_candidates": search.total_candidates,
            "reranked": search.reranked,
            "audit_id": search.audit_id,
            "as_of": effective_as_of.isoformat() if effective_as_of else None,
            "believed_as_of": (
                effective_believed_as_of.isoformat()
                if effective_believed_as_of
                else None
            ),
            "truncated_tokens": False,
            "max_tokens": max_tokens,
        }
        if max_tokens is not None:
            payload = _apply_token_budget(payload, max_tokens=max_tokens, mode=mode)
        payload["diagnostics"] = {
            "search": dict(search.diagnostics or {}),
            "temporal_plan": (
                temporal_plan.to_diagnostics() if temporal_plan is not None else None
            ),
            "graph": {
                "subject_count": len(subject_list),
                "structured_fact_blocks": len(structured_facts),
                "structured_facts_returned": sum(
                    int(block.get("returned") or 0) for block in structured_facts
                ),
                "structured_facts_truncated_any": any(
                    bool(block.get("truncated")) for block in structured_facts
                ),
                "fact_diff_blocks": len(fact_diffs),
                "fact_diffs_changed": sum(
                    int((block.get("counts") or {}).get("changed") or 0)
                    for block in fact_diffs
                ),
                "path_blocks": len(path_blocks),
                "paths_returned": sum(
                    int(block.get("returned") or 0) for block in path_blocks
                ),
                "paths_truncated_any": any(
                    bool(block.get("truncated")) for block in path_blocks
                ),
                "paths_capped_any": any(
                    bool(block.get("capped")) for block in path_blocks
                ),
                "path_max_depth_requested": path_max_depth,
                "path_limit_requested": path_limit,
            },
            "packing": {
                "max_tokens": max_tokens,
                "truncated_tokens": bool(payload.get("truncated_tokens")),
            },
        }
        return payload

    def _resolve_subjects(
        self,
        principal: Principal,
        subjects: list[SubjectRef],
        about: str | None,
    ) -> dict[str, Any]:
        out: list[SubjectRef] = []
        seen: set[tuple[str, str]] = set()
        for subject in subjects:
            key = (subject.type.strip().lower(), subject.id.strip())
            if not key[0] or not key[1] or key in seen:
                continue
            seen.add(key)
            out.append(SubjectRef(type=key[0], id=key[1]))

        about_doc_ids: list[str] | None = None
        if about and about.strip():
            resolved = self._require_resolver().resolve_about_subject(principal, about.strip())
            if resolved.get("kind") == "ambiguous":
                return {
                    "status": "ambiguous",
                    "detail": resolved.get("detail"),
                    "disambiguation": resolved.get("disambiguation", []),
                    "query": None,
                    "mode": None,
                    "subjects": [],
                    "passages": [],
                    "search_facts": [],
                    "structured_facts": [],
                    "paths": [],
                    "fact_diffs": [],
                    "truncated_tokens": False,
                }
            if resolved.get("kind") == "person":
                person_ids = list(resolved.get("about_person_ids") or [])
                for person_id in person_ids:
                    key = ("person", person_id)
                    if key not in seen:
                        seen.add(key)
                        out.append(SubjectRef(type="person", id=person_id))
            elif resolved.get("kind") == "entity":
                entity_id = str(resolved.get("canonical_id") or "").strip()
                entity_type = str(resolved.get("category") or "entity").strip().lower()
                if entity_id and entity_type:
                    key = (entity_type, entity_id)
                    if key not in seen:
                        seen.add(key)
                        out.append(SubjectRef(type=entity_type, id=entity_id))
            about_doc_ids = list(resolved.get("about_doc_ids") or []) or None

        return {"status": "ok", "subjects": out, "about_doc_ids": about_doc_ids}


def _apply_token_budget(
    payload: dict[str, Any],
    *,
    max_tokens: int,
    mode: RetrieveMode,
) -> dict[str, Any]:
    """Drop lowest-priority channels until the packed JSON-ish text fits."""
    if max_tokens < 1:
        raise ValueError("max_tokens must be >= 1")

    def measure(data: dict[str, Any]) -> int:
        return count_tokens(str(data))

    if measure(payload) <= max_tokens:
        return payload

    # Priority: keep earlier channels; trim later lists first.
    if mode == "graph_first":
        trim_order = ("passages", "search_facts", "paths", "fact_diffs", "structured_facts")
    elif mode == "joint":
        trim_order = ("paths", "fact_diffs", "search_facts", "structured_facts", "passages")
    else:
        trim_order = ("paths", "fact_diffs", "structured_facts", "search_facts", "passages")

    trimmed = dict(payload)
    for key in trim_order:
        while measure(trimmed) > max_tokens:
            value = trimmed.get(key)
            if isinstance(value, list) and value:
                trimmed[key] = value[:-1]
                trimmed["truncated_tokens"] = True
                continue
            break
        if measure(trimmed) <= max_tokens:
            break
    return trimmed
