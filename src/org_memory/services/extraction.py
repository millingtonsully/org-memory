"""Extract entities, relationships, and claims from document text.

Window splitting lives in ``extraction_windows``. Graph apply lives in
``extraction_apply``. This module owns the LLM window loop and durable
checkpoints.
"""

from __future__ import annotations

import hashlib
import json

import structlog
from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from org_memory.core.errors import VendorAPIError
from org_memory.db.engine import session_scope
from org_memory.db.orm import Document, ExtractionWindow
from org_memory.db.repositories import (
    GraphRepository,
    PersonRepository,
    SpendRepository,
)
from org_memory.ports.embedder import Embedder
from org_memory.services.extraction_apply import ExtractionApplyMixin
from org_memory.services.extraction_windows import split_windows
from org_memory.taxonomy_registry import get_taxonomy_registry

logger = structlog.get_logger(__name__)

_MAX_DOCUMENT_CONTEXT_CHARS = 4000

_EXTRACTION_SYSTEM_PROMPT_TEMPLATE = """You extract evidence-backed organizational observations.
Return ONLY a JSON object with this exact shape (no markdown fences):
{{
  "document_context": "compact named subjects, aliases, and referents for later segments",
  "entities": [
    {{
      "type": "person|team|project|glossary",
      "name": "...",
      "description": "...",
      "evidence_quote": "verbatim supporting text from this segment"
    }}
  ],
  "relationships": [
    {{
      "from": {{"type": "person|team|project|glossary", "name": "..."}},
      "to": {{"type": "person|team|project|glossary", "name": "..."}},
      "relationship_type": "registry relationship_type key",
      "evidence_quote": "verbatim supporting text from this segment",
      "confidence": 0.0-1.0,
      "valid_from": "ISO-8601 instant or null",
      "valid_to": "ISO-8601 instant or null",
      "time_grain": "day|month|quarter|year|unknown",
      "time_expression": "raw time phrase from the segment or empty"
    }}
  ],
  "claims": [
    {{
      "subject": {{"type": "person|team|project|glossary", "name": "..."}},
      "predicate": "registry predicate key",
      "object": "...",
      "evidence_quote": "verbatim supporting text from this segment",
      "confidence": 0.0-1.0,
      "valid_from": "ISO-8601 instant or null",
      "valid_to": "ISO-8601 instant or null",
      "time_grain": "day|month|quarter|year|unknown",
      "time_expression": "raw time phrase from the segment or empty"
    }}
  ]
}}
Rules:
- Only extract observations explicitly supported by this segment. Do not infer
  an organizational relationship merely from co-occurrence.
- evidence_quote is mandatory and must be copied verbatim from this segment.
- Document reference time (t_ref) for relative phrases is provided in the
  segment header. Prefer explicit dates in the text; otherwise leave
  valid_from null so the service grounds against t_ref. time_grain must not
  be finer than the evidence (e.g. "March" → month, not day).
{schema_block}
- Entity type guidance:
  - person: named humans (employees, contractors). Prefer claims/relationships over
    inventing duplicate person entities when the text only mentions a person.
  - team: named org groups/squads/departments (e.g. "Platform", "Clinical Ops").
  - project: named initiatives/workstreams with a clear label in the text.
  - glossary: internal acronyms/terms whose meaning is explained or used as jargon
    (e.g. "CarePod", "ICR"). For every glossary entity, ALSO emit a claim with
    predicate "definition" whose object is the org-specific meaning, with the same
    evidence_quote when possible.
- Relationship guidance:
  - member_of: person → team when membership is explicit.
  - reports_to: person → person when management is explicit.
  - uses_term: person|project → glossary when they clearly use that term.
- Subject/object `type` must be one of the allowed entity types (or person), never
  the opaque label "entity".
- The supplied prior document context is for resolving references only. It is
  not evidence and must never be quoted or emitted as a fact by itself.
- confidence reflects how explicitly the text supports the fact.
- Prefer few high-quality extractions over many speculative ones.
- Empty arrays are fine when the text contains no organizational facts."""


