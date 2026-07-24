"""Extract entities, relationships, and claims from document text.
"""

from __future__ import annotations

import hashlib
import json

import structlog
from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from org_memory.core.errors import VendorAPIError
from org_memory.core.settings import get_settings
from org_memory.db.engine import session_scope
from org_memory.db.orm import Claim, Document, ExtractionWindow, Relationship
from org_memory.db.repositories import (
    GraphRepository,
    PersonRepository,
    SpendRepository,
)
from org_memory.domain.fact_lifecycle import FactStatus, status_for_confidence
from org_memory.ports.embedder import Embedder
from org_memory.taxonomy_registry import get_taxonomy_registry

logger = structlog.get_logger(__name__)

# Extraction windows use a large char budget (~32k chars, roughly 8k tokens on
# English prose). This is deliberately much larger than a retrieval child chunk:
# complete extraction cost is linear in source length, but larger windows avoid
# repeating input/output overhead.
_WINDOW_TARGET_CHARS = 32000
# Tail of each window repeated at the start of the next, so a fact split
# across a boundary is seen whole at least once. Duplicated extractions are
# deduped by the graph repositories.
_WINDOW_OVERLAP_CHARS = 2000
_MAX_DOCUMENT_CONTEXT_CHARS = 4000

_EXTRACTION_SYSTEM_PROMPT_TEMPLATE = """You extract evidence-backed organizational observations.
Return ONLY a JSON object with this exact shape (no markdown fences):
{{
  "document_context": "compact named subjects, aliases, and referents for later segments",
  "entities": [
    {{
      "type": "free-form lowercase kind",
      "name": "...",
      "description": "...",
      "evidence_quote": "..."
    }}
  ],
  "relationships": [
    {{
      "from": {{"type": "person" | "entity", "name": "..."}},
      "to": {{"type": "person" | "entity", "name": "..."}},
      "relationship_type": "registry relationship_type key",
      "evidence_quote": "verbatim supporting text from this segment",
      "confidence": 0.0-1.0
    }}
  ],
  "claims": [
    {{
      "subject": {{"type": "person" | "entity", "name": "..."}},
      "predicate": "registry predicate key",
      "object": "...",
      "evidence_quote": "verbatim supporting text from this segment",
      "confidence": 0.0-1.0
    }}
  ]
}}
Rules:
- Only extract observations explicitly supported by this segment. Do not infer
  an organizational relationship merely from co-occurrence.
- evidence_quote is mandatory and must be copied verbatim from this segment.
{schema_block}
- The supplied prior document context is for resolving references only. It is
  not evidence and must never be quoted or emitted as a fact by itself.
- confidence reflects how explicitly the text supports the fact.
- Prefer few high-quality extractions over many speculative ones.
- Empty arrays are fine when the text contains no organizational facts."""


