"""Subject fact snapshot diff composed from query_subject_facts."""

from __future__ import annotations

from datetime import datetime

from org_memory.db.repositories import GraphRepository
from org_memory.domain.models import Principal
from org_memory.services.facts_query import query_subject_facts
from org_memory.services.temporality.diff import diff_fact_snapshots
from org_memory.taxonomy_registry import get_taxonomy_registry


def diff_subject_facts(
    graph: GraphRepository,
    *,
    subject_type: str,
    subject_id: str,
    principal: Principal,
    predicate: str | None = None,
    as_of_from: datetime | None = None,
    as_of_to: datetime | None = None,
    believed_as_of_from: datetime | None = None,
    believed_as_of_to: datetime | None = None,
    as_of_grain: str | None = None,
    limit: int = 50,
) -> dict:
    """Compare two temporal snapshots of the same subject.

    Provide either a world-time pair (``as_of_from`` / ``as_of_to``) or a
    belief-time pair (``believed_as_of_from`` / ``believed_as_of_to``). Mixing
    axes or omitting a bound raises ``ValueError``.
    """
    world = as_of_from is not None or as_of_to is not None
    belief = believed_as_of_from is not None or believed_as_of_to is not None
    if world and belief:
        raise ValueError(
            "Provide either as_of_from/as_of_to or "
            "believed_as_of_from/believed_as_of_to, not both."
        )
    if world:
        if as_of_from is None or as_of_to is None:
            raise ValueError("as_of_from and as_of_to are both required for world-time diff.")
        if as_of_from >= as_of_to:
            raise ValueError("as_of_from must be strictly before as_of_to.")
        axis = "world"
        snap_from = query_subject_facts(
            graph,
            subject_type=subject_type,
            subject_id=subject_id,
            principal=principal,
            predicate=predicate,
            as_of=as_of_from,
            as_of_grain=as_of_grain,
            limit=limit,
        )
        snap_to = query_subject_facts(
            graph,
            subject_type=subject_type,
            subject_id=subject_id,
            principal=principal,
            predicate=predicate,
            as_of=as_of_to,
            as_of_grain=as_of_grain,
            limit=limit,
        )
    elif belief:
        if believed_as_of_from is None or believed_as_of_to is None:
            raise ValueError(
                "believed_as_of_from and believed_as_of_to are both required "
                "for belief-time diff."
            )
        if believed_as_of_from >= believed_as_of_to:
            raise ValueError(
                "believed_as_of_from must be strictly before believed_as_of_to."
            )
        axis = "belief"
        snap_from = query_subject_facts(
            graph,
            subject_type=subject_type,
            subject_id=subject_id,
            principal=principal,
            predicate=predicate,
            believed_as_of=believed_as_of_from,
            as_of_grain=as_of_grain,
            limit=limit,
        )
        snap_to = query_subject_facts(
            graph,
            subject_type=subject_type,
            subject_id=subject_id,
            principal=principal,
            predicate=predicate,
            believed_as_of=believed_as_of_to,
            as_of_grain=as_of_grain,
            limit=limit,
        )
    else:
        raise ValueError(
            "World-time (as_of_from/as_of_to) or belief-time "
            "(believed_as_of_from/believed_as_of_to) pair is required."
        )

    registry = get_taxonomy_registry()
    exclusive = frozenset(
        key
        for key, pred in registry.predicates.items()
        if pred.mutually_exclusive
    )
    diff = diff_fact_snapshots(
        list(snap_from["facts"]),
        list(snap_to["facts"]),
        exclusive_predicates=exclusive,
    )
    return {
        "subject_type": snap_from["subject_type"],
        "subject_id": snap_from["subject_id"],
        "predicate": snap_from["predicate"],
        "axis": axis,
        "as_of_from": as_of_from.isoformat() if as_of_from else None,
        "as_of_to": as_of_to.isoformat() if as_of_to else None,
        "believed_as_of_from": (
            believed_as_of_from.isoformat() if believed_as_of_from else None
        ),
        "believed_as_of_to": (
            believed_as_of_to.isoformat() if believed_as_of_to else None
        ),
        "as_of_grain": as_of_grain,
        "from_snapshot": {
            "returned": snap_from["returned"],
            "truncated": snap_from["truncated"],
            "facts": snap_from["facts"],
        },
        "to_snapshot": {
            "returned": snap_to["returned"],
            "truncated": snap_to["truncated"],
            "facts": snap_to["facts"],
        },
        **diff,
    }
