"""Worldbuilder structured profile parsing, grounding, and graph seeding."""

from __future__ import annotations

from types import SimpleNamespace

from org_memory.services.worldbuilder import (
    _ensure_structured_from_graph,
    _ground_structured_profile,
    _parse_structured_profile,
)


def test_parse_structured_profile_json() -> None:
    raw = """
    {
      "subject_descriptions": [{"text": "Eng", "confidence": 0.9, "evidence_doc_ids": ["d1"]}],
      "org_work_context": [],
      "vocabulary": [],
      "caveats": [],
      "team_signals": [],
      "profile_prose": "Eng on Payments"
    }
    """
    parsed, ok = _parse_structured_profile(raw)
    assert ok is True
    assert parsed["profile_prose"] == "Eng on Payments"
    assert parsed["subject_descriptions"][0]["text"] == "Eng"


def test_parse_structured_profile_invalid_is_prose_only_scaffold() -> None:
    parsed, ok = _parse_structured_profile("not json at all")
    assert ok is False
    assert parsed["profile_prose"] == "not json at all"
    assert parsed["subject_descriptions"] == []


def test_ground_structured_profile_filters_unknown_ids() -> None:
    structured = {
        "subject_descriptions": [
            {
                "text": "Leads billing",
                "confidence": 0.8,
                "evidence_doc_ids": ["doc:ok", "doc:nope"],
                "source_record_ids": ["claim:ok", "claim:nope"],
            }
        ],
        "org_work_context": [],
        "vocabulary": [{"term": "CarePod", "note": "unit", "evidence_doc_ids": ["doc:ok"]}],
        "caveats": ["uncertain title"],
        "team_signals": [],
        "profile_prose": "summary",
    }
    grounded = _ground_structured_profile(
        structured,
        allowed_doc_ids={"doc:ok"},
        allowed_record_ids={"claim:ok"},
    )
    assert grounded["subject_descriptions"][0]["evidence_doc_ids"] == ["doc:ok"]
    assert grounded["subject_descriptions"][0]["source_record_ids"] == ["claim:ok"]
    assert grounded["vocabulary"][0]["evidence_doc_ids"] == ["doc:ok"]


def test_invalid_json_seeds_structured_fields_from_graph() -> None:
    structured, ok = _parse_structured_profile("raw prose failure")
    assert ok is False
    claim = SimpleNamespace(
        claim_id="claim:1",
        predicate="title",
        object_text="Staff Engineer",
        confidence=0.9,
    )
    rel = SimpleNamespace(
        relationship_id="rel:1",
        from_type="person",
        from_id="p1",
        to_type="team",
        to_id="t1",
        relationship_type="member_of",
        confidence=0.8,
    )
    source = _ensure_structured_from_graph(
        structured,
        claims=[(claim, ["doc:1"])],
        relationships=[(rel, ["doc:1"])],
        model_json_ok=False,
        display_name="Ada",
    )
    assert source == "graph"
    assert structured["subject_descriptions"][0]["text"] == "title: Staff Engineer"
    assert structured["subject_descriptions"][0]["source_record_ids"] == ["claim:1"]
    assert structured["team_signals"][0]["source_record_ids"] == ["rel:1"]
    # Raw model text is preserved; structured fields come from graph.
    assert structured["profile_prose"] == "raw prose failure"
    assert any("graph facts" in c for c in structured["caveats"])


def test_definition_claim_seeds_vocabulary_with_display_name() -> None:
    structured = {
        "subject_descriptions": [],
        "org_work_context": [],
        "vocabulary": [],
        "caveats": [],
        "team_signals": [],
        "profile_prose": "",
    }
    claim = SimpleNamespace(
        claim_id="claim:def",
        predicate="definition",
        object_text="Internal care unit roster",
        confidence=1.0,
    )
    source = _ensure_structured_from_graph(
        structured,
        claims=[(claim, ["doc:g"])],
        relationships=[],
        model_json_ok=True,
        display_name="CarePod",
    )
    assert source == "graph"
    assert structured["vocabulary"][0]["term"] == "CarePod"
    assert structured["vocabulary"][0]["note"] == "Internal care unit roster"


def test_model_fields_preserved_when_seeding_empty_buckets() -> None:
    structured = {
        "subject_descriptions": [
            {
                "text": "From model",
                "confidence": 0.7,
                "evidence_doc_ids": ["doc:1"],
                "source_record_ids": [],
            }
        ],
        "org_work_context": [],
        "vocabulary": [],
        "caveats": [],
        "team_signals": [],
        "profile_prose": "model prose",
    }
    claim = SimpleNamespace(
        claim_id="claim:1",
        predicate="title",
        object_text="Ignored because model already filled subject_descriptions",
        confidence=1.0,
    )
    rel = SimpleNamespace(
        relationship_id="rel:1",
        from_type="person",
        from_id="p1",
        to_type="team",
        to_id="t1",
        relationship_type="member_of",
        confidence=0.8,
    )
    source = _ensure_structured_from_graph(
        structured,
        claims=[(claim, ["doc:1"])],
        relationships=[(rel, ["doc:1"])],
        model_json_ok=True,
    )
    assert source == "model_and_graph"
    assert structured["subject_descriptions"][0]["text"] == "From model"
    assert structured["team_signals"][0]["source_record_ids"] == ["rel:1"]
