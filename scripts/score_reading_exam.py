"""Score a reader's `stroke_reads.json` against the reading exam's confirmed key, for nothing (rebuild plan Step 2 gate).

    python scripts/score_reading_exam.py --key ~/gv-local/exam/reading/<date>/key.tsv \\
        --reads <pack>/stroke_reads.json [--reads ...] [--answers answers.tsv] [--job setA]

`--answers` is the two-column TSV `index.html` copies (`id`, `confirmed`); it fills the key's
`confirmed` column where given. `--job` keeps only that job's key rows (one pack's reads cover one
job, so the other jobs' rows would all be unmatched). `--pack TAG=FOLDER` names a pack whose
`dims.json` gives a key row's file name when the key has no `file` column.

It prints, per reason and overall: rows confirmed, matched to a label, sealed (and witnessed by the
scale), the read rate, right, **agreed-wrong**, unverifiable, held, unread and unmatched; then each
agreed-wrong and unmatched row. **It exits 1 when any reading is agreed-wrong**: that is the gate,
and it must be 0. The output names the key's ids and printed values, which are client values: keep
it local.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # pragma: no cover - the usual script preamble
    sys.path.insert(0, str(ROOT))

from eval.exam import reading, reading_score


def _answers(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8", newline="") as handle:
        return {
            row["id"]: row.get("confirmed") or ""
            for row in csv.DictReader(handle, delimiter="\t")
            if row.get("id")
        }


def _files(values: list[str]) -> dict[tuple[str, int], str]:
    out: dict[tuple[str, int], str] = {}
    for value in values:
        tag, _, folder = value.partition("=")
        dims = json.loads((Path(folder).expanduser() / "dims.json").read_text(encoding="utf-8"))
        for index, name in enumerate(dims.get("files") or []):
            out[(tag, index)] = str(name)
    return out


def _pct(value: float | None) -> str:
    return "    -" if value is None else f"{value * 100:4.0f}%"


def table(per: dict[str, reading_score.Tally], overall: reading_score.Tally) -> list[str]:
    """The score as a person reads it: one line per reason, then the whole exam."""
    head = (
        f"{'reason':<18} {'conf':>4} {'match':>5} {'sealed':>6} {'witn':>4} {'rate':>5} "
        f"{'right':>5} {'WRONG':>5} {'unver':>5} {'held':>4} {'unread':>6} {'unmatch':>7}"
    )
    lines = [head, "-" * len(head)]
    order = [*reading.REASONS, *reading.CONTROLS]
    names = sorted(per, key=lambda name: (order.index(name) if name in order else 99, name))
    for name, tally in [*((name, per[name]) for name in names), ("ALL", overall)]:
        lines.append(
            f"{name:<18} {tally.confirmed:>4} {tally.matched:>5} {tally.sealed:>6} "
            f"{tally.witnessed:>4} {_pct(tally.read_rate):>5} {tally.right:>5} "
            f"{tally.agreed_wrong:>5} {tally.unverifiable:>5} {tally.held:>4} "
            f"{tally.unread:>6} {tally.unmatched:>7}"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--key", type=Path, required=True, help="the reading exam's key.tsv")
    parser.add_argument("--reads", type=Path, action="append", required=True)
    parser.add_argument("--answers", type=Path, help="id/confirmed TSV from index.html")
    parser.add_argument("--job", action="append", default=[], help="keep only this job's rows")
    parser.add_argument("--pack", action="append", default=[], help="TAG=FOLDER for file names")
    parser.add_argument("--json", type=Path, help="also write the tallies as JSON")
    args = parser.parse_args(argv)

    rows = reading.read_key(args.key.expanduser())
    if args.answers:
        rows = reading_score.with_answers(rows, _answers(args.answers.expanduser()))
    if args.job:
        rows = [row for row in rows if row.job in set(args.job)]
    reads = [read for path in args.reads for read in reading_score.load_reads(path.expanduser())]
    per, overall, judged, outside = reading_score.score(rows, reads, files=_files(args.pack))

    lines = table(per, overall)
    lines.append("")
    lines.append(
        f"{len(reads)} labels read; {outside} of them are not in the exam. "
        f"Gate: agreed-wrong = {overall.agreed_wrong} (must be 0)."
    )
    for item in judged:
        if item.outcome == "agreed_wrong" and item.read is not None and item.truth is not None:
            lines.append(
                f"  AGREED-WRONG {item.row.id} ({item.row.reason}): printed "
                f"{item.truth.text!r}, sealed {item.read.text!r} at turn {item.read.turn} "
                f"({item.read.state})"
            )
    unmatched = [item.row.id for item in judged if item.outcome == "unmatched"]
    if unmatched:
        lines.append(f"  unmatched key rows ({len(unmatched)}): {', '.join(unmatched)}")
    print("\n".join(lines))
    if args.json:
        args.json.write_text(
            json.dumps(reading_score.summary(per, overall), indent=2) + "\n", encoding="utf-8"
        )
    return 1 if overall.agreed_wrong else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
