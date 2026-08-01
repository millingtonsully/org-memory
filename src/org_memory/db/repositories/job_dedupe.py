"""Open-job dedupe matchers for the Postgres job queue.

Returns SQL fragments and bind params so ``JobRepository`` keeps at most one
open job per logical key (document, person pair, conflict slot, …).
"""

from __future__ import annotations

from org_memory.domain.jobs import JobType


def open_duplicate_match(
    job_type: str, payload: dict, *, workspace_id: str
) -> tuple[str, dict] | None:
    """Return ``(match_sql, params)`` for an open duplicate, or None if none.

    ``params`` always includes ``ws`` (workspace_id). Callers add ``job_type``.
    """
    params: dict = {"ws": workspace_id}
    match_sql: str | None = None

    if job_type in (
        JobType.extract_graph.value,
        JobType.embed_chunks.value,
    ):
        doc_id = payload.get("doc_id")
        if not doc_id:
            return None
        match_sql = "payload->>'doc_id' = :doc_id"
        params["doc_id"] = doc_id
    elif job_type == JobType.adjudicate_persons.value:
        person_a = payload.get("person_a")
        person_b = payload.get("person_b")
        if not person_a or not person_b:
            return None
        # The pair is unordered: a<->b is the same decision as b<->a.
        low, high = sorted([person_a, person_b])
        match_sql = (
            "least(payload->>'person_a', payload->>'person_b') = :low "
            "AND greatest(payload->>'person_a', payload->>'person_b') = :high"
        )
        params.update({"low": low, "high": high})
    elif job_type == JobType.resolve_claim_conflict.value:
        subject_type = payload.get("subject_type")
        subject_id = payload.get("subject_id")
        predicate = payload.get("predicate")
        if not subject_type or not subject_id or not predicate:
            return None
        match_sql = (
            "payload->>'subject_type' = :subject_type "
            "AND payload->>'subject_id' = :subject_id "
            "AND payload->>'predicate' = :predicate"
        )
        params.update(
            {
                "subject_type": subject_type,
                "subject_id": subject_id,
                "predicate": predicate,
            }
        )
    elif job_type == JobType.resolve_relationship_conflict.value:
        from_type = payload.get("from_type")
        from_id = payload.get("from_id")
        relationship_type = payload.get("relationship_type")
        if not from_type or not from_id or not relationship_type:
            return None
        match_sql = (
            "payload->>'from_type' = :from_type "
            "AND payload->>'from_id' = :from_id "
            "AND payload->>'relationship_type' = :relationship_type"
        )
        params.update(
            {
                "from_type": from_type,
                "from_id": from_id,
                "relationship_type": relationship_type,
            }
        )
    elif job_type in (
        JobType.generate_taxonomy_proposals.value,
        JobType.aggregate_collaboration_edges.value,
        JobType.push_taxonomy_proposal_webhook.value,
    ):
        match_sql = "true"
    elif job_type == JobType.refresh_identity_embedding.value:
        person_id = payload.get("person_id")
        if not person_id:
            return None
        match_sql = "payload->>'person_id' = :person_id"
        params["person_id"] = person_id

    if match_sql is None:
        return None
    return match_sql, params
