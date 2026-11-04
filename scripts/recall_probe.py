"""Measure whether the AI *finds* more, on a few pages, for a few dollars (harness rebuild, Phase 1).

The $0 exam (`scripts/exam_replay.py`) replays answers a paid run already wrote, so it can prove a
sorting or grading change is safe and can never show that a change makes the AI notice more. That
is the number that bounds the product: recall is 5-15%, and 48 of the 74 known problems were never
raised by any run. This is the cheap way to test the finding stage itself.

It runs the real full check on **only the pages that carry known problems**, plus a few control
pages that carry none, and scores what came back against the exam's miss tables.

    # what a probe would cost, and the pages it would use - nothing sent, nothing spent
    python scripts/recall_probe.py plan --job setA --kind "gv rule"
    python scripts/recall_probe.py plan --job setA --max-pages 12 --repeats 3

    # the same again, as a dry run of the real thing (still nothing sent)
    python scripts/recall_probe.py run --job setA --kind "gv rule" \\
        --api http://127.0.0.1:8071 --package <id> --out ~/gv-local/exam/probe/gv-rule

    # the paid run: only with --spend, only after the preflight passes
    python scripts/recall_probe.py run ... --spend

    # score saved probe runs, or two arms against each other
    python scripts/recall_probe.py score --out ~/gv-local/exam/probe/gv-rule

    # the fact cache
    python scripts/recall_probe.py cache --stats
    python scripts/recall_probe.py cache --coverage <set pack folder>

**Money.** Nothing here spends anything without `--spend`, and `--spend` is refused unless Docker
is answering with the sandbox image, more than 5 GB is free, and the app is already running. One
paid job at a time: repeats and arms go one after another, never together.

**Client values.** A probe's output is the client's drawings turned into findings, so `--out` must
be outside the repository; the script refuses an output folder inside a git tree.
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

from eval.exam import truth
from eval.probe import config, cost, factcache, facts, pages, preflight, runner, score

_DEFAULT_OUT = Path.home() / "gv-local" / "exam" / "probe"


def _selection(args: argparse.Namespace) -> pages.Selection:
    misses = truth.load_misses()
    jobs = pages.load_jobs()
    if args.job not in jobs:
        raise SystemExit(f"unknown job {args.job!r}; known: {', '.join(sorted(jobs))}")
    return pages.select(
        args.job,
        misses,
        vendor_pages=jobs[args.job],
        kinds=frozenset(args.kind) if args.kind else None,
        severities=frozenset(args.severity) if args.severity else None,
        miss_ids=frozenset(args.miss) if args.miss else None,
        max_pages=args.max_pages,
        controls=args.controls,
    )


def _arms(args: argparse.Namespace) -> list[config.Arm]:
    if args.arms:
        return config.load_arms(Path(args.arms).expanduser())
    return [config.Arm(name="today", note="the prompts this branch already has")]


def _outside_repo(where: Path) -> Path:
    """A probe's output is client data: refuse to write it inside a git tree."""
    where = where.expanduser().resolve()
    for folder in (where, *where.parents):
        if (folder / ".git").exists():
            raise SystemExit(
                f"{where} is inside a git tree ({folder}); a probe's answers are the client's "
                f"drawings turned into findings and must stay out of the repository"
            )
    return where


def _print_plan(selection: pages.Selection, plans: list[runner.Plan]) -> None:
    print(f"job {selection.job}")
    print(
        f"  pages            {len(selection.pages)}  "
        f"({len(selection.problem_pages)} carry a known problem, "
        f"{len(selection.control_pages)} control)"
    )
    print(f"  problem pages    {list(selection.problem_pages)}")
    print(f"  control pages    {list(selection.control_pages)}")
    print(
        f"  problems scored  {selection.scoreable} of {len(selection.asked)} asked"
        + (f"  (not covered: {len(selection.uncovered)})" if selection.uncovered else "")
        + (f"  (partly: {len(selection.partly)})" if selection.partly else "")
    )
    for plan in plans:
        money = plan.estimate
        print(f"\n  arm {plan.arm}")
        print(
            f"    per run        ${money.cost_usd:.2f} "
            f"(${money.low_usd:.2f}-${money.high_usd:.2f}), "
            f"{money.seconds / 60:.0f} min, ceiling ${money.ceiling_usd:.2f}"
        )
        print(
            f"    experiment     ${money.total_cost_usd:.2f} over {money.runs} run(s), "
            f"{money.total_seconds / 60:.0f} min, ceiling ${money.total_ceiling_usd:.2f}"
        )
        print(f"    fitted on      {money.fitted_on} runs already paid for")
        for line in plan.calls:
            print(f"    would call     {line}")
        for warning in plan.warnings:
            print(f"    NOTE           {warning}")


