"""Live retrieval eval: seed gold corpus, run retrieve_context, score.

Requires DATABASE_URL. Uses an evaluation-only fixture embedder (planted
vectors) so the run does not call a vendor embedding API. Writes predictions
JSON and prints the same report shape as ``score_retrieval``.

Example::

    python -m org_memory.eval.run_live
    python -m org_memory.eval.run_live --predictions-out /tmp/preds.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

from org_memory.eval.fixture_embedder import EvalFixtureEmbedder
from org_memory.eval.harness import (
    CasePrediction,
    default_gold_path,
    load_gold_set,
    score_predictions,
)
from org_memory.eval.predict import predictions_from_retrieve_payload
from org_memory.eval.seed_corpus import seed_gold_corpus


def _configure_eval_env(workspace_id: str) -> None:
    """Ensure Settings can boot for an isolated eval workspace."""
    os.environ["WORKSPACE_ID"] = workspace_id
    os.environ.setdefault("SERVICE_API_KEY", "eval-service-key")
    os.environ.setdefault("EMBEDDING_API_KEY", "eval-unused-embed-key")
    os.environ.setdefault("RERANK_API_KEY", "eval-unused-rerank-key")
    os.environ.setdefault("OBJECT_STORE_BACKEND", "supabase")
    os.environ.setdefault("SUPABASE_PROJECT_URL", "https://eval.invalid")
    os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "eval-unused")
    os.environ.setdefault("RETENTION_DAYS", "30")
    os.environ.setdefault("SPEND_ALERT_TOKENS_MONTHLY", "1000000")
    os.environ.setdefault("SPEND_HARD_LIMIT_TOKENS_MONTHLY", "2000000")


class _UnusedReranker:
    """Rerank should be skipped when the shortlist fits under limit."""

    model_name = "eval-unused-reranker"

    def rerank(self, query: str, documents: list[str]) -> tuple[list[float], int]:
        raise RuntimeError(
            "eval run expected rerank to be skipped (shortlist within limit)"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=None)
    parser.add_argument(
        "--predictions-out",
        type=Path,
        default=None,
        help="Optional path to write the generated predictions JSON",
    )
    parser.add_argument(
        "--workspace-id",
        default=None,
        help="Workspace id to seed (default: eval-<random>)",
    )
    args = parser.parse_args(argv)

    if not os.environ.get("DATABASE_URL"):
        raise SystemExit(
            "DATABASE_URL is required for live retrieval eval "
            "(seeds a hermetic workspace and runs retrieve_context)."
        )

    workspace_id = args.workspace_id or f"eval-{uuid.uuid4().hex[:12]}"
    _configure_eval_env(workspace_id)

    from org_memory.core.settings import get_settings
    from org_memory.db import engine as engine_mod
    from org_memory.db.engine import session_scope
    from org_memory.db.repositories import (
        AuditRepository,
        ChunkSearchRepository,
        GraphRepository,
        PersonRepository,
    )
    from org_memory.domain.models import Principal
    from org_memory.services.retrieval import RetrievalService
    from org_memory.services.retrieve_context import RetrieveContextService, SubjectRef

    get_settings.cache_clear()
    engine_mod._engine = None
    engine_mod._session_factory = None

    cases = load_gold_set(args.gold)
    embedder = EvalFixtureEmbedder()
    principal = Principal(
        principal_id="user:eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        groups=[],
    )

    serializable: dict[str, dict[str, list[str]]] = {}
    with session_scope() as session:
        seed_gold_corpus(
            session, workspace_id=workspace_id, cases=cases, embedder=embedder
        )
        retrieval = RetrievalService(
            search_repo=ChunkSearchRepository(session),
            audit_repo=AuditRepository(session),
            embedder=embedder,
            reranker=_UnusedReranker(),
            graph_repo=GraphRepository(session),
            person_repo=PersonRepository(session),
        )
        service = RetrieveContextService(
            session=session,
            retrieval=retrieval,
            graph=GraphRepository(session),
        )

        for case in cases:
            as_of = datetime.fromisoformat(case.as_of) if case.as_of else None
            believed_as_of = (
                datetime.fromisoformat(case.believed_as_of)
                if case.believed_as_of
                else None
            )
            subjects = [SubjectRef(type=t, id=i) for t, i in case.subjects]
            payload = service.retrieve(
                principal=principal,
                query=case.query,
                mode=case.mode,  # type: ignore[arg-type]
                limit=max(case.k, 10),
                subjects=subjects,
                about=case.about,
                as_of=as_of,
                believed_as_of=believed_as_of,
                as_of_grain=case.as_of_grain,
            )
            if payload.get("status") == "ambiguous":
                raise SystemExit(
                    f"case {case.case_id!r} resolved ambiguously: {payload.get('detail')}"
                )
            pred = predictions_from_retrieve_payload(payload)
            serializable[case.case_id] = {
                "doc_ids": list(pred.doc_ids),
                "claim_ids": list(pred.claim_ids),
                "diff_changed_pairs": [list(p) for p in pred.diff_changed_pairs],
            }

    if args.predictions_out is not None:
        args.predictions_out.parent.mkdir(parents=True, exist_ok=True)
        args.predictions_out.write_text(
            json.dumps(serializable, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    report = score_predictions(
        cases,
        {
            case_id: CasePrediction(
                doc_ids=tuple(payload["doc_ids"]),
                claim_ids=tuple(payload["claim_ids"]),
                diff_changed_pairs=tuple(
                    (str(p[0]), str(p[1]))
                    for p in payload.get("diff_changed_pairs") or []
                ),
            )
            for case_id, payload in serializable.items()
        },
    )
    out = report.to_dict()
    out["workspace_id"] = workspace_id
    out["gold"] = str(args.gold or default_gold_path())
    json.dump(out, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    if report.missing_predictions:
        return 2
    avgs = report.averages()
    if avgs.get("doc_hit_at_k", 1.0) < 1.0:
        return 1
    if avgs.get("claim_hit_at_k", 1.0) < 1.0:
        return 1
    if avgs.get("diff_changed_hit", 1.0) < 1.0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
