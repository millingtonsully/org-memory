"""Background job types.

Single source of truth for the queue's job type strings. Producers (ingest,
extraction) and the worker registry both reference this enum, so a typo can't
enqueue a job no handler will ever run.
"""

from __future__ import annotations

from enum import Enum


class JobType(str, Enum):
    embed_chunks = "embed_chunks"
    extract_graph = "extract_graph"
    adjudicate_persons = "adjudicate_persons"
    resolve_claim_conflict = "resolve_claim_conflict"
    resolve_relationship_conflict = "resolve_relationship_conflict"
    generate_taxonomy_proposals = "generate_taxonomy_proposals"
    aggregate_collaboration_edges = "aggregate_collaboration_edges"
    push_taxonomy_proposal_webhook = "push_taxonomy_proposal_webhook"
    refresh_identity_embedding = "refresh_identity_embedding"
