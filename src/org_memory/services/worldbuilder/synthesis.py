"""Cache, LLM synthesize, ground, and shape Worldbuilder profile payloads.

``WorldbuilderService`` gathers viewer-visible evidence; this collaborator turns
that evidence into a structured profile, reusing a cache entry when the exact
document set is unchanged and re-grounding every hit so ACL loss cannot revive
ids the viewer can no longer see.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from org_memory.core.settings import get_settings
from org_memory.db.engine import session_scope
from org_memory.db.repositories import SpendRepository, SynthesisTraceRepository
from org_memory.domain.models import Passage, Principal
from org_memory.services.worldbuilder.profile_structure import (
    WorldbuilderCategory,
    add_staleness_caveats,
    build_profile_prompt,
    category_system_prompt,
    ensure_structured_from_graph,
    graph_record_ids,
    ground_structured_profile,
    parse_structured_profile,
)
from org_memory.services.worldbuilder.resolution import SubjectResolver


class ProfileSynthesizer:
    def __init__(
        self,
        traces: SynthesisTraceRepository,
        synthesizer,
        resolver: SubjectResolver,
    ):
        self._traces = traces
        self._synth = synthesizer
        self._resolver = resolver

    def synthesize(
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
        add_staleness_caveats(structured, claims_payload(claims), evidence)
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
        graph_claims = claims_payload(claims)
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


def claims_payload(claims) -> list[dict]:
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
