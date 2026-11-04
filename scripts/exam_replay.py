"""Score the current code against the hand-audited exam, for nothing, in a couple of minutes.

The rebuild's rule (Anant, 2026-11-04): every change is scored on the hand-audited real jobs before
any paid run. This is that gate. It loads each saved run's findings, re-groups them with the code on
this branch, and scores both those groups and the ones the run stored against `eval/exam/`. No AI
call, no network, no database, no drawing.

    python scripts/exam_replay.py --runs ~/gv-local/exam/runs.json
    python scripts/exam_replay.py --runs ~/gv-local/exam/runs.json --json out.json

`--runs` names a local JSON file `{"base": "<folder>", "runs": {"<tag>": {"run": "<file>"}}}`,
because a saved run is the client's drawings turned into findings and never lives in this
repository. Everything the exam itself knows — which answers were audited, what the helpers said,
which architect page each vendor page belonged to, and the real problems no run raised — is in
`eval/exam/` and carries no client text.

What it prints, in the order it matters:

1. **Recall against the known problems.** How many of the real problems the helpers found on the
   drawings each run actually raised. Every finding in this product is written by an AI and code
   only does arithmetic on what the AI already wrote down, so this is the number that bounds the
   product. It is a ceiling, not a census: the helpers read the pages they were given, so the true
   recall is lower.
2. **Stability.** Problems one run found and another missed on the same drawings.
3. **Precision on the audited answers**, with the leaks: a real difference filed as Not compared,
   and an agreement filed the same way.
4. **Pairing**, which is upstream of all of it.

`--stage pairing` or `--stage diff` prints one stage exam alone (rebuild plan Step 1A), so a
pairing failure is never read as a finding failure:

- **pairing**: per run, the audited vendor pages paired exactly, paired right but only as a best
  guess, rightly left with nothing to compare, on the right sheet but the wrong view, on a wrong
  page, and left unpaired where the audit names a page;
- **diff**: per run and side, red precision, please-check precision and recall against the miss
  list (found, partly, missed), on every audited answer and on the pages paired right only.

`--stage all` (the default) prints the whole report, then both stage tables.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # pragma: no cover - the usual script preamble
    sys.path.insert(0, str(ROOT))

from eval.exam import replay, truth


def _load_runs(path: Path) -> tuple[Path, dict[str, dict[str, Any]]]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    return Path(manifest.get("base", path.parent)).expanduser(), dict(manifest["runs"])


def run(manifest_path: Path) -> dict[str, Any]:
    """Everything the exam can say about this branch, as data."""
    base, manifest = _load_runs(manifest_path)
    registry = truth.load_runs()
    cases = truth.load_cases()
    pairing = truth.load_pairing()
    misses = truth.load_misses()
    status = truth.load_miss_status()

    findings: dict[str, list[dict[str, Any]]] = {}
    groups: dict[str, dict[str, tuple[str, str]]] = {}
    guesses: dict[str, dict[int, set[int]]] = {}
    for tag, entry in manifest.items():
        known = registry.get(tag, {})
        rows = replay.findings_of(base / entry["run"])
        findings[tag] = rows
        groups[tag] = replay.groups_of(rows, bool(known.get("flag_mode")))
        guesses[tag] = replay.guessed_pages(rows, replay.coverage_of(base / entry["run"]))

    jobs = {tag: str(registry[tag]["job"]) for tag in manifest if tag in registry}
    scores = replay.score_cases(cases, groups)
    recall = replay.score_misses(misses, status, findings, jobs)
    saved_pairing = [row for row in pairing if row.run in manifest]
    right_pages = replay.score_cases(
        cases, groups, replay.right_paired_pages(saved_pairing, guesses)
    )
    return {
        "cases": len(cases),
        "runs": sorted(manifest),
        "answers": {tag: len(rows) for tag, rows in findings.items()},
        "recall": recall,
        "stability": replay.stability(misses, status, jobs),
        "precision": [
            {
                "run": score.run,
                "side": score.side,
                "cases": score.cases,
                "listed": score.listed,
                "precision": score.precision,
                "recall_within_answers": score.recall,
                "reds": score.reds,
                "red_precision": score.red_precision,
                "leaked_problems": score.leaks_problem,
                "leaked_agreements": score.leaks_agreement,
                "listed_right": score.listed_right,
                "should_list": score.should_list,
                "reds_true": score.reds_true,
                "unknown": score.unknown,
                "per_group": {
                    group: dict(counts) for group, counts in sorted(score.per_group.items())
                },
            }
            for score in scores
        ],
        "pairing": {
            tag: dict(counts) for tag, counts in sorted(replay.score_pairing(pairing).items())
        },
        "never_found": [miss.miss_id for miss in replay.never_found(misses, status)],
        "never_found_kinds": replay.never_found_kinds(misses, status),
        "moved": _moved(groups),
        "pairing_stage": replay.pairing_stage(saved_pairing, guesses),
        "diff_stage": replay.diff_stage(scores, right_pages, recall),
    }


def _moved(groups: dict[str, dict[str, tuple[str, str]]]) -> dict[str, dict[str, int]]:
    """Per run, how many answers this branch would put somewhere other than the run stored. This
    is the gap between what the user saw and what today's code does; Phase 2 closes it."""
    out: dict[str, dict[str, int]] = {}
    for tag, pairs in sorted(groups.items()):
        moves: dict[str, int] = {}
        for stored, today in pairs.values():
            if stored != today:
                moves[f"{stored} -> {today}"] = moves.get(f"{stored} -> {today}", 0) + 1
        out[tag] = dict(sorted(moves.items(), key=lambda item: -item[1]))
    return out