class ExtractionService(ExtractionApplyMixin):
    def __init__(self, session: Session, synthesizer, embedder: Embedder):
        self._session = session
        self._graph = GraphRepository(session)
        self._persons = PersonRepository(session)
        self._synthesizer = synthesizer
        self._embedder = embedder
        self._mention_cache: dict[str, tuple[str, str] | None] = {}
        # (subject_type, subject_id, predicate) slots this run left active. The
        # worker checks these for same-predicate contradictions afterwards.
        self.active_claim_slots: set[tuple[str, str, str]] = set()

    def extract_for_document(self, doc: Document, heartbeat=None) -> dict:
        """Extract graph facts for one document, covering its full text.

        LLM output and graph applies for each window are committed durably so a
        mid-document failure retries only unfinished windows.
        """
        self.active_claim_slots = set()
        header = (
            f"Source: {doc.source_system} | Title: {doc.title or '(untitled)'}\n"
            f"Document reference time (t_ref): {doc.event_time.isoformat()}\n"
        )
        if doc.author_display_name:
            header += f"Author: {doc.author_display_name}\n"

        windows = split_windows(doc.rendered_text)
        content_hash = hashlib.sha256(doc.rendered_text.encode("utf-8")).hexdigest()
        summary = {
            "entities": 0,
            "relationships": 0,
            "claims": 0,
            "active_facts": 0,
            "proposed_facts": 0,
            "skipped_mentions": 0,
            "dropped_unverifiable": 0,
            "dropped_untyped": 0,
            "cache_hits": 0,
            "applied_skips": 0,
        }
        total_tokens = 0
        document_context = ""
        system_prompt = _EXTRACTION_SYSTEM_PROMPT_TEMPLATE.format(
            schema_block=get_taxonomy_registry().prompt_constraint_block()
        )

        with session_scope() as probe:
            applied_count = (
                probe.query(ExtractionWindow)
                .filter(
                    ExtractionWindow.doc_id == doc.doc_id,
                    ExtractionWindow.content_hash == content_hash,
                    ExtractionWindow.applied == True,  # noqa: E712
                )
                .count()
            )
        if applied_count == 0:
            with session_scope() as wipe_session:
                GraphRepository(wipe_session).remove_extraction_evidence(doc.doc_id)

        for window_index, window_text in enumerate(windows):
            if heartbeat is not None:
                heartbeat()
            window_hash = hashlib.sha256(window_text.encode("utf-8")).hexdigest()
            with session_scope() as durable:
                row = durable.get(ExtractionWindow, (doc.doc_id, content_hash, window_index))
                if (
                    row is not None
                    and row.window_hash == window_hash
                    and row.applied
                ):
                    parsed = dict(row.parsed_output)
                    summary["cache_hits"] += 1
                    summary["applied_skips"] += 1
                else:
                    tokens = 0
                    if row is not None and row.window_hash == window_hash:
                        parsed = dict(row.parsed_output)
                        summary["cache_hits"] += 1
                        tokens = row.tokens
                    else:
                        window_header = header
                        if len(windows) > 1:
                            window_header += (
                                f"Segment {window_index + 1} of {len(windows)}\n"
                            )
                        if document_context:
                            window_header += (
                                "\nPRIOR DOCUMENT CONTEXT "
                                "(reference resolution only; not evidence):\n"
                                f"{document_context}\n"
                            )
                        raw, tokens = self._synthesizer.complete(
                            system_prompt, window_header + "\n" + window_text
                        )
                        total_tokens += tokens
                        try:
                            parsed = json.loads(
                                raw.strip()
                                .removeprefix("```json")
                                .removesuffix("```")
                                .strip()
                            )
                        except json.JSONDecodeError as exc:
                            raise VendorAPIError(
                                "extraction",
                                200,
                                f"Extractor returned non-JSON output for "
                                f"segment {window_index + 1}/{len(windows)}",
                                raw_response=raw,
                            ) from exc

                    old_graph, old_persons = self._graph, self._persons
                    self._graph = GraphRepository(durable)
                    self._persons = PersonRepository(durable)
                    try:
                        self._apply_extraction(doc, parsed, summary, window_text)
                    finally:
                        self._graph, self._persons = old_graph, old_persons

                    durable.merge(
                        ExtractionWindow(
                            doc_id=doc.doc_id,
                            content_hash=content_hash,
                            window_index=window_index,
                            window_hash=window_hash,
                            parsed_output=parsed,
                            tokens=tokens,
                            applied=True,
                        )
                    )
                    if tokens and (row is None or row.window_hash != window_hash):
                        SpendRepository(durable).record(
                            "extraction",
                            "synthesis",
                            self._synthesizer.model_name,
                            tokens,
                        )

            emitted_context = str(parsed.get("document_context", "")).strip()
            if emitted_context:
                document_context = emitted_context[-_MAX_DOCUMENT_CONTEXT_CHARS:]

        self._session.expire_all()
        self.active_claim_slots = self._active_slots_for_document(doc)

        logger.info(
            "extraction.completed",
            doc_id=doc.doc_id,
            tokens=total_tokens,
            windows=len(windows),
            **summary,
        )
        return summary

    def _active_slots_for_document(self, doc: Document) -> set[tuple[str, str, str]]:
        rows = self._session.execute(
            sql(
                """
                SELECT subject_type, subject_id, predicate
                FROM claims
                WHERE workspace_id = :workspace_id
                  AND status = 'active'
                  AND :doc_id = ANY(evidence_doc_ids)
                """
            ),
            {"workspace_id": doc.workspace_id, "doc_id": doc.doc_id},
        ).fetchall()
        return {(r.subject_type, r.subject_id, r.predicate) for r in rows}
