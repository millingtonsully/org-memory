"""Precedence and gating for taxonomy_proposals field-value write-backs (pure domain)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

_EPOCH = datetime.min.replace(tzinfo=UTC)

# Higher is better. Connector ground truth outranks agent promote and extraction.
PRECEDENCE_GROUND_TRUTH = 4
PRECEDENCE_AGENT_PROMOTE = 3
PRECEDENCE_EXTRACTION_MULTI = 2
PRECEDENCE_EXTRACTION_SINGLE = 1

PRECEDENCE_CLASS = {
    PRECEDENCE_GROUND_TRUTH: "ground_truth",
    PRECEDENCE_AGENT_PROMOTE: "agent_promote",
    PRECEDENCE_EXTRACTION_MULTI: "extraction_multi",
    PRECEDENCE_EXTRACTION_SINGLE: "extraction_single",
}


def precedence_rank(*, created_by: str, evidence_count: int) -> int:
    if created_by.startswith("structured_field"):
        return PRECEDENCE_GROUND_TRUTH
    if created_by.startswith("agent_promote"):
        return PRECEDENCE_AGENT_PROMOTE
    if evidence_count >= 2:
        return PRECEDENCE_EXTRACTION_MULTI
    return PRECEDENCE_EXTRACTION_SINGLE


def precedence_class_name(rank: int) -> str:
    return PRECEDENCE_CLASS.get(rank, "extraction_single")


@dataclass(frozen=True)
class ProposalCandidate:
    claim_id: str
    subject_type: str
    subject_id: str
    predicate: str
    value_text: str
    confidence: float
    evidence_doc_ids: list[str]
    created_by: str
    latest_evidence_at: datetime | None
    taxonomy_key: str
    field_key: str

    @property
    def rank(self) -> int:
        return precedence_rank(
            created_by=self.created_by,
            evidence_count=len(self.evidence_doc_ids),
        )

    @property
    def sort_key(self) -> tuple:
        return (
            self.rank,
            self.confidence,
            self.latest_evidence_at or _EPOCH,
            self.claim_id,
        )


def pick_slot_winners(candidates: list[ProposalCandidate]) -> list[ProposalCandidate]:
    """One winner per (subject, taxonomy_key, field_key) by precedence."""
    best: dict[tuple[str, str, str, str], ProposalCandidate] = {}
    for candidate in candidates:
        slot = (
            candidate.subject_type,
            candidate.subject_id,
            candidate.taxonomy_key,
            candidate.field_key,
        )
        current = best.get(slot)
        if current is None or candidate.sort_key > current.sort_key:
            best[slot] = candidate
    return list(best.values())