def split_windows(text: str) -> list[str]:
    """Split text into paragraph-aligned extraction windows covering ALL of it.

    Windows target _WINDOW_TARGET_CHARS and carry _WINDOW_OVERLAP_CHARS of the
    previous window's tail so boundary-spanning facts are seen whole. A single
    paragraph longer than the target becomes its own window (never truncated).
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= _WINDOW_TARGET_CHARS:
        return [text]

    paragraphs = [p for p in (part.strip() for part in text.split("\n\n")) if p]
    windows: list[str] = []
    current: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current:
            windows.append("\n\n".join(current))
            current = []
            current_len = 0

    for para in paragraphs:
        if current and current_len + len(para) + 2 > _WINDOW_TARGET_CHARS:
            tail = "\n\n".join(current)
            flush()
            # Overlap: seed the next window with the tail of the previous one.
            overlap = tail[-_WINDOW_OVERLAP_CHARS:]
            current = [overlap]
            current_len = len(overlap) + 2
        current.append(para)
        current_len += len(para) + 2
    flush()
    return windows


class ExtractionService:
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
        header = f"Source: {doc.source_system} | Title: {doc.title or '(untitled)'}\n"
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

    def _apply_extraction(self, doc: Document, parsed: dict, summary: dict, source_window: str) -> None:
        """Write one window's parsed facts to the graph, updating summary counts."""
        activation_confidence = get_settings().fact_activation_confidence
        registry = get_taxonomy_registry()

        for item in parsed.get("entities", []):
            entity_type = str(item.get("type", "")).strip().lower()
            name = str(item.get("name", "")).strip()
            if not entity_type or not name:
                continue
            if not registry.is_known_entity_type(entity_type):
                summary["dropped_untyped"] += 1
                continue
            if not _quote_is_supported(item.get("evidence_quote"), source_window):
                summary["dropped_unverifiable"] += 1
                continue
            self._graph.upsert_entity(
                entity_type,
                name,
                description=item.get("description", ""),
                evidence_doc_id=doc.doc_id,
            )
            summary["entities"] += 1

        for item in parsed.get("relationships", []):
            if not _quote_is_supported(item.get("evidence_quote"), source_window):
                summary["dropped_unverifiable"] += 1
                continue
            from_ref = self._resolve_ref(item.get("from", {}), summary)
            to_ref = self._resolve_ref(item.get("to", {}), summary)
            rel_type = item.get("relationship_type", "").strip().lower()
            if from_ref is None or to_ref is None or not rel_type:
                continue
            if not registry.is_known_relationship_type(rel_type):
                # Unknown types never become active organizational truth.
                summary["dropped_untyped"] += 1
                continue
            rel_def = registry.relationship_types[rel_type]
            if from_ref[0] not in rel_def.from_types or to_ref[0] not in rel_def.to_types:
                summary["dropped_untyped"] += 1
                continue
            confidence = _parse_confidence(item.get("confidence"))
            status = status_for_confidence(confidence, activation_confidence).value
            evidence_quote = str(item["evidence_quote"])
            self._graph.add_relationship(
                Relationship(
                    workspace_id=doc.workspace_id,
                    from_type=from_ref[0],
                    from_id=from_ref[1],
                    to_type=to_ref[0],
                    to_id=to_ref[1],
                    from_label=str(item.get("from", {}).get("name", "")).strip(),
                    to_label=str(item.get("to", {}).get("name", "")).strip(),
                    relationship_type=rel_type,
                    confidence=confidence,
                    status=status,
                    evidence_doc_ids=[doc.doc_id],
                    evidence_quotes=[{"doc_id": doc.doc_id, "quote": evidence_quote}],
                    origin_from_id=from_ref[1],
                    origin_to_id=to_ref[1],
                    created_by="extraction",
                    decided_by=("automatic:confidence_gate" if status == "active" else ""),
                    valid_from=doc.event_time,
                )
            )
            summary["relationships"] += 1
            summary[f"{status}_facts"] += 1

        for item in parsed.get("claims", []):
            if not _quote_is_supported(item.get("evidence_quote"), source_window):
                summary["dropped_unverifiable"] += 1
                continue
            subject = self._resolve_ref(item.get("subject", {}), summary)
            predicate = item.get("predicate", "").strip().lower()
            object_text = str(item.get("object", "")).strip()
            if subject is None or not predicate or not object_text:
                continue
            if not registry.is_known_predicate(predicate):
                # Persist as proposed-only untyped audit trail; never activate.
                summary["dropped_untyped"] += 1
                self._graph.add_claim(
                    Claim(
                        workspace_id=doc.workspace_id,
                        subject_type=subject[0],
                        subject_id=subject[1],
                        predicate=predicate,
                        object_text=object_text,
                        confidence=_parse_confidence(item.get("confidence")),
                        status=FactStatus.proposed.value,
                        evidence_doc_ids=[doc.doc_id],
                        evidence_quotes=[
                            {"doc_id": doc.doc_id, "quote": str(item["evidence_quote"])}
                        ],
                        origin_subject_id=subject[1],
                        created_by="extraction:untyped",
                        decided_by="",
                        valid_from=doc.event_time,
                    )
                )
                summary["claims"] += 1
                summary["proposed_facts"] += 1
                continue
            confidence = _parse_confidence(item.get("confidence"))
            status = status_for_confidence(confidence, activation_confidence).value
            evidence_quote = str(item["evidence_quote"])
            stored = self._graph.add_claim(
                Claim(
                    workspace_id=doc.workspace_id,
                    subject_type=subject[0],
                    subject_id=subject[1],
                    predicate=predicate,
                    object_text=object_text,
                    confidence=confidence,
                    status=status,
                    evidence_doc_ids=[doc.doc_id],
                    evidence_quotes=[{"doc_id": doc.doc_id, "quote": evidence_quote}],
                    origin_subject_id=subject[1],
                    created_by="extraction",
                    decided_by=("automatic:confidence_gate" if status == "active" else ""),
                    valid_from=doc.event_time,
                )
            )
            if stored.status == FactStatus.active.value:
                self.active_claim_slots.add(
                    (stored.subject_type, stored.subject_id, stored.predicate)
                )
            summary["claims"] += 1
            summary[f"{status}_facts"] += 1

    def _resolve_ref(self, ref: dict, summary: dict) -> tuple[str, str] | None:
        """Map an extracted type and name to a graph node id."""
        ref_type = ref.get("type", "")
        name = ref.get("name", "").strip()
        if not name:
            return None
        if ref_type == "person":
            person_matches = self._persons.search_by_name(name, limit=2)
            normalized = self._graph.normalize_name(name)
            exact_people = [
                p
                for p in person_matches
                if normalized
                in {
                    self._graph.normalize_name(p.display_name),
                    *(self._graph.normalize_name(alias) for alias in (p.name_aliases or [])),
                }
            ]
            if len(exact_people) == 1:
                return ("person", exact_people[0].canonical_id)
            # Embeddings only propose identity merges elsewhere; never bind
            # extraction mentions from semantic similarity alone.
            summary["skipped_mentions"] += 1
            return None
        if ref_type == "entity":
            entity_matches = self._graph.search_entities(name, limit=2)
            exact_entities = [
                entity
                for entity in entity_matches
                if entity.normalized_name == self._graph.normalize_name(name)
            ]
            if len(exact_entities) != 1:
                summary["skipped_mentions"] += 1
                return None
            entity = exact_entities[0]
            return (entity.entity_type, entity.entity_id)
        return None


def _normalize_evidence(value: str) -> str:
    return " ".join(value.split()).casefold()


def _quote_is_supported(quote: object, source_window: str) -> bool:
    """Require non-trivial verbatim evidence after whitespace normalization."""
    if not isinstance(quote, str):
        return False
    normalized_quote = _normalize_evidence(quote)
    if len(normalized_quote) < 8:
        return False
    return normalized_quote in _normalize_evidence(source_window)


def _parse_confidence(value: object) -> float:
    """Parse an LLM confidence without allowing malformed values through."""
    if isinstance(value, bool):
        raise ValueError("Extractor confidence must be a number between 0 and 1")
    if not isinstance(value, (int, float, str)):
        raise ValueError("Extractor confidence must be a number between 0 and 1")
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Extractor confidence must be a number between 0 and 1") from exc
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("Extractor confidence must be a number between 0 and 1")
    return confidence
