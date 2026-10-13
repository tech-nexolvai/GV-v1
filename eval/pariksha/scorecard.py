"""Store a Pariksha scorecard as one immutable, versioned JSON file (#1219).

Why a file and not the `evaluation_runs` tables is explained in `eval/pariksha/__init__.py`. What the
tables guarantee, the file keeps:

* **Immutable.** A scorecard is written once, with exclusive creation; writing to an existing path
  raises. A trend built from editable records would mean nothing (`EvaluationRun` is `Immutable`
  for the same reason).
* **Tamper-evident.** The file carries the SHA-256 of its own canonical contents; a file whose
  contents no longer match is refused on reading.
* **Exact.** Every metric is stored as its numerator and denominator and rebuilt from them, never
  from a decimal (`eval/runs.py` does the same with `metric_results`); a denominator of 0 reads back
  as "not measured", never as 0.

Scorecards hold what the readers read on client drawings: keep them under `~/gv-local/`, never in
git (`scripts/pariksha_scorecard.py` refuses a path inside the repository).

Source: issue #1219 · Verification: `tests/eval/test_pariksha_scorecard.py`
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

from eval.metrics import MetricResult
from eval.pariksha.score import CaseScore, ReaderScore, Scorecard

__all__ = [
    "SCORECARD_FORMAT",
    "ScorecardError",
    "read_scorecard",
    "scorecard_document",
    "write_scorecard",
]

SCORECARD_FORMAT: Final = "pariksha-scorecard/1"


class ScorecardError(ValueError):
    """The file is not a `pariksha-scorecard/1` scorecard, or it was changed after it was written."""


def _digest(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def scorecard_document(card: Scorecard) -> dict[str, Any]:
    """The scorecard as JSON, without its hash."""
    return {
        "format": SCORECARD_FORMAT,
        "agent": card.agent,
        "key": {"name": card.key_name, "version": card.key_version, "sha256": card.key_sha256},
        "code_version": card.code_version,
        "scorer_version": card.scorer_version,
        "extractor_versions": dict(card.extractor_versions),
        "source": dict(card.source),
        "created_at": card.created_at,
        "metrics": {
            name: {
                "numerator": metric.numerator,
                "denominator": metric.denominator,
                "note": metric.note,
            }
            for name, metric in card.metrics.items()
        },
        "cases": [
            {
                "case_id": case.case_id,
                "question": case.question,
                "expected": case.expected,
                "outcome": case.outcome,
                "readers": [
                    {
                        "model_id": reader.model_id,
                        "answer": reader.answer,
                        "verdict": reader.verdict,
                    }
                    for reader in case.readers
                ],
                "cost_micros": case.cost_micros,
                "note": case.note,
            }
            for case in card.cases
        ],
    }


def write_scorecard(card: Scorecard, path: Path) -> None:
    """Write the scorecard once; an existing file is never overwritten."""
    document = scorecard_document(card)
    document["sha256"] = _digest(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(document, handle, indent=1, ensure_ascii=False)
        handle.write("\n")


def _metric(name: str, data: Mapping[str, Any]) -> MetricResult:
    numerator, denominator = int(data["numerator"]), int(data["denominator"])
    return MetricResult(
        key=name,
        value=Fraction(numerator, denominator) if denominator else None,
        numerator=numerator,
        denominator=denominator,
        note=str(data.get("note") or ""),
    )


def read_scorecard(path: Path) -> Scorecard:
    """Read a stored scorecard, refusing one of another format or one changed after writing."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ScorecardError(f"{path.name} is not JSON: {error}") from error
    if not isinstance(document, dict) or document.get("format") != SCORECARD_FORMAT:
        raise ScorecardError(f"{path.name} is not a {SCORECARD_FORMAT} scorecard")
    stored = document.pop("sha256", None)
    if stored != _digest(document):
        raise ScorecardError(
            f"{path.name} was changed after it was written (its hash no longer matches); "
            "a scorecard is a record, so score the run again instead of editing one"
        )
    try:
        return Scorecard(
            agent=document["agent"],
            key_name=document["key"]["name"],
            key_version=document["key"]["version"],
            key_sha256=document["key"]["sha256"],
            code_version=document["code_version"],
            scorer_version=document["scorer_version"],
            extractor_versions=dict(document["extractor_versions"]),
            source=dict(document["source"]),
            metrics={name: _metric(name, data) for name, data in document["metrics"].items()},
            cases=tuple(
                CaseScore(
                    case_id=case["case_id"],
                    question=case["question"],
                    expected=case["expected"],
                    outcome=case["outcome"],
                    readers=tuple(
                        ReaderScore(
                            model_id=reader["model_id"],
                            answer=reader["answer"],
                            verdict=reader["verdict"],
                        )
                        for reader in case["readers"]
                    ),
                    cost_micros=int(case["cost_micros"]),
                    note=case["note"],
                )
                for case in document["cases"]
            ),
            created_at=document["created_at"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ScorecardError(f"{path.name} is missing a scorecard field: {error}") from error