def _plan(args: argparse.Namespace) -> int:
    selection = _selection(args)
    arms = _arms(args)
    plans = [
        runner.plan_for(selection, arm, repeats=args.repeats, arms=len(arms), cap_usd=args.cap_usd)
        for arm in arms
    ]
    if args.json:
        Path(args.json).expanduser().write_text(
            json.dumps(
                {
                    "selection": {
                        "job": selection.job,
                        "problem_pages": list(selection.problem_pages),
                        "control_pages": list(selection.control_pages),
                        "covered": list(selection.covered),
                        "partly": list(selection.partly),
                        "uncovered": list(selection.uncovered),
                    },
                    "arms": [plan.as_dict() for plan in plans],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    _print_plan(selection, plans)
    return 0


def _save(where: Path, name: str, data: Any) -> None:
    where.mkdir(parents=True, exist_ok=True)
    (where / name).write_text(json.dumps(data, indent=1, default=str) + "\n", encoding="utf-8")


def _run(args: argparse.Namespace) -> int:
    selection = _selection(args)
    arms = _arms(args)
    plans = [
        runner.plan_for(selection, arm, repeats=args.repeats, arms=len(arms), cap_usd=args.cap_usd)
        for arm in arms
    ]
    _print_plan(selection, plans)

    report = preflight.check(
        api=args.api if args.spend else None,
        disk_path=Path(args.out).expanduser() if args.out else _DEFAULT_OUT,
        want_docker=not args.no_docker,
    )
    print("\npreflight")
    for line in report.lines:
        print(f"  {line}")

    if not args.spend:
        print("\nDry run: nothing was sent and nothing was spent. Add --spend to run it.")
        return 0
    if not report.ok:
        print("\nRefusing to start a paid probe:")
        for problem in report.problems:
            print(f"  - {problem}")
        return 2
    if not args.api:
        raise SystemExit("--spend needs --api (the running app's URL); the probe never starts it")
    if not (args.package or args.chat):
        raise SystemExit("--spend needs --package (or --chat) to say which drawing set to check")
    where = _outside_repo(Path(args.out) if args.out else _DEFAULT_OUT)
    headers = {
        name.strip(): value.strip()
        for name, value in (item.split(":", 1) for item in args.header if ":" in item)
    }

    runs: dict[str, list[score.RepeatFindings]] = {}
    chat = args.chat
    for plan in plans:
        runs[plan.arm] = []
        for repeat in range(1, args.repeats + 1):
            tag = f"{plan.arm}-r{repeat}"
            print(f"\n{tag}: asking for {len(plan.pages)} pages ...")
            result = runner.ask(
                args.api,
                package=args.package,
                chat=chat,
                body=plan.body,
                headers=headers,
                out=where / f"{tag}.events.jsonl",
            )
            chat = result.chat
            _save(where, f"{tag}.run.json", result.as_dict())
            if result.error:
                print(f"  failed: {result.error}")
                continue
            stored = runner.fetch_chat(args.api, result.chat, headers=headers)
            (where / f"{tag}.json").write_text(json.dumps(stored, indent=1), encoding="utf-8")
            found = _findings(where / f"{tag}.json")
            print(
                f"  ${result.cost_usd:.2f}, {result.seconds / 60:.0f} min, " f"{len(found)} answers"
            )
            runs[plan.arm].append(
                score.RepeatFindings(
                    repeat=repeat,
                    findings=found,
                    cost_usd=result.cost_usd,
                    seconds=result.seconds,
                )
            )
            cost.append_point(
                cost.Point(
                    run=f"{selection.job}-probe",
                    pages=len(plan.pages),
                    cost_usd=result.cost_usd,
                    seconds=result.seconds,
                    picked=True,
                    note=f"{plan.arm}, repeat {repeat}",
                )
            )
    _save(
        where,
        "selection.json",
        {
            "job": selection.job,
            "problem_pages": list(selection.problem_pages),
            "control_pages": list(selection.control_pages),
            "covered": list(selection.covered),
            "partly": list(selection.partly),
            "uncovered": list(selection.uncovered),
        },
    )
    return _report(selection, runs, where)


def _findings(path: Path) -> list[dict[str, Any]]:
    from eval.exam import replay

    return replay.findings_of(path)


def _percent(share: float | None) -> str:
    return "-" if share is None else f"{100 * share:.0f}%"


def _report(
    selection: pages.Selection,
    runs: dict[str, list[score.RepeatFindings]],
    where: Path,
) -> int:
    misses = truth.load_misses()
    scores = {
        arm: score.score_arm(arm, selection, repeats, misses)
        for arm, repeats in runs.items()
        if repeats
    }
    if not scores:
        print("\nNo finished run to score.")
        return 1
    print("\nrecall")
    for arm, result in scores.items():
        mean = _percent(result.mean_recall)
        best = _percent(result.best_recall)
        noise = result.control_noise
        loud = "-" if noise is None else f"{noise:.2f} flags a page"
        print(
            f"  {arm:14s} {result.found_every_time} found every time, {result.flaky} flaky, "
            f"{result.missed} missed, of {result.scoreable} scoreable"
        )
        print(
            f"  {'':14s} mean recall {mean}, best {best}, control noise {loud}, "
            f"${result.cost_usd:.2f}"
        )
    out: dict[str, Any] = {arm: result.as_dict() for arm, result in scores.items()}
    names = list(scores)
    if len(names) >= 2:
        out["compare"] = score.compare(scores[names[0]], scores[names[1]])
        print(f"\n  {out['compare']['verdict']}")
    _save(where, "score.json", out)
    print(f"\nwritten to {where / 'score.json'}")
    return 0


def _score(args: argparse.Namespace) -> int:
    where = Path(args.out).expanduser()
    stored = json.loads((where / "selection.json").read_text(encoding="utf-8"))
    selection = pages.Selection(
        job=stored["job"],
        problem_pages=tuple(stored["problem_pages"]),
        control_pages=tuple(stored["control_pages"]),
        covered=tuple(stored["covered"]),
        partly=tuple(stored.get("partly") or ()),
        uncovered=tuple(stored.get("uncovered") or ()),
        asked=tuple(stored["covered"]) + tuple(stored.get("uncovered") or ()),
    )
    runs: dict[str, list[score.RepeatFindings]] = {}
    for path in sorted(where.glob("*-r*.json")):
        if path.name.endswith(".run.json"):
            continue
        arm, _, rest = path.stem.rpartition("-r")
        meta = where / f"{path.stem}.run.json"
        spent = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
        runs.setdefault(arm, []).append(
            score.RepeatFindings(
                repeat=int(rest or 1),
                findings=_findings(path),
                cost_usd=float(spent.get("cost_usd") or 0.0),
                seconds=float(spent.get("seconds") or 0.0),
            )
        )
    return _report(selection, runs, where)


def _pairing(job: str) -> dict[int, int]:
    """The architect page each vendor page of this job belongs with, as the helpers settled it."""
    jobs = {tag: info["job"] for tag, info in truth.load_runs().items()}
    out: dict[int, int] = {}
    for row in truth.load_pairing():
        if jobs.get(row.run) != job:
            continue
        right = str(row.right_architect_page or "").strip()
        if right.isdigit():
            out.setdefault(row.vendor_page, int(right))
    return out


def _cache(args: argparse.Namespace) -> int:
    if args.stats:
        print(json.dumps(factcache.stats().as_dict(), indent=2))
        return 0
    pack = Path(args.coverage or args.harvest or "").expanduser()
    if not pack.is_dir():
        raise SystemExit("give --stats, or --coverage/--harvest with a set pack folder")
    only: frozenset[int] | None = None
    if args.job:
        picked = pages.select(
            args.job,
            truth.load_misses(),
            vendor_pages=pages.load_jobs()[args.job],
            kinds=frozenset(args.kind) if args.kind else None,
            controls=args.controls,
        )
        only = frozenset(picked.pages)
    harvested = facts.harvest_pack(pack, role=args.role or None, only=only)
    if args.coverage:
        report = facts.coverage(harvested).as_dict()
        if args.job:
            report["architect_side"] = facts.architect_cover(
                pack, [page.page for page in harvested], _pairing(args.job)
            )
        print(json.dumps(report, indent=2))
        return 0
    kept = 0
    for page in harvested:
        text = json.dumps(page.as_dict(), sort_keys=True).encode("utf-8")
        factcache.store(
            page.as_dict(),
            digest=factcache.page_digest(text),
            prompt_version=facts.HARVEST_VERSION,
            model="code",
            note=f"harvested from a set pack, page {page.page}",
        )
        kept += 1
    print(f"kept {kept} pages in {factcache.root()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    subs = parser.add_subparsers(dest="command", required=True)

    def _common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--job", required=True, help="setA, setB or setC")
        sub.add_argument(
            "--kind", action="append", default=[], help="only problems of this kind (repeatable)"
        )
        sub.add_argument(
            "--severity", action="append", default=[], help="should-flag or minor (repeatable)"
        )
        sub.add_argument("--miss", action="append", default=[], help="one problem id (repeatable)")
        sub.add_argument("--max-pages", type=int, help="cap the problem pages at the best N")
        sub.add_argument("--controls", type=int, default=2, help="pages with no known problem")
        sub.add_argument("--repeats", type=int, default=1, help="runs per arm (the stage is noisy)")
        sub.add_argument("--arms", help="a JSON file of arms to compare")
        sub.add_argument("--cap-usd", type=float, help="the message's own money cap")

    plan = subs.add_parser("plan", help="the pages and the price; nothing is sent")
    _common(plan)
    plan.add_argument("--json", help="also write the plan here")
    plan.set_defaults(run=_plan)

    run = subs.add_parser("run", help="dry run by default; --spend to really run it")
    _common(run)
    run.add_argument("--api", help="the running app's URL, e.g. http://127.0.0.1:8071")
    run.add_argument("--package", help="the drawing set's package id")
    run.add_argument("--chat", help="an existing chat to ask in")
    run.add_argument("--out", help="where to keep the answers (outside the repository)")
    run.add_argument("--header", action="append", default=[], help='an extra header, "Name: value"')
    run.add_argument("--spend", action="store_true", help="really run it, and really pay")
    run.add_argument("--no-docker", action="store_true", help="skip the Docker check (not advised)")
    run.set_defaults(run=_run)

    scored = subs.add_parser("score", help="score probe runs already saved")
    scored.add_argument("--out", required=True, help="the folder a probe run wrote")
    scored.set_defaults(run=_score)

    cache = subs.add_parser("cache", help="the fact cache")
    cache.add_argument("--stats", action="store_true")
    cache.add_argument("--coverage", help="a set pack folder: what share of the facts it holds")
    cache.add_argument("--harvest", help="a set pack folder: keep its pages' facts in the cache")
    cache.add_argument(
        "--role", default="vendor", help="the vendor or architect upload, or empty for every page"
    )
    cache.add_argument("--job", help="narrow to the pages a probe of this job would run")
    cache.add_argument("--kind", action="append", default=[], help="with --job: only this kind")
    cache.add_argument("--controls", type=int, default=2, help="with --job: control pages")
    cache.set_defaults(run=_cache)

    args = parser.parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":  # pragma: no cover - the command line
    raise SystemExit(main())
