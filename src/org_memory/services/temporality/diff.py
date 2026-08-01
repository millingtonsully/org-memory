"""Pure snapshot diff of shaped fact dicts between two temporal points."""

from __future__ import annotations

from typing import Any


def diff_fact_snapshots(
    facts_from: list[dict[str, Any]],
    facts_to: list[dict[str, Any]],
    *,
    exclusive_predicates: frozenset[str] | set[str] | None = None,
) -> dict[str, Any]:
    """Classify facts as unchanged, added, removed, or changed.

    Identity is ``fact_id``. For registry-exclusive predicates, a predicate that
    has different fact ids at the two snapshots is reported as ``changed``
    (from/to pair) rather than separate add+remove.
    """
    exclusive = frozenset(exclusive_predicates or ())
    by_id_from = {_fact_id(f): f for f in facts_from if _fact_id(f)}
    by_id_to = {_fact_id(f): f for f in facts_to if _fact_id(f)}

    common_ids = set(by_id_from) & set(by_id_to)
    only_from_ids = set(by_id_from) - set(by_id_to)
    only_to_ids = set(by_id_to) - set(by_id_from)

    unchanged = [by_id_from[i] for i in sorted(common_ids)]
    removed_ids = set(only_from_ids)
    added_ids = set(only_to_ids)
    changed: list[dict[str, Any]] = []

    from_by_pred: dict[str, list[str]] = {}
    to_by_pred: dict[str, list[str]] = {}
    for fid in only_from_ids:
        pred = str(by_id_from[fid].get("predicate") or "")
        if pred in exclusive:
            from_by_pred.setdefault(pred, []).append(fid)
    for fid in only_to_ids:
        pred = str(by_id_to[fid].get("predicate") or "")
        if pred in exclusive:
            to_by_pred.setdefault(pred, []).append(fid)

    for pred in sorted(set(from_by_pred) & set(to_by_pred)):
        from_ids = from_by_pred[pred]
        to_ids = to_by_pred[pred]
        # Pair one-to-one in stable fact_id order when both sides have values.
        pairs = min(len(from_ids), len(to_ids))
        from_ids_sorted = sorted(from_ids)
        to_ids_sorted = sorted(to_ids)
        for i in range(pairs):
            fid_from = from_ids_sorted[i]
            fid_to = to_ids_sorted[i]
            if by_id_from[fid_from].get("object") == by_id_to[fid_to].get("object"):
                continue
            changed.append(
                {
                    "predicate": pred,
                    "from": by_id_from[fid_from],
                    "to": by_id_to[fid_to],
                }
            )
            removed_ids.discard(fid_from)
            added_ids.discard(fid_to)

    removed = [by_id_from[i] for i in sorted(removed_ids)]
    added = [by_id_to[i] for i in sorted(added_ids)]
    return {
        "unchanged": unchanged,
        "added": added,
        "removed": removed,
        "changed": changed,
        "counts": {
            "unchanged": len(unchanged),
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
        },
    }


def _fact_id(fact: dict[str, Any]) -> str:
    return str(fact.get("fact_id") or "").strip()
