"""Rule gates and reversible writes for automatic person resolution."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy.orm import Session

from org_memory.db.orm import (
    Claim,
    DocumentParticipant,
    Person,
    PersonAlias,
    PersonMergeDecision,
    Relationship,
    utcnow,
)
from org_memory.db.repositories import PersonMergeDecisionRepository, PersonRepository


def _normalized_name(value: str) -> str:
    return " ".join(value.casefold().split())


def _alias_payload(aliases: list[PersonAlias]) -> list[dict]:
    return sorted(
        (
            {
                "source": alias.source_system,
                "external_id": alias.external_id,
                "name": _normalized_name(alias.display_name),
                "email": alias.email.casefold(),
                "email_verified": alias.email_verified,
                "observed_person_id": alias.observed_person_id,
            }
            for alias in aliases
        ),
        key=lambda item: json.dumps(item, sort_keys=True),
    )


def identity_fingerprint(
    person_a: Person,
    aliases_a: list[PersonAlias],
    person_b: Person,
    aliases_b: list[PersonAlias],
) -> str:
    """Hash exactly the identity evidence sent to adjudication."""
    people_payload: list[dict] = [
        {
            "id": person_a.canonical_id,
            "name": person_a.display_name,
            "aliases": _alias_payload(aliases_a),
        },
        {
            "id": person_b.canonical_id,
            "name": person_b.display_name,
            "aliases": _alias_payload(aliases_b),
        },
    ]
    payload = {"people": sorted(people_payload, key=lambda item: str(item["id"]))}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def hard_identity_conflicts(aliases_a: list[PersonAlias], aliases_b: list[PersonAlias]) -> list[str]:
    """Return verified/source-key contradictions that forbid a merge."""

    def source_ids(aliases: list[PersonAlias]) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for alias in aliases:
            if alias.source_system and alias.external_id:
                result.setdefault(alias.source_system, set()).add(alias.external_id)
        return result

    ids_a = source_ids(aliases_a)
    ids_b = source_ids(aliases_b)
    conflicts: list[str] = []
    for source in sorted(set(ids_a) & set(ids_b)):
        if ids_a[source].isdisjoint(ids_b[source]):
            conflicts.append(f"conflicting_source_id:{source}")
    return conflicts


def corroborating_signals(
    aliases_a: list[PersonAlias],
    aliases_b: list[PersonAlias],
    person_a: Person,
    person_b: Person,
    similarity: float,
) -> list[str]:
    """Independent-enough structured signals required alongside the LLM."""
    signals: list[str] = []
    names_a = {
        _normalized_name(value)
        for value in [person_a.display_name, *(a.display_name for a in aliases_a)]
        if _normalized_name(value)
    }
    names_b = {
        _normalized_name(value)
        for value in [person_b.display_name, *(a.display_name for a in aliases_b)]
        if _normalized_name(value)
    }
    if names_a & names_b:
        signals.append("shared_normalized_name")

    domains_a = {
        alias.email.rsplit("@", 1)[1].casefold()
        for alias in aliases_a
        if "@" in alias.email and alias.email_verified
    }
    domains_b = {
        alias.email.rsplit("@", 1)[1].casefold()
        for alias in aliases_b
        if "@" in alias.email and alias.email_verified
    }
    if domains_a & domains_b:
        signals.append("shared_verified_email_domain")

    emails_a = {
        normalize_email(alias.email)
        for alias in aliases_a
        if alias.email and alias.email_verified
    }
    emails_b = {
        normalize_email(alias.email)
        for alias in aliases_b
        if alias.email and alias.email_verified
    }
    if emails_a & emails_b:
        signals.append("shared_email_address")

    if similarity >= 0.95:
        signals.append("very_high_identity_similarity")
    return signals


def has_sufficient_corroboration(signals: list[str]) -> bool:
    """Require a shared name plus a shared email address."""
    observed = set(signals)
    return "shared_normalized_name" in observed and "shared_email_address" in observed


def normalize_email(value: str) -> str:
    cleaned = value.strip().casefold()
    if "@" not in cleaned:
        return cleaned
    local, _, domain = cleaned.partition("@")
    if "+" in local:
        local = local.split("+", 1)[0]
    if domain in {"gmail.com", "googlemail.com"}:
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def refresh_identity_metadata(session: Session, person: Person) -> None:
    """Store only deterministic metadata derived from source aliases."""
    aliases = (
        session.query(PersonAlias)
        .filter(
            PersonAlias.workspace_id == person.workspace_id,
            PersonAlias.person_id == person.canonical_id,
        )
        .all()
    )
    person.identity_metadata = {
        "sources": sorted({alias.source_system for alias in aliases}),
        "alias_count": len(aliases),
        "verified_email_count": sum(alias.email_verified for alias in aliases),
        "verified_identifier_namespaces": sorted(
            {
                alias.source_system.removeprefix("identity:")
                for alias in aliases
                if alias.source_system.startswith("identity:") and alias.external_id
            }
        ),
    }


def merge_people(session: Session, keep: Person, merge: Person) -> None:
    """Apply a reversible merge while preserving immutable origin ids."""
    if keep.workspace_id != merge.workspace_id:
        raise ValueError("Cannot merge people from different workspaces")
    if keep.canonical_id == merge.canonical_id:
        raise ValueError("Cannot merge a person into itself")
    if keep.merged_into_id or merge.merged_into_id:
        raise ValueError("Person merge candidates must both be canonical roots")

    session.query(PersonAlias).filter(
        PersonAlias.workspace_id == keep.workspace_id,
        PersonAlias.person_id == merge.canonical_id,
    ).update({PersonAlias.person_id: keep.canonical_id}, synchronize_session=False)
    session.query(DocumentParticipant).filter(
        DocumentParticipant.workspace_id == keep.workspace_id,
        DocumentParticipant.person_id == merge.canonical_id,
    ).update({DocumentParticipant.person_id: keep.canonical_id}, synchronize_session=False)
    session.query(Relationship).filter(
        Relationship.workspace_id == keep.workspace_id,
        Relationship.from_type == "person",
        Relationship.from_id == merge.canonical_id,
    ).update({Relationship.from_id: keep.canonical_id}, synchronize_session=False)
    session.query(Relationship).filter(
        Relationship.workspace_id == keep.workspace_id,
        Relationship.to_type == "person",
        Relationship.to_id == merge.canonical_id,
    ).update({Relationship.to_id: keep.canonical_id}, synchronize_session=False)
    session.query(Claim).filter(
        Claim.workspace_id == keep.workspace_id,
        Claim.subject_type == "person",
        Claim.subject_id == merge.canonical_id,
    ).update({Claim.subject_id: keep.canonical_id}, synchronize_session=False)

    names = set(keep.name_aliases or []) | set(merge.name_aliases or [])
    names.update([keep.display_name, merge.display_name])
    keep.name_aliases = sorted(name for name in names if name)
    if not keep.primary_email:
        keep.primary_email = merge.primary_email
    keep.resolution_status = "canonical"
    keep.updated_at = utcnow()
    merge.merged_into_id = keep.canonical_id
    merge.resolution_status = "merged"
    merge.updated_at = utcnow()
    refresh_identity_metadata(session, keep)
    refresh_identity_metadata(session, merge)


def split_person(session: Session, root: Person, child: Person, reason: str) -> None:
    """Restore rows to their immutable observed owner after a contradiction."""
    session.query(PersonAlias).filter(
        PersonAlias.workspace_id == root.workspace_id,
        PersonAlias.person_id == root.canonical_id,
        PersonAlias.observed_person_id == child.canonical_id,
    ).update({PersonAlias.person_id: child.canonical_id}, synchronize_session=False)
    session.query(DocumentParticipant).filter(
        DocumentParticipant.workspace_id == root.workspace_id,
        DocumentParticipant.person_id == root.canonical_id,
        DocumentParticipant.observed_person_id == child.canonical_id,
    ).update({DocumentParticipant.person_id: child.canonical_id}, synchronize_session=False)
    session.query(Relationship).filter(
        Relationship.workspace_id == root.workspace_id,
        Relationship.from_type == "person",
        Relationship.from_id == root.canonical_id,
        Relationship.origin_from_id == child.canonical_id,
    ).update({Relationship.from_id: child.canonical_id}, synchronize_session=False)
    session.query(Relationship).filter(
        Relationship.workspace_id == root.workspace_id,
        Relationship.to_type == "person",
        Relationship.to_id == root.canonical_id,
        Relationship.origin_to_id == child.canonical_id,
    ).update({Relationship.to_id: child.canonical_id}, synchronize_session=False)
    session.query(Claim).filter(
        Claim.workspace_id == root.workspace_id,
        Claim.subject_type == "person",
        Claim.subject_id == root.canonical_id,
        Claim.origin_subject_id == child.canonical_id,
    ).update({Claim.subject_id: child.canonical_id}, synchronize_session=False)

    child.merged_into_id = None
    child.resolution_status = "provisional"
    child.updated_at = utcnow()
    root.updated_at = utcnow()
    refresh_identity_metadata(session, root)
    refresh_identity_metadata(session, child)

    decision = (
        session.query(PersonMergeDecision)
        .filter(
            PersonMergeDecision.workspace_id == root.workspace_id,
            PersonMergeDecision.status == "auto_merged",
            PersonMergeDecision.a_id.in_([root.canonical_id, child.canonical_id]),
            PersonMergeDecision.b_id.in_([root.canonical_id, child.canonical_id]),
        )
        .order_by(PersonMergeDecision.created_at.desc())
        .first()
    )
    if decision is not None:
        decision.status = "split_conflict"
        decision.reversed_at = utcnow()
        decision.reversal_reason = reason


def reconcile_merged_identity_conflicts(session: Session, root: Person) -> None:
    """Automatically split merged components when a new hard key conflicts."""
    people = PersonRepository(session)
    root_aliases = people.aliases_observed_for(root.canonical_id)
    for child in people.merged_children(root.canonical_id):
        child_aliases = people.aliases_observed_for(child.canonical_id)
        conflicts = hard_identity_conflicts(root_aliases, child_aliases)
        if conflicts:
            split_person(session, root, child, ";".join(conflicts))
            fingerprint = identity_fingerprint(root, root_aliases, child, child_aliases)
            PersonMergeDecisionRepository(session).add(
                "person",
                root.canonical_id,
                child.canonical_id,
                verdict="different",
                confidence=1.0,
                reason="New deterministic identity conflict triggered automatic split.",
                status="split_conflict",
                signals=conflicts,
                evidence_fingerprint=fingerprint,
                decided_by="automatic:conflict_detector",
            )
