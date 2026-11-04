"""Build the exam's answer files from the saved runs and the hand audits (harness rebuild, Phase 0).

The inputs are client data and stay out of this repository: the saved run JSONs are the client's
drawings turned into findings, and the hand audits quote them freely. This reads both from a local
manifest, joins every audited row to the finding it judged, and writes the two files the exam
scores against — `eval/exam/cases.tsv` and `eval/exam/pairing.tsv` — carrying **no client text**:
a case is a run tag, a finding id, a page number, who raised it, and a neutral label
(`eval/exam/labels.py`). `misses.tsv` and `miss_status.tsv` are written by hand from the audits'
own "Misses" sections and are not generated here, because a miss has no finding to join to.

    python scripts/build_exam.py --manifest ~/gv-local/exam/manifest.json

The manifest is a JSON object `{"base": "<folder>", "runs": {"<tag>": {"run": "<file>", "audits":
["<file>", ...], "flag_mode": false, "job": "setA"}}}`, with every path relative to `base`.

It is read-only on its inputs, makes no AI call and touches no database.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # pragma: no cover - the usual script preamble
    sys.path.insert(0, str(ROOT))

from eval.exam.audit_parse import parse_audit
from eval.exam.labels import label_of
from eval.exam.truth import CASES, PAIRING

CASE_COLUMNS = (
    "run",
    "finding_id",
    "page",
    "by",
    "category",
    "label",
    "stored_group",
    "verdict",
    "audit",
)


def _findings(path: Path) -> dict[str, dict[str, Any]]:
    """Every answer of a saved run's newest full check, by finding id. The loader is the one the
    stuck report uses, so the exam sees exactly the answers a person saw."""
    from app.review.assistant import stuck_report

    blocks, _ = stuck_report._blocks_of(json.loads(path.read_text(encoding="utf-8")))
    rows, _ = stuck_report.answers(blocks)
    return {str(finding.get("id")): finding for finding in rows}


def _who(finding: dict[str, Any]) -> str:
    """Which stage wrote the answer, with a helper's number dropped: the number is the helper's
    place in one run's roster, so it means nothing across runs and only widens the data."""
    author = str(finding.get("created_by") or "unknown")
    return author.split(":", 1)[0] if author.startswith("helper") else author


def build(manifest: dict[str, Any], base: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """The exam cases and the rows that could not be joined, for every run in the manifest."""
    from app.review.assistant import stuck_report

    cases: list[dict[str, Any]] = []
    unmatched: list[str] = []
    for tag, entry in manifest["runs"].items():
        findings = _findings(base / entry["run"])
        seen: set[str] = set()
        for audit in entry["audits"]:
            name = Path(audit).stem
            for row in parse_audit(base / audit, name):
                if row.finding_id in seen:
                    continue
                finding = findings.get(row.finding_id)
                if finding is None:
                    unmatched.append(f"{tag}\t{name}\t{row.finding_id}\tno such finding in the run")
                    continue
                seen.add(row.finding_id)
                page = stuck_report._page_number(finding.get("page"))
                if row.page is not None and row.page != page:
                    unmatched.append(
                        f"{tag}\t{name}\t{row.finding_id}\taudit says page {row.page}, run says {page}"
                    )
                cases.append(
                    {
                        "run": tag,
                        "finding_id": row.finding_id,
                        "page": page,
                        "by": _who(finding),
                        "category": str(finding.get("category") or "other"),
                        "label": label_of(str(finding.get("item") or "")),
                        "stored_group": stuck_report.group_of(finding),
                        "verdict": row.verdict,
                        "audit": name,
                    }
                )
    cases.sort(key=lambda case: (case["run"], case["page"], int(case["finding_id"][1:])))
    return cases, unmatched


def _digest(cases: list[dict[str, Any]]) -> str:
    """A short fingerprint of the case list, so a run registry can say which build it describes."""
    text = "\n".join(f"{c['run']}/{c['finding_id']}/{c['verdict']}" for c in cases)
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def write_tsv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    """Write rows as a tab-separated file with a header, overwriting what was there."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(columns), delimiter="\t", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="the local manifest of runs and audits")
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--pairing", type=Path, default=PAIRING, help="checked, never written")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest).expanduser()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = Path(manifest.get("base", manifest_path.parent)).expanduser()

    cases, unmatched = build(manifest, base)
    write_tsv(args.cases, CASE_COLUMNS, cases)

    per_run = Counter(case["run"] for case in cases)
    print(f"{len(cases)} exam cases, fingerprint {_digest(cases)}")
    for tag, count in sorted(per_run.items()):
        groups = Counter(c["stored_group"] for c in cases if c["run"] == tag)
        print(f"  {tag:<12} {count:>4}  {dict(sorted(groups.items()))}")
    print(f"{len(unmatched)} audited rows could not be joined")
    for line in unmatched:
        print(f"  {line}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
