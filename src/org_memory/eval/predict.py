"""Extract ranked prediction ids from a retrieve_context response payload."""

from __future__ import annotations

from typing import Any

from org_memory.eval.harness import CasePrediction


def predictions_from_retrieve_payload(payload: dict[str, Any]) -> CasePrediction:
    doc_ids: list[str] = []
    seen_docs: set[str] = set()
    for passage in payload.get("passages") or []:
        doc_id = str(passage.get("doc_id") or "").strip()
        if doc_id and doc_id not in seen_docs:
            seen_docs.add(doc_id)
            doc_ids.append(doc_id)

    claim_ids: list[str] = []
    seen_claims: set[str] = set()

    def _add_claim(fact_id: object) -> None:
        cid = str(fact_id or "").strip()
        if cid and cid not in seen_claims:
            seen_claims.add(cid)
            claim_ids.append(cid)

    for fact in payload.get("search_facts") or []:
        _add_claim(fact.get("fact_id"))
    for block in payload.get("structured_facts") or []:
        for fact in block.get("facts") or []:
            _add_claim(fact.get("fact_id"))

    return CasePrediction(doc_ids=tuple(doc_ids), claim_ids=tuple(claim_ids))
