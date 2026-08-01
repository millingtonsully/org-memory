"""CLI: score a predictions JSON file against the retrieval gold set.

Example::

    python -m org_memory.eval.score_retrieval --predictions preds.json

Predictions file shape::

    {
      "case_id": {"doc_ids": ["doc:a", "doc:b"], "claim_ids": ["claim:1"]}
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from org_memory.eval.harness import (
    default_gold_path,
    load_gold_set,
    predictions_from_mapping,
    score_predictions,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gold",
        type=Path,
        default=None,
        help=f"Gold set JSON (default: {default_gold_path()})",
    )
    parser.add_argument(
        "--predictions",
        type=Path,
        required=True,
        help=(
            "JSON map of case_id to ranked doc_ids / claim_ids "
            "(see evals/retrieval/example_predictions.json)"
        ),
    )
    args = parser.parse_args(argv)

    if not args.predictions.is_file():
        example = Path("evals/retrieval/example_predictions.json")
        raise SystemExit(
            f"predictions file not found: {args.predictions}\n"
            f"Create that file, or try the shipped example:\n"
            f"  python -m org_memory.eval.score_retrieval "
            f"--predictions {example.as_posix()}"
        )

    cases = load_gold_set(args.gold)
    raw = json.loads(args.predictions.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit("predictions file must be a JSON object")
    report = score_predictions(cases, predictions_from_mapping(raw))
    json.dump(report.to_dict(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    if report.missing_predictions:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