def report(result: dict[str, Any]) -> list[str]:
    """The short table a person reads."""
    lines: list[str] = []
    lines.append("RECALL against the known real problems (a ceiling: the audits sampled pages)")
    lines.append(
        f"{'run':<11} {'job':<6} {'known':>5} {'scored':>6} {'found':>5} {'partly':>6} "
        f"{'missed':>6} {'recall':>7} {'matcher':>8}"
    )
    lines.append("-" * 70)
    for tag, row in sorted(result["recall"].items()):
        hand = row["hand"]
        recall = row["recall"]
        lines.append(
            f"{tag:<11} {row['job']:<6} {row['known_problems']:>5} {row['scored']:>6} "
            f"{hand.get('found', 0):>5} {hand.get('partly', 0):>6} {hand.get('missed', 0):>6} "
            f"{'  -  ' if recall is None else f'{recall * 100:6.1f}%'} "
            f"{row['auto'].get('found', 0):>8}"
        )
    lines.append("")
    lines.append("STABILITY: problems one run found and another missed, same drawings")
    for job, row in sorted(result["stability"].items()):
        lines.append(
            f"  {job}: {row['swing']} known problems swing between {', '.join(row['runs'])}"
        )
    lines.append(f"  never found by any run: {len(result['never_found'])}")
    for kind, count in result["never_found_kinds"].items():
        lines.append(f"    {kind:<22} {count}")
    lines.append("")
    lines.append("PRECISION on the audited answers (leak! = a real problem filed as Not compared,")
    lines.append("leak= = an agreement filed the same way)")
    scores = [
        replay.Scores(
            run=row["run"],
            side=row["side"],
            cases=row["cases"],
            listed=row["listed"],
            listed_right=row["listed_right"],
            should_list=row["should_list"],
            leaks_problem=row["leaked_problems"],
            leaks_agreement=row["leaked_agreements"],
            reds=row["reds"],
            reds_true=row["reds_true"],
        )
        for row in result["precision"]
    ]
    lines.extend(replay.table(scores))
    lines.append("")
    lines.append("PAIRING: vendor pages sent to the right architect page")
    for tag, counts in result["pairing"].items():
        total = sum(counts.values())
        right = counts.get("yes", 0)
        lines.append(
            f"  {tag:<11} {right}/{total} right"
            + (f", {counts['view']} right page wrong view" if counts.get("view") else "")
            + (f", {counts['no']} wrong page" if counts.get("no") else "")
        )
    lines.append("")
    lines.append("WHAT TODAY'S CODE WOULD MOVE (stored group -> this branch's group)")
    for tag, moves in result["moved"].items():
        lines.append(
            f"  {tag:<11} " + (", ".join(f"{k}: {v}" for k, v in moves.items()) or "nothing")
        )
    return lines


