"""Background job handlers, one module per concern.

Each handler takes the job session plus the job payload and raises on failure
so the queue can retry or dead-letter. The worker loop in
``org_memory.workers.run`` maps each ``JobType`` to one of these functions.
"""

from org_memory.workers.handlers.collaboration import handle_aggregate_collaboration_edges
from org_memory.workers.handlers.conflicts import (
    handle_resolve_claim_conflict,
    handle_resolve_relationship_conflict,
)
from org_memory.workers.handlers.embedding import (
    handle_embed_chunks,
    handle_refresh_identity_embedding,
)
from org_memory.workers.handlers.graph_extraction import handle_extract_graph
from org_memory.workers.handlers.identity import handle_adjudicate_persons
from org_memory.workers.handlers.proposals import (
    handle_generate_taxonomy_proposals,
    handle_push_taxonomy_proposal_webhook,
)

__all__ = [
    "handle_adjudicate_persons",
    "handle_aggregate_collaboration_edges",
    "handle_embed_chunks",
    "handle_extract_graph",
    "handle_generate_taxonomy_proposals",
    "handle_push_taxonomy_proposal_webhook",
    "handle_refresh_identity_embedding",
    "handle_resolve_claim_conflict",
    "handle_resolve_relationship_conflict",
]
