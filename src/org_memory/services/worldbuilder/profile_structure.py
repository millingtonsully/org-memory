"""Pure functions that shape a Worldbuilder profile.

Everything here operates on plain data: model output text, graph rows already
filtered to the viewer, and retrieval passages. No database access happens in
this module, which keeps the grounding rules directly unit-testable.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from org_memory.domain.models import Passage

WorldbuilderCategory = Literal["person", "team", "project", "glossary"]
CATEGORIES: tuple[WorldbuilderCategory, ...] = ("person", "team", "project", "glossary")

_PROFILE_JSON_SCHEMA_HINT = """
Return ONLY valid JSON (no markdown) with this shape:
{
  "subject_descriptions": [
    {"text": str, "confidence": number 0-1, "evidence_doc_ids": [str], "source_record_ids": [str]}
  ],
  "org_work_context": [
    {"text": str, "confidence": number, "evidence_doc_ids": [str], "source_record_ids": [str]}
  ],
  "vocabulary": [{"term": str, "note": str, "evidence_doc_ids": [str]}],
  "caveats": [str],
  "team_signals": [{"text": str, "confidence": number, "evidence_doc_ids": [str]}],
  "profile_prose": str
}
Rules:
- Use ONLY GRAPH FACTS and EVIDENCE. Never invent.
- evidence_doc_ids must be doc_ids from the evidence/graph sections.
- source_record_ids may be claim_id or relationship_id values from GRAPH FACTS when used.
- Omit empty arrays. Put uncertainty in caveats.
- profile_prose is a short readable summary of the structured fields only.
""".strip()


def category_system_prompt(category: WorldbuilderCategory) -> str:
    focus = {
        "person": "Role & team, current projects, collaborators, recent activity.",
        "team": "Purpose, members/signals, owned work, recent activity.",
        "project": "Goal, status signals, participants, risks/blockers from evidence.",
        "glossary": "Org-specific definition of the term, usage context, related teams/projects.",
    }[category]
    return (
        f"You are Worldbuilder. Synthesize a structured {category} profile.\n"
        f"Focus: {focus}\n"
        f"{_PROFILE_JSON_SCHEMA_HINT}"
    )


def build_profile_prompt(
    category: str,
    name: str,
    graph_block: str,
    passages: list[Passage],
    query: str,
) -> str:
    evidence_block = (
        "\n\n".join(
            f"[doc_id={p.doc_id}] ({p.source_type}, {p.event_time.date().isoformat()}, "
            f"by {p.author_display_name})\n{p.text}"
            for p in passages
        )
        or "(no evidence visible to this viewer)"
    )
    return (
        f"CATEGORY: {category}\n"
        f"SUBJECT: {name}\n"
        f"FOCUS_QUERY: {query or name}\n\n"
        f"GRAPH FACTS (active automatic relationships and claims):\n{graph_block}\n\n"
        f"EVIDENCE:\n{evidence_block}"
    )


def graph_record_ids(relationships, claims) -> set[str]:
    ids: set[str] = set()
    for r, _ in relationships:
        ids.add(r.relationship_id)
    for c, _ in claims:
        ids.add(c.claim_id)
    return ids


def parse_structured_profile(raw: str) -> tuple[dict[str, Any], bool]:
    """Parse model JSON. Returns (structured, model_json_ok).

    On parse failure, returns prose-only scaffolding with empty arrays and
    model_json_ok=False. Callers must run ensure_structured_from_graph so
    agents never see empty structured fields when graph facts exist.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed, True
    except json.JSONDecodeError:
        pass
    return {
        "subject_descriptions": [],
        "org_work_context": [],
        "vocabulary": [],
        "caveats": [],
        "team_signals": [],
        "profile_prose": raw.strip(),
    }, False


def structured_buckets_nonempty(structured: dict[str, Any]) -> bool:
    for key in ("subject_descriptions", "org_work_context", "vocabulary", "team_signals"):
        items = structured.get(key)
        if isinstance(items, list) and items:
            return True
    return False


