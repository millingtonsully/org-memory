"""Apply one extraction window's parsed JSON into the graph.

Bound onto ``ExtractionService`` as a mixin so the LLM/window loop stays in
``extraction.py`` while entity/relationship/claim writes live here.
"""

from __future__ import annotations

from org_memory.core.settings import get_settings
from org_memory.db.orm import Claim, Document, Relationship
from org_memory.db.repositories import GraphRepository, PersonRepository
from org_memory.domain.fact_lifecycle import FactStatus, status_for_confidence
from org_memory.services.temporality.eager_close import (
    eager_close_claim_slot,
    eager_close_relationship_slot,
)
from org_memory.services.temporality.grounding import ground_fact_times
from org_memory.taxonomy_registry import get_taxonomy_registry


class ExtractionApplyMixin:
    """Graph-write half of extraction (expects ``_graph``, ``_persons``, slots)."""

    _graph: GraphRepository
    _persons: PersonRepository
    active_claim_slots: set[tuple[str, str, str]]

    def _apply_extraction(
        self, doc: Document, parsed: dict, summary: dict, source_window: str
    ) -> None:
        """Write one window's parsed facts to the graph, updating summary counts."""
        activation_confidence = get_settings().fact_activation_confidence
        registry = get_taxonomy_registry()
        glossary_seeded: list[tuple[str, str, str]] = []

        for item in parsed.get("entities", []):
            entity_type = str(item.get("type", "")).strip().lower()
            name = str(item.get("name", "")).strip()
            if not entity_type or not name:
                continue
            if entity_type == "person":
                # People are bound through identity resolution only.
                summary["skipped_mentions"] += 1
                continue
            if not registry.is_known_entity_type(entity_type):
                summary["dropped_untyped"] += 1
                continue
            if not quote_is_supported(item.get("evidence_quote"), source_window):
                summary["dropped_unverifiable"] += 1
                continue
            description = str(item.get("description", "") or "").strip()
            entity = self._graph.upsert_entity(
                entity_type,
                name,
                description=description,
                evidence_doc_id=doc.doc_id,
            )
            summary["entities"] += 1
            if entity_type == "glossary":
                quote = str(item["evidence_quote"])
                glossary_seeded.append((entity.entity_id, description, quote))

        for item in parsed.get("relationships", []):
            if not quote_is_supported(item.get("evidence_quote"), source_window):
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
            confidence = parse_confidence(item.get("confidence"))
            status = status_for_confidence(confidence, activation_confidence).value
            evidence_quote = str(item["evidence_quote"])
            grounded = ground_fact_times(item, t_ref=doc.event_time)
            if grounded is None:
                summary["dropped_unverifiable"] += 1
                continue
            stored_rel = self._graph.add_relationship(
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
                    valid_from=grounded.valid_from,
                    valid_to=grounded.valid_to,
                    time_grain=grounded.time_grain,
                )
            )
            if stored_rel.status == FactStatus.active.value:
                eager_close_relationship_slot(self._graph, stored_rel)
            summary["relationships"] += 1
            summary[f"{status}_facts"] += 1

        for item in parsed.get("claims", []):
            if not quote_is_supported(item.get("evidence_quote"), source_window):
                summary["dropped_unverifiable"] += 1
                continue
            subject = self._resolve_ref(item.get("subject", {}), summary)
            predicate = item.get("predicate", "").strip().lower()
            object_text = str(item.get("object", "")).strip()
            if subject is None or not predicate or not object_text:
                continue
            grounded = ground_fact_times(item, t_ref=doc.event_time)
            if grounded is None:
                summary["dropped_unverifiable"] += 1
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
                        confidence=parse_confidence(item.get("confidence")),
                        status=FactStatus.proposed.value,
                        evidence_doc_ids=[doc.doc_id],
                        evidence_quotes=[
                            {"doc_id": doc.doc_id, "quote": str(item["evidence_quote"])}
                        ],
                        origin_subject_id=subject[1],
                        created_by="extraction:untyped",
                        decided_by="",
                        valid_from=grounded.valid_from,
                        valid_to=grounded.valid_to,
                        time_grain=grounded.time_grain,
                    )
                )
                summary["claims"] += 1
                summary["proposed_facts"] += 1
                continue
            pred_def = registry.predicates[predicate]
            if subject[0] not in pred_def.subject_types:
                summary["dropped_untyped"] += 1
                continue
            confidence = parse_confidence(item.get("confidence"))
            status = status_for_confidence(confidence, activation_confidence).value
            evidence_quote = str(item["evidence_quote"])
            stored_claim = self._graph.add_claim(
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
                    valid_from=grounded.valid_from,
                    valid_to=grounded.valid_to,
                    time_grain=grounded.time_grain,
                )
            )
            if stored_claim.status == FactStatus.active.value:
                self.active_claim_slots.add(
                    (
                        stored_claim.subject_type,
                        stored_claim.subject_id,
                        stored_claim.predicate,
                    )
                )
                eager_close_claim_slot(self._graph, stored_claim)
            summary["claims"] += 1
            summary[f"{status}_facts"] += 1

        # Glossary entities without an explicit definition claim still get one when
        # the extractor provided a description grounded in the same evidence quote.
        self._seed_glossary_definitions(
            doc=doc,
            seeded=glossary_seeded,
            parsed_claims=parsed.get("claims", []),
            summary=summary,
            activation_confidence=activation_confidence,
        )

    def _seed_glossary_definitions(
        self,
        *,
        doc: Document,
        seeded: list[tuple[str, str, str]],
        parsed_claims: list,
        summary: dict,
        activation_confidence: float,
    ) -> None:
        if not seeded:
            return
        claimed_subjects: set[str] = set()
        for item in parsed_claims:
            if str(item.get("predicate", "")).strip().lower() != "definition":
                continue
            sub = item.get("subject") or {}
            name = str(sub.get("name", "")).strip()
            if name:
                claimed_subjects.add(self._graph.normalize_name(name))
        for entity_id, description, quote in seeded:
            if not description or len(description) < 8:
                continue
            entity = self._graph.get_entity(entity_id)
            if entity is None:
                continue
            if self._graph.normalize_name(entity.name) in claimed_subjects:
                continue
            confidence = min(0.85, activation_confidence + 0.05)
            status = status_for_confidence(confidence, activation_confidence).value
            if doc.event_time is None:
                continue
            grounded = ground_fact_times({}, t_ref=doc.event_time)
            if grounded is None:
                continue
            stored_claim = self._graph.add_claim(
                Claim(
                    workspace_id=doc.workspace_id,
                    subject_type="glossary",
                    subject_id=entity_id,
                    predicate="definition",
                    object_text=description,
                    confidence=confidence,
                    status=status,
                    evidence_doc_ids=[doc.doc_id],
                    evidence_quotes=[{"doc_id": doc.doc_id, "quote": quote}],
                    origin_subject_id=entity_id,
                    created_by="extraction:glossary_seed",
                    decided_by=("automatic:confidence_gate" if status == "active" else ""),
                    valid_from=grounded.valid_from,
                    valid_to=grounded.valid_to,
                    time_grain=grounded.time_grain,
                )
            )
            if stored_claim.status == FactStatus.active.value:
                self.active_claim_slots.add(("glossary", entity_id, "definition"))
                eager_close_claim_slot(self._graph, stored_claim)
            summary["claims"] += 1
            summary[f"{status}_facts"] += 1

    def _resolve_ref(self, ref: dict, summary: dict) -> tuple[str, str] | None:
        """Map an extracted type and name to a graph node id."""
        ref_type = str(ref.get("type", "") or "").strip().lower()
        name = str(ref.get("name", "") or "").strip()
        if not name:
            return None
        registry = get_taxonomy_registry()

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

        # Prefer explicit ontology types (team/project/glossary). Legacy "entity"
        # remains accepted for older cached windows.
        entity_type: str | None
        if ref_type in {"", "entity"}:
            entity_type = None
        elif registry.is_known_entity_type(ref_type) and ref_type != "person":
            entity_type = ref_type
        else:
            summary["dropped_untyped"] += 1
            return None

        entity_matches = self._graph.search_entities(
            name, limit=5, entity_type=entity_type
        )
        exact_entities = [
            entity
            for entity in entity_matches
            if entity.normalized_name == self._graph.normalize_name(name)
            and (entity_type is None or entity.entity_type == entity_type)
        ]
        if len(exact_entities) != 1:
            summary["skipped_mentions"] += 1
            return None
        entity = exact_entities[0]
        return (entity.entity_type, entity.entity_id)


def normalize_evidence(value: str) -> str:
    return " ".join(value.split()).casefold()


def quote_is_supported(quote: object, source_window: str) -> bool:
    """Require non-trivial verbatim evidence after whitespace normalization."""
    if not isinstance(quote, str):
        return False
    normalized_quote = normalize_evidence(quote)
    if len(normalized_quote) < 8:
        return False
    return normalized_quote in normalize_evidence(source_window)


def parse_confidence(value: object) -> float:
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
