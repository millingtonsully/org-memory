"""Person adjudication job: decide whether two person records are the same human.

Deterministic gates run first (hard source-id conflicts block, weak
corroboration abstains). Only pairs with structured corroboration reach the
LLM, and an automatic merge additionally requires the configured confidence
floor. Every decision is recorded with the exact evidence fingerprint it was
made on, so replays with unchanged evidence are free.
"""

from __future__ import annotations

import structlog
from sqlalchemy.orm import Session

from org_memory.core.errors import VendorAPIError
from org_memory.core.settings import get_settings
from org_memory.db.engine import session_scope
from org_memory.db.orm import Person, utcnow
from org_memory.db.repositories import (
    PersonMergeDecisionRepository,
    PersonRepository,
    SpendRepository,
)
from org_memory.services.identity_merge import (
    corroborating_signals,
    hard_identity_conflicts,
    has_sufficient_corroboration,
    identity_fingerprint,
    merge_people,
    reconcile_exclusive_slots_after_person_merge,
)
from org_memory.workers.handlers._shared import assert_spend_under_hard_limit, parse_llm_json

logger = structlog.get_logger(__name__)

_ADJUDICATION_SYSTEM_PROMPT = """You are an identity-resolution adjudicator.
Given two source-derived person records, decide if they are the SAME real
person, DIFFERENT people, or if the evidence is UNSURE.
Return ONLY this JSON object:
{"verdict": "same" | "different" | "unsure", "confidence": 0.0-1.0, "reason": "..."}
Do not treat a similar name alone as identity proof. Different source systems
use unrelated id namespaces. Explain the concrete evidence behind the verdict."""


def handle_adjudicate_persons(session: Session, payload: dict, synthesizer, heartbeat=None) -> None:
    person_ids = sorted({payload["person_a"], payload["person_b"]})
    if len(person_ids) != 2:
        return
    assert_spend_under_hard_limit()
    if heartbeat is not None:
        heartbeat()
    locked_people = (
        session.query(Person)
        .filter(
            Person.canonical_id.in_(person_ids),
            Person.workspace_id == get_settings().workspace_id,
        )
        .order_by(Person.canonical_id)
        .with_for_update()
        .all()
    )
    if len(locked_people) != 2:
        return
    by_id = {person.canonical_id: person for person in locked_people}
    person_a = by_id[payload["person_a"]]
    person_b = by_id[payload["person_b"]]
    if person_a.merged_into_id or person_b.merged_into_id:
        return

    persons = PersonRepository(session)
    decisions = PersonMergeDecisionRepository(session)
    aliases_a = persons.aliases_for(person_a.canonical_id)
    aliases_b = persons.aliases_for(person_b.canonical_id)
    fingerprint = identity_fingerprint(person_a, aliases_a, person_b, aliases_b)
    if decisions.find_by_fingerprint(fingerprint) is not None:
        logger.info(
            "worker.person_adjudication_cached",
            person_a=person_a.canonical_id,
            person_b=person_b.canonical_id,
        )
        return

    conflicts = hard_identity_conflicts(aliases_a, aliases_b)
    if conflicts:
        decisions.add(
            "person",
            person_a.canonical_id,
            person_b.canonical_id,
            verdict="different",
            confidence=1.0,
            reason="Deterministic source identifiers conflict.",
            status="blocked_conflict",
            signals=conflicts,
            evidence_fingerprint=fingerprint,
            decided_by="automatic:conflict_detector",
        )
        return

    signals = corroborating_signals(
        aliases_a,
        aliases_b,
        person_a,
        person_b,
        float(payload.get("candidate_similarity", 0.0)),
    )
    if not has_sufficient_corroboration(signals):
        decisions.add(
            "person",
            person_a.canonical_id,
            person_b.canonical_id,
            verdict="unsure",
            confidence=0.0,
            reason="Insufficient structured corroboration to justify an LLM call.",
            status="unsure",
            signals=signals,
            evidence_fingerprint=fingerprint,
            decided_by="automatic:corroboration_gate",
        )
        return

    def _describe(person: Person, aliases) -> str:
        alias_lines = "\n".join(
            f"  - source={alias.source_system!r}; external_id={alias.external_id!r}; "
            f"name={alias.display_name!r}; email={alias.email!r}; "
            f"email_verified={alias.email_verified}"
            for alias in aliases
        )
        return f"display_name={person.display_name!r} email={person.primary_email!r}\n{alias_lines}"

    raw, tokens = synthesizer.complete(
        _ADJUDICATION_SYSTEM_PROMPT,
        f"STRUCTURED CORROBORATION: {signals}\n\n"
        f"RECORD A:\n{_describe(person_a, aliases_a)}\n\n"
        f"RECORD B:\n{_describe(person_b, aliases_b)}",
    )
    with session_scope() as spend_session:
        SpendRepository(spend_session).record(
            "adjudication",
            "synthesis",
            synthesizer.model_name,
            tokens,
        )

    verdict = parse_llm_json("identity-adjudication", raw)
    verdict_kind, confidence, reason = _validated_verdict(verdict, raw)

    auto_merge = (
        verdict_kind == "same"
        and confidence >= get_settings().identity_merge_confidence
        and has_sufficient_corroboration(signals)
    )
    if auto_merge:
        keep, merge = sorted(
            [person_a, person_b],
            key=lambda person: (person.created_at, person.canonical_id),
        )
        merge_people(session, keep, merge)
        reconcile_exclusive_slots_after_person_merge(session, keep)
        status = "auto_merged"
    else:
        status = verdict_kind

    decisions.add(
        "person",
        person_a.canonical_id,
        person_b.canonical_id,
        verdict_kind,
        confidence,
        reason,
        status=status,
        signals=signals,
        evidence_fingerprint=fingerprint,
        decided_by=f"automatic:llm:{synthesizer.model_name}",
    )
    logger.info(
        "worker.person_adjudicated",
        person_a=person_a.canonical_id,
        person_b=person_b.canonical_id,
        verdict=verdict_kind,
        confidence=confidence,
        status=status,
        signals=signals,
    )
    person_a.updated_at = utcnow()
    person_b.updated_at = utcnow()


def _validated_verdict(verdict: dict, raw: str) -> tuple[str, float, str]:
    """Validate the adjudicator's JSON shape and value ranges, or raise."""
    verdict_kind = verdict.get("verdict")
    if verdict_kind not in {"same", "different", "unsure"}:
        raise VendorAPIError(
            "identity-adjudication",
            200,
            f"invalid verdict: {verdict_kind!r}",
            raw_response=raw,
        )
    confidence_value = verdict.get("confidence")
    if confidence_value is None or isinstance(confidence_value, bool):
        raise VendorAPIError(
            "identity-adjudication",
            200,
            "confidence must be a number between 0 and 1",
            raw_response=raw,
        )
    try:
        confidence = float(confidence_value)
    except (TypeError, ValueError) as exc:
        raise VendorAPIError(
            "identity-adjudication",
            200,
            "confidence must be a number between 0 and 1",
            raw_response=raw,
        ) from exc
    if not 0.0 <= confidence <= 1.0:
        raise VendorAPIError(
            "identity-adjudication",
            200,
            "confidence must be a number between 0 and 1",
            raw_response=raw,
        )
    reason = str(verdict.get("reason", "")).strip()
    if not reason:
        raise VendorAPIError(
            "identity-adjudication",
            200,
            "reason must be non-empty",
            raw_response=raw,
        )
    return verdict_kind, confidence, reason