def _pct(numerator: int, denominator: int) -> str:
    """`n/d (p%)`, or `-` when there is nothing to divide."""
    if not denominator:
        return "-"
    return f"{numerator}/{denominator} ({numerator / denominator * 100:.0f}%)"


def pairing_report(result: dict[str, Any]) -> list[str]:
    """The pairing exam as a person reads it: one line per run."""
    lines = [
        "PAIRING EXAM: each audited vendor page, by how it was paired",
        (
            f"{'run':<11} {'pages':>5} {'exact':>5} {'guess':>5} {'none ok':>7} {'view':>5} "
            f"{'wrong':>5} {'unpaired':>8}  wrong ones marked a guess"
        ),
        "-" * 84,
    ]
    for tag, row in result["pairing_stage"].items():
        wrong = row["wrong_view"] + row["wrong_page"]
        lines.append(
            f"{tag:<11} {row['pages']:>5} {row['exact']:>5} {row['best_guess']:>5} "
            f"{row['nothing_to_compare']:>7} {row['wrong_view']:>5} {row['wrong_page']:>5} "
            f"{row['unpaired']:>8}  {row['wrong_marked_guess']} of {wrong}"
        )
    lines.append(
        "exact = right page, not a guess; guess = right page, marked a best guess or unsure;"
    )
    lines.append(
        "none ok = rightly nothing to compare; view = right sheet, wrong view; wrong = another page"
    )
    return lines


def diff_report(result: dict[str, Any]) -> list[str]:
    """The diff exam as a person reads it: one line per run and side, then the miss list."""
    lines = [
        "DIFF EXAM: red and please-check precision (all audited answers | pages paired right)",
        (
            f"{'run':<11} {'side':<7} {'red precision':>15} {'please-check':>15} "
            f"{'red, right pages':>17} {'check, right pages':>19}"
        ),
        "-" * 90,
    ]
    for tag, sides in result["diff_stage"].items():
        for side in ("stored", "today"):
            row = sides.get(side)
            if row is None:
                continue
            clean = row["right_pages"] or {}
            lines.append(
                f"{tag:<11} {side:<7} {_pct(row['reds_true'], row['reds']):>15} "
                f"{_pct(row['checks_true'], row['checks']):>15} "
                f"{_pct(clean.get('reds_true', 0), clean.get('reds', 0)):>17} "
                f"{_pct(clean.get('checks_true', 0), clean.get('checks', 0)):>19}"
            )
    lines.append("")
    lines.append("DIFF EXAM: recall against the miss list (hand-scored; a ceiling)")
    lines.append(
        f"{'run':<11} {'known':>5} {'found':>5} {'partly':>6} {'missed':>6} {'unscored':>8} "
        f"{'recall':>7}"
    )
    lines.append("-" * 56)
    for tag, sides in result["diff_stage"].items():
        misses = sides.get("stored", next(iter(sides.values())))["misses"]
        recall = misses["recall"]
        lines.append(
            f"{tag:<11} {misses['known']:>5} {misses['found']:>5} {misses['partly']:>6} "
            f"{misses['missed']:>6} {misses['unscored']:>8} "
            f"{'   -  ' if recall is None else f'{recall * 100:6.1f}%'}"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", required=True, help="the local manifest of saved runs")
    parser.add_argument("--json", type=Path, help="also write the whole score as JSON, for CI")
    parser.add_argument(
        "--stage",
        choices=("all", "pairing", "diff"),
        default="all",
        help="print one stage exam alone (default: the whole report, then both stages)",
    )
    args = parser.parse_args(argv)

    result = run(Path(args.runs).expanduser())
    if args.stage == "pairing":
        lines = pairing_report(result)
    elif args.stage == "diff":
        lines = diff_report(result)
    else:
        lines = [*report(result), "", *pairing_report(result), "", *diff_report(result)]
    print("\n".join(lines))
    if args.json:
        args.json.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