def ensure_structured_from_graph(
    structured: dict[str, Any],
    *,
    claims,
    relationships,
    model_json_ok: bool,
    display_name: str = "",
) -> str:
    """Fill empty structured buckets from ACL'd graph facts. Never invents text.

    Returns profile_structure_source: model | graph | model_and_graph | prose_only.
    """
    had_model_fields = model_json_ok and structured_buckets_nonempty(structured)
    seeded = False

    if not structured.get("subject_descriptions"):
        descriptions = []
        for claim, visible_doc_ids in claims:
            text = f"{claim.predicate}: {claim.object_text}".strip()
            if not text:
                continue
            descriptions.append(
                {
                    "text": text,
                    "confidence": float(claim.confidence or 0.0),
                    "evidence_doc_ids": list(visible_doc_ids),
                    "source_record_ids": [claim.claim_id],
                }
            )
        if descriptions:
            structured["subject_descriptions"] = descriptions
            seeded = True

    if not structured.get("vocabulary"):
        vocab = []
        for claim, visible_doc_ids in claims:
            if str(claim.predicate).strip().lower() != "definition":
                continue
            note = str(claim.object_text or "").strip()
            if not note:
                continue
            vocab.append(
                {
                    "term": (display_name or "definition").strip(),
                    "note": note,
                    "evidence_doc_ids": list(visible_doc_ids),
                }
            )
        if vocab:
            structured["vocabulary"] = vocab
            seeded = True

    if not structured.get("team_signals"):
        signals = []
        for rel, visible_doc_ids in relationships:
            text = (
                f"{rel.from_type}:{rel.from_id} {rel.relationship_type} "
                f"{rel.to_type}:{rel.to_id}"
            )
            signals.append(
                {
                    "text": text,
                    "confidence": float(rel.confidence or 0.0),
                    "evidence_doc_ids": list(visible_doc_ids),
                    "source_record_ids": [rel.relationship_id],
                }
            )
        if signals:
            structured["team_signals"] = signals
            seeded = True

    if not str(structured.get("profile_prose") or "").strip():
        bits = [
            item["text"]
            for item in (structured.get("subject_descriptions") or [])
            if isinstance(item, dict) and item.get("text")
        ]
        if bits:
            structured["profile_prose"] = "; ".join(bits[:8])
            seeded = True

    caveats = structured.setdefault("caveats", [])
    if not isinstance(caveats, list):
        structured["caveats"] = []
        caveats = structured["caveats"]

    if not model_json_ok:
        msg = (
            "Model did not return valid structured JSON; "
            "structured fields were filled from graph facts when available."
            if seeded
            else (
                "Model did not return valid structured JSON and no graph facts "
                "were available; profile_prose contains raw synthesis only."
            )
        )
        if msg not in caveats:
            caveats.append(msg)
    elif seeded and not had_model_fields:
        msg = (
            "Model returned no usable structured fields; "
            "structured fields were filled from graph facts."
        )
        if msg not in caveats:
            caveats.append(msg)
    elif seeded and had_model_fields:
        msg = "Empty structured buckets were filled from graph facts."
        if msg not in caveats:
            caveats.append(msg)

    if had_model_fields and seeded:
        return "model_and_graph"
    if had_model_fields:
        return "model"
    if seeded or structured_buckets_nonempty(structured):
        return "graph"
    return "prose_only"


def ground_structured_profile(
    structured: dict[str, Any],
    *,
    allowed_doc_ids: set[str],
    allowed_record_ids: set[str],
) -> dict[str, Any]:
    """Drop citations the viewer cannot see and normalize field shapes.

    Runs on both fresh model output and cache hits, so a cached profile can
    never revive ids the viewer has since lost access to.
    """

    def _filter_docs(ids: Any) -> list[str]:
        if not isinstance(ids, list):
            return []
        return [str(i) for i in ids if str(i) in allowed_doc_ids]

    def _filter_records(ids: Any) -> list[str]:
        if not isinstance(ids, list):
            return []
        return [str(i) for i in ids if str(i) in allowed_record_ids]

    for key in ("subject_descriptions", "org_work_context", "team_signals"):
        items = structured.get(key)
        if not isinstance(items, list):
            structured[key] = []
            continue
        cleaned = []
        for item in items:
            if not isinstance(item, dict) or not str(item.get("text") or "").strip():
                continue
            cleaned.append(
                {
                    "text": str(item["text"]).strip(),
                    "confidence": float(item.get("confidence") or 0.0),
                    "evidence_doc_ids": _filter_docs(item.get("evidence_doc_ids")),
                    "source_record_ids": _filter_records(item.get("source_record_ids")),
                }
            )
        structured[key] = cleaned

    vocab = structured.get("vocabulary")
    if not isinstance(vocab, list):
        structured["vocabulary"] = []
    else:
        structured["vocabulary"] = [
            {
                "term": str(item.get("term") or "").strip(),
                "note": str(item.get("note") or "").strip(),
                "evidence_doc_ids": _filter_docs(item.get("evidence_doc_ids")),
            }
            for item in vocab
            if isinstance(item, dict) and str(item.get("term") or "").strip()
        ]

    caveats = structured.get("caveats")
    if not isinstance(caveats, list):
        structured["caveats"] = []
    else:
        structured["caveats"] = [str(c).strip() for c in caveats if str(c).strip()]

    prose = structured.get("profile_prose") or structured.get("profile") or ""
    structured["profile_prose"] = str(prose).strip()
    return structured


def add_staleness_caveats(
    structured: dict[str, Any],
    graph_claims: list[dict],
    evidence: list[Passage],
) -> None:
    """Flag claims that are much older than the newest supporting passage."""
    if not graph_claims or not evidence:
        return
    newest_evidence = max((p.event_time for p in evidence if p.event_time), default=None)
    if newest_evidence is None:
        return
    stale = []
    for claim in graph_claims:
        valid_from = claim.get("valid_from")
        if not valid_from:
            continue
        try:
            claim_time = datetime.fromisoformat(valid_from)
        except ValueError:
            continue
        if claim_time.tzinfo is None:
            claim_time = claim_time.replace(tzinfo=UTC)
        age_days = (newest_evidence - claim_time).total_seconds() / 86400.0
        if age_days > 180:
            stale.append(
                f"Graph claim '{claim['predicate']}' may be stale relative to newer evidence "
                f"({int(age_days)} days older than newest passage)."
            )
    if stale:
        structured.setdefault("caveats", [])
        structured["caveats"].extend(stale)
