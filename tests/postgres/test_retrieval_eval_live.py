"""Postgres live retrieval eval: seed gold corpus and score retrieve_context."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.postgres


def test_run_live_eval_seeds_and_scores(hermetic_workspace, tmp_path: Path, monkeypatch) -> None:
    """End-to-end grader against a real DB with the fixture embedder."""
    monkeypatch.setenv("DATABASE_URL", __import__("os").environ["DATABASE_URL"])
    from org_memory.eval.run_live import main

    out = tmp_path / "preds.json"
    code = main(
        [
            "--workspace-id",
            hermetic_workspace,
            "--predictions-out",
            str(out),
        ]
    )
    assert code == 0
    preds = json.loads(out.read_text(encoding="utf-8"))
    assert "glossary-definition" in preds
    assert preds["glossary-definition"]["doc_ids"]
    assert "claim:carepod-definition" in preds["glossary-definition"]["claim_ids"]
