"""Measure semantic-typing coverage/accuracy against human-confirmed gold labels only.

The script intentionally makes no prediction from a drawing.  Give it an optional JSON object of
``{case_id: semantic_type_or_null}`` emitted by a typing run; without it, it reports the safe
all-abstain baseline.  The gold-set files live under gitignored ``data/`` in normal use.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from eval.semantic_typing import TypingLabel, TypingPrediction, score_typing
from vocabulary.semantic_types import SemanticType


def _labels(root: Path) -> tuple[TypingLabel, ...]:
    labels: list[TypingLabel] = []
    for path in sorted(root.rglob("answer_key.json")):
        payload: dict[str, Any] = json.loads(path.read_text())
        annotator = str(payload.get("provenance", {}).get("annotator", ""))
        if "PM-confirmed" not in annotator:
            continue
        for observation in payload.get("ground_truth", {}).get("observations", []):
            labels.append(
                TypingLabel(
                    case_id=str(payload["id"]),
                    semantic_type=SemanticType(str(observation["semantic_type"])),
                )
            )
    return tuple(labels)


def _predictions(path: Path | None) -> tuple[TypingPrediction, ...]:
    if path is None:
        return ()
    raw: dict[str, str | None] = json.loads(path.read_text())
    return tuple(
        TypingPrediction(
            case_id=case_id,
            semantic_type=None if semantic_type is None else SemanticType(semantic_type),
        )
        for case_id, semantic_type in sorted(raw.items())
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("goldset", type=Path)
    parser.add_argument("--predictions", type=Path)
    args = parser.parse_args()
    score = score_typing(_labels(args.goldset), _predictions(args.predictions))
    accuracy = (
        "n/a (no automatic types)"
        if score.accuracy_when_typed is None
        else f"{score.accuracy_when_typed:.1%}"
    )
    print("Semantic typing scorecard")
    print(f"  eligible human-confirmed labels: {score.eligible}")
    print(f"  automatic type coverage: {score.predicted}/{score.eligible} ({score.coverage:.1%})")
    print(f"  exact accuracy when typed: {score.correct}/{score.predicted} ({accuracy})")
    print(f"  abstentions / REVIEW_REQUIRED: {score.abstained}/{score.eligible}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
