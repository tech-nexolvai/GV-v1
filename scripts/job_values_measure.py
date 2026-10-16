#!/usr/bin/env python3
"""Measure the job values reader (#1407) on stored drawing sets: found / right / wrong / unreadable.

    .venv/bin/python scripts/job_values_measure.py \\
        --set set1=<AI_Set_1.pdf> --set set2=<AI_Set_2.pdf> --set set3=<AI_Set_3.pdf> \\
        [--architect-file set9=<architect.pdf>] --key <local key.json> --out ~/gv-local/<folder> \\
        [--allow-paid --cap-usd 3 --runs 2 --env-file <file with OPENROUTER_API_KEY>]

**No model is called unless `--allow-paid --cap-usd X` is given, and `GV_JOB_VALUES_READER=true`.**
Without them the script does everything code does at no cost (the architect drawings on each page,
the code finder's notes) and prints which drawings a paid run would send and what it would cost.
With them, at most X US dollars is spent over all runs: one spend guard reserves each call's worst
case before sending it, and the reading stops cleanly at the cap. The cap may not exceed $3.

**Nothing is paid twice within a folder.** Every attempt goes to `<out>/calls.jsonl` with its cost
and raw reply (the depth script's ledger, `scripts/depth_read.Ledger`). Run 1 is always asked; a
later run (`--runs 2`) asks again too, because measuring that the readers say the same twice is the
point. Re-running the script into the same folder replays stored answers instead.

**The readers:** the decided Claude pair through OpenRouter (Google Vertex only, no data kept),
keyed by `OPENROUTER_API_KEY` from the environment or `--env-file`. Prices from `--rates`.

**The key** is a local JSON file (never committed: it holds a client's values), per set and value:

    {"set1": {"cabinet_depth": {"printed": false}, ...},
     "set3": {"backsplash_thickness": {"printed": true, "value": "5/8\\"", "unsure_ok": false}}}

Grading per value: an auto or suggested value equal to the key is **right**; a value where the key
says not printed, or a different value, is **wrong**; a suggestion with two different readings, or a
refusal, is **unreadable** (a person picks); nothing where the key has a value is **missed** (or
right, where `unsure_ok`). **Pass:** 0 wrong in every run, and every value marked `"must_find":
true` found.

Read-only on every database: it reads PDFs only. Exit codes: 0 done, 2 refused.

Source: issue #1407.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from extraction.job_values.decide import SETTINGS, JobValueResult, Outcome
from extraction.job_values.read import (
    CLAUDE_PAIR,
    JOB_VALUES_PROMPT_ID,
    MEASURED_JOB_VALUES_SETTINGS,
    PreparedPanel,
    choose_panels,
    estimated_input_tokens,
    job_values_reader_enabled,
    prepare_pdf_panels,
    read_job_values,
)
from units.normalise import normalise_to_inches

REFUSED = 2
DEFAULT_RATES = REPO / "deploy" / "model_rates.us-east-1.json"
MAX_CAP_USD = Decimal(3)
#: The output a reply is expected to use, for the estimate only (the cap reserves the maximum).
EXPECTED_OUTPUT_TOKENS = 1500


class Refused(Exception):
    """The script will not do what was asked; the message says why."""


def _env_key(env_file: Path | None) -> str | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    if env_file is None or not env_file.exists():
        return None
    for line in env_file.read_text().splitlines():
        name, _, value = line.strip().removeprefix("export ").partition("=")
        if name.strip().upper() == "OPENROUTER_API_KEY" and value.strip():
            return value.strip().strip('"').strip("'")
    return None


def _named(pairs: Sequence[str]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for pair in pairs:
        name, _, path = pair.partition("=")
        if not name or not path:
            raise Refused(f"{pair!r}: write NAME=PATH")
        out[name] = Path(path).expanduser()
    return out


def prepare_set(pdf: Path, *, architect_file: bool) -> list[PreparedPanel]:
    from extraction.reader import read_pages

    data = pdf.read_bytes()
    panels: list[PreparedPanel] = []
    for index in range(len(read_pages(data))):
        panels += prepare_pdf_panels(
            data,
            index,
            document=pdf.name,
            architect_file=architect_file,
            settings=MEASURED_JOB_VALUES_SETTINGS,
        )
    return panels


def estimate_usd(panels: Sequence[PreparedPanel], rates: Any) -> Decimal:
    """What one run would cost: each chosen drawing's tokens at each reader's stated price."""
    chosen, _left = choose_panels(panels, MEASURED_JOB_VALUES_SETTINGS)
    total = Decimal(0)
    for prepared in chosen:
        tokens = estimated_input_tokens(prepared, MEASURED_JOB_VALUES_SETTINGS)
        for model in CLAUDE_PAIR:
            rate = rates.rate_for(model)
            total += (
                Decimal(tokens) * rate.input_per_1k_tokens
                + Decimal(EXPECTED_OUTPUT_TOKENS) * rate.output_per_1k_tokens
            ) / 1000
    return total


def _exact(text: str) -> Fraction:
    return normalise_to_inches(text).exact


def grade(result: JobValueResult, key: Mapping[str, Any] | None) -> str:
    """right / wrong / unreadable / missed / ungraded for one value."""
    if key is None:
        return "ungraded"
    printed = bool(key.get("printed"))
    if result.outcome in (Outcome.AUTO, Outcome.SUGGESTION):
        if result.value is None:
            return "unreadable"
        if not printed:
            return "wrong"
        return "right" if result.value == _exact(str(key["value"])) else "wrong"
    if result.outcome is Outcome.REFUSED:
        return "unreadable" if printed else "right"
    if printed and not key.get("unsure_ok"):
        return "missed"
    return "right"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--set", action="append", default=[], help="NAME=PDF (a combined sheet)")
    parser.add_argument(
        "--architect-file", action="append", default=[], help="NAME=PDF (the architect's own file)"
    )
    parser.add_argument("--key", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--cap-usd", type=Decimal)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--rates", type=Path, default=DEFAULT_RATES)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args(argv)

    sys.path.insert(0, str(REPO / "scripts"))
    from scripts.depth_dry_run import Refused as DryRefused
    from scripts.depth_dry_run import outside_git

    try:
        if args.allow_paid and (
            args.cap_usd is None or not Decimal(0) < args.cap_usd <= MAX_CAP_USD
        ):
            raise Refused(
                f"--allow-paid needs a spend cap: --cap-usd X (more than 0, at most ${MAX_CAP_USD})"
            )
        if args.cap_usd is not None and not args.allow_paid:
            raise Refused("--cap-usd is only for a paid run: add --allow-paid")
        if args.allow_paid and not job_values_reader_enabled(os.environ):
            raise Refused(
                "the job values reader is off: a paid run needs GV_JOB_VALUES_READER=true"
            )
        if not 1 <= args.runs <= 3:
            raise Refused("--runs is 1 to 3")
        sets = {name: (path, False) for name, path in _named(args.set).items()}
        sets.update({name: (path, True) for name, path in _named(args.architect_file).items()})
        if not sets:
            raise Refused("name at least one set: --set NAME=PDF")
        out = outside_git(args.out)
        key = {} if args.key is None else json.loads(args.key.read_text())
    except (Refused, DryRefused, ValueError) as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return REFUSED

    from app.runs.rates import load_model_rates
    from scripts.depth_read import Ledger
    from workflow.reader_reuse import question_identity

    rates = load_model_rates(args.rates)
    out.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(out / "calls.jsonl", rates)

    prepared: dict[str, list[PreparedPanel]] = {}
    estimate = Decimal(0)
    for name, (pdf, architect_file) in sets.items():
        prepared[name] = prepare_set(pdf, architect_file=architect_file)
        chosen, left = choose_panels(prepared[name], MEASURED_JOB_VALUES_SETTINGS)
        cost = estimate_usd(prepared[name], rates)
        estimate += cost
        print(
            f"{name}: {len(prepared[name])} architect drawing(s); a paid run sends {len(chosen)} "
            f"(pages {sorted({p.panel.page_index + 1 for p in chosen})}), about ${cost:.2f} a run; "
            f"{len(left)} not sent"
        )
    print(
        f"estimate: about ${estimate:.2f} a run, ${estimate * args.runs:.2f} for {args.runs} run(s)"
    )

    clients: Mapping[str, Any] | None = None
    guard = None
    if args.allow_paid:
        from extraction.slot_reader.anthropic import BatchSpendGuard
        from extraction.slot_reader.openrouter import ThreadLocalOpenRouterClients

        api_key = _env_key(args.env_file)
        if api_key is None:
            print(
                "refused: OPENROUTER_API_KEY is not set (environment or --env-file)",
                file=sys.stderr,
            )
            return REFUSED
        shared = ThreadLocalOpenRouterClients(api_key, timeout_seconds=180)

        class _PerThread:
            def converse(self, **request: Any) -> Any:
                return shared.for_current_thread().converse(**request)

        clients = dict.fromkeys(CLAUDE_PAIR, _PerThread())
        guard = BatchSpendGuard(args.cap_usd, rates)

    def stored(model: str, packet: Mapping[str, object]) -> str | None:
        identity = question_identity(packet)
        found = ledger.stored.get((model, JOB_VALUES_PROMPT_ID, identity or ""))
        return None if found is None else found[0]

    report: dict[str, Any] = {"runs": [], "estimate_usd": str(estimate)}
    passed = True
    for run in range(1, args.runs + 1):
        tally: Counter[str] = Counter()
        rows: dict[str, Any] = {}
        for name, panels in prepared.items():
            reading = read_job_values(
                panels,
                clients=clients,
                settings=MEASURED_JOB_VALUES_SETTINGS,
                record_attempt=ledger.record,
                spend_guard=guard,
                stored=stored if run == 1 else None,
            )
            rows[name] = {}
            for setting in SETTINGS:
                result = reading.results[setting]
                verdict = grade(result, key.get(name, {}).get(setting))
                tally[verdict] += 1
                must = key.get(name, {}).get(setting, {}).get("must_find")
                if verdict == "wrong" or (must and verdict != "right"):
                    passed = False
                rows[name][setting] = {
                    "outcome": result.outcome.value,
                    "value": None if result.value is None else str(result.value),
                    "page": None if result.page_index is None else result.page_index + 1,
                    "source": result.source,
                    "grade": verdict,
                    "reasons": list(result.reasons),
                    "held": list(result.held),
                }
                print(
                    f"run {run} {name} {setting}: {result.outcome.value}"
                    + ("" if result.value is None else f" {result.value}")
                    + ("" if result.page_index is None else f" p.{result.page_index + 1}")
                    + f" -> {verdict}"
                )
            if reading.stopped:
                print(f"run {run} {name}: stopped ({reading.stopped})")
        report["runs"].append({"tally": dict(tally), "values": rows})
        print(f"run {run}: {dict(tally)}")
    report["spent_usd"] = str(ledger.spent)
    report["calls"] = ledger.calls
    report["reused"] = ledger.reused
    report["pass"] = passed and bool(args.allow_paid)
    (out / "report.json").write_text(json.dumps(report, indent=1))
    print(
        f"paid calls {ledger.calls} (${ledger.spent:.4f}), reused {ledger.reused}; "
        + (
            "PASS"
            if report["pass"]
            else "not a pass" + ("" if args.allow_paid else " (no paid run)")
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
