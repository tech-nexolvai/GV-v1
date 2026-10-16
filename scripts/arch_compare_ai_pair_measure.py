#!/usr/bin/env python3
"""Measure the direct compare's AI pair check on the three AI sets: right / wrong pairs, and cost.

    .venv/bin/python scripts/arch_compare_ai_pair_measure.py \\
        --set 1=<AI_Set_1.pdf> --set 2=<AI_Set_2.pdf> --set 3=<AI_Set_3.pdf> \\
        --verdicts <local verdicts.json> --old-pairs 1=<set1 pairs.json> --old-pairs 2=<...> \\
        --key 1=<arch_key.json> --key 2=<arch_key.json> --out ~/gv-local/<folder> \\
        [--eye <local eye.json>] [--allow-paid --cap-usd 5 --runs 2 --env-file <file>]

**No model is called unless `--allow-paid --cap-usd X` is given and `GV_ARCH_COMPARE_AI_PAIR=true`.**
Without them the script does all of code's work at no cost: the geometry pass on every page, every
question the AI check would ask with its picture (written to `<out>/pictures/`, local only), and the
cost estimate. With them, at most X US dollars is spent over all runs (one spend guard reserves each
call's worst case first); the cap may not exceed $10.

**The estimate** is each picture's documented visual tokens plus the question's words, at each
reader's stated price, with the output the pair's stored answers to a one-picture question used
(`--calls`, a `calls.jsonl` of an earlier paid run: its mean and largest output per call), or about
300 and 900 tokens when none is given.

**Nothing is paid twice within a folder.** Every attempt goes to `<out>/calls.jsonl`
(`scripts.depth_read.Ledger`); a re-run into the same folder replays stored answers. A second run
(`--runs 2`) asks again, because the two runs agreeing is part of the measurement.

**Scoring** (per set):

* an AI pair whose vendor line is the line of an earlier eye-checked pair (`--old-pairs`, the proof
  kit's `pairs.json`, and `--verdicts`, its right/wrong eye check, same page and architect
  dimension) takes that verdict;
* every other AI pair is graded by `--eye` (`{"<set>": {"<page>:<view>.<A>": "right"|"wrong"}}`),
  filled in by a person (or the eye check) from `<out>/check/`, where each new AI pair's picture is
  written with its chosen line; until then it is **unchecked**;
* the architect key (`--key`, local) is printed beside each AI pair whose text it lists, as context.

**Pass** (the standing rule): 0 wrong pairs in every run, no pair unchecked, and more right pairs
than the geometry pass alone gives. Writes `<out>/report.json`. Read-only on every database: it
reads PDFs and local JSON only. Exit codes: 0 done, 2 refused.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from extraction.compare.ai_pair import (
    AI_PAIR_PROMPT_ID,
    CLAUDE_PAIR,
    MEASURED_AI_PAIR_SETTINGS,
    PdfPictures,
    Question,
    ai_pair_enabled,
    estimated_input_tokens,
    pair_with_ais,
    questions,
)
from extraction.compare.pair import Pairing, PairStatus, compare_pdf

REFUSED = 2
DEFAULT_RATES = REPO / "deploy" / "model_rates.us-east-1.json"
MAX_CAP_USD = Decimal(10)
#: Without `--calls`: the pair's stored one-picture answers (job values, 2026-10-16) used about 250
#: output tokens a call, at most about 800.
OUTPUT_TOKENS, OUTPUT_TOKENS_HIGH = 300, 900
#: Two vendor lines are the same line when every end is within this many points.
SAME_LINE_PT = 2.0


class Refused(Exception):
    """The script will not do what was asked; the message says why."""


def _named(pairs: Sequence[str]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for pair in pairs:
        name, _, path = pair.partition("=")
        if not name or not path:
            raise Refused(f"{pair!r}: write NAME=PATH")
        out[name] = Path(path).expanduser()
    return out


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


def output_tokens(calls: Path | None) -> tuple[int, int]:
    """The likely and the largest output per call, from an earlier paid run's ledger."""
    if calls is None or not calls.exists():
        return OUTPUT_TOKENS, OUTPUT_TOKENS_HIGH
    seen = [
        row["output_tokens"]
        for row in map(json.loads, calls.read_text().splitlines())
        if isinstance(row.get("output_tokens"), int) and row.get("reused_from") is None
    ]
    if not seen:
        return OUTPUT_TOKENS, OUTPUT_TOKENS_HIGH
    return round(sum(seen) / len(seen)), max(seen)


def estimate_usd(asked: Sequence[Question], rates: Any, output: int) -> Decimal:
    total = Decimal(0)
    for question in asked:
        tokens = estimated_input_tokens(question, MEASURED_AI_PAIR_SETTINGS)
        for model in CLAUDE_PAIR:
            rate = rates.rate_for(model)
            total += (
                Decimal(tokens) * rate.input_per_1k_tokens
                + Decimal(output) * rate.output_per_1k_tokens
            ) / 1000
    return total


def suggested_cap(estimate: Decimal, runs: int) -> Decimal:
    """A quarter over the likely cost of every run, rounded up to a whole dollar, at most the
    script's own limit: the guard reserves each call's worst case, so the cap also stops a run
    whose replies run long before it can cost much more."""
    wanted = estimate * runs * Decimal("1.25")
    return min(MAX_CAP_USD, wanted.to_integral_value(rounding="ROUND_CEILING"))


def _tag(p: Pairing) -> str:
    return f"{p.page_index + 1}:{p.architect_view}.{p.architect.number}"


def _same_line(p: Pairing, box: Sequence[float] | None) -> bool:
    if p.vendor is None or box is None:
        return False
    mine = p.vendor.box
    return all(
        abs(float(a) - float(b)) <= SAME_LINE_PT
        for a, b in zip((mine.x0, mine.top, mine.x1, mine.bottom), box, strict=True)
    )


def grade(
    p: Pairing,
    verdicts: Mapping[str, Any],
    old: Mapping[str, Any],
    eye: Mapping[str, str],
) -> str:
    """right / wrong / unchecked for one AI pair."""
    tag = _tag(p)
    verdict = verdicts.get(tag, {})
    if verdict.get("pair") in ("right", "wrong") and _same_line(p, old.get(tag)):
        return str(verdict["pair"])
    if eye.get(tag) in ("right", "wrong"):
        return eye[tag]
    return "unchecked"


def _old_pairs(path: Path | None) -> dict[str, Any]:
    """The proof kit's earlier pairs: each paired architect dimension's vendor line box."""
    if path is None:
        return {}
    found: dict[str, Any] = {}
    for page in json.loads(path.read_text())["pages"]:
        for row in page["pairs"]:
            if row["status"] == "paired":
                found[f"{page['page']}:{row['arch_view']}.{row['A']}"] = row["vendor_box"]
    return found


def _key_note(p: Pairing, key: Mapping[str, Any]) -> str | None:
    def norm(text: str) -> str:
        return "".join(text.replace("’", "'").split())

    for page in key.get("pages", []):
        if str(page.get("page")) != str(p.page_index + 1):
            continue
        for width in page.get("arch_widths") or []:
            if norm(str(width.get("text", ""))) == norm(p.architect.text):
                return f"key: ticks on {width.get('ticks_on')}; {page.get('comparable')}"
    return None


def _check_picture(out: Path, name: str, question: Question, p: Pairing) -> Path:
    """The question's picture with the chosen line ringed, for the eye check (local only)."""
    import io

    from PIL import Image, ImageDraw

    image = Image.open(io.BytesIO(question.picture)).convert("RGB")
    draw = ImageDraw.Draw(image)
    draw.text(
        (8, image.height - 18), f"AI pair: line {_line_number(question, p)}", fill=(200, 0, 0)
    )
    path = (
        out
        / "check"
        / f"set{name}_p{p.page_index + 1}_A{p.architect_view}-{p.architect.number}.png"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def _line_number(question: Question, p: Pairing) -> int | None:
    for k, (line, _label) in enumerate(question.candidates, start=1):
        if line == p.vendor:
            return k
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--set", action="append", default=[], help="NAME=PDF")
    parser.add_argument("--verdicts", type=Path)
    parser.add_argument("--old-pairs", action="append", default=[], help="NAME=pairs.json")
    parser.add_argument("--key", action="append", default=[], help="NAME=arch_key.json")
    parser.add_argument("--eye", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--calls", type=Path, help="an earlier paid run's calls.jsonl")
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
        if args.allow_paid and not ai_pair_enabled(os.environ):
            raise Refused("the AI pair check is off: a paid run needs GV_ARCH_COMPARE_AI_PAIR=true")
        if not 1 <= args.runs <= 3:
            raise Refused("--runs is 1 to 3")
        sets = _named(args.set)
        if not sets:
            raise Refused("name at least one set: --set NAME=PDF")
        out = outside_git(args.out)
        old = {name: _old_pairs(path) for name, path in _named(args.old_pairs).items()}
        keys = {name: json.loads(path.read_text()) for name, path in _named(args.key).items()}
        verdicts = {} if args.verdicts is None else json.loads(args.verdicts.read_text())
        eye = {} if args.eye is None else json.loads(args.eye.read_text())
    except (Refused, DryRefused, ValueError) as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return REFUSED

    from app.runs.rates import load_model_rates
    from scripts.depth_read import Ledger
    from workflow.reader_reuse import question_identity

    rates = load_model_rates(args.rates)
    out.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(out / "calls.jsonl", rates)
    likely, high = output_tokens(args.calls)

    prepared: dict[str, tuple[bytes, Any, tuple[Pairing, ...], PdfPictures]] = {}
    estimate = high_estimate = Decimal(0)
    for name, pdf in sets.items():
        data = pdf.read_bytes()
        results = compare_pdf(data)
        pages = [page for page, _pairings in results]
        pairings = tuple(p for _page, found in results for p in found)
        pictures = PdfPictures(data, MEASURED_AI_PAIR_SETTINGS)
        asked, not_asked = questions(pages, pairings, pictures, MEASURED_AI_PAIR_SETTINGS)
        for question in asked:
            folder = out / "pictures"
            folder.mkdir(exist_ok=True)
            (folder / f"set{name}_{question.question_id}.png").write_bytes(question.picture)
        cost = estimate_usd(asked, rates, likely)
        estimate += cost
        high_estimate += estimate_usd(asked, rates, high)
        geometry = sum(p.status is PairStatus.PAIRED for p in pairings)
        tokens = sum(estimated_input_tokens(q, MEASURED_AI_PAIR_SETTINGS) for q in asked)
        print(
            f"set {name}: {len(pairings)} architect dimensions, {geometry} paired by geometry; "
            f"{len(asked)} AI questions on {len({q.position for q in asked})} unpaired "
            f"dimensions ({tokens} input tokens), {len({pos for pos, _ in not_asked})} not asked; "
            f"about ${cost:.2f} a run"
        )
        why = Counter(reason for _pos, reason in not_asked)
        for reason, count in why.most_common():
            print(f"  not asked ({count}): {reason}")
        prepared[name] = (data, pages, pairings, pictures)
    print(
        f"estimate: about ${estimate:.2f} a run (up to ${high_estimate:.2f} if replies run long, "
        f"{likely}/{high} output tokens a call), ${estimate * args.runs:.2f} to "
        f"${high_estimate * args.runs:.2f} for {args.runs} run(s); suggested cap "
        f"${suggested_cap(estimate, args.runs):.2f}"
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
        found = ledger.stored.get((model, AI_PAIR_PROMPT_ID, identity or ""))
        return None if found is None else found[0]

    report: dict[str, Any] = {
        "runs": [],
        "estimate_usd": str(estimate),
        "high_usd": str(high_estimate),
    }
    passed = bool(args.allow_paid)
    for run in range(1, args.runs + 1):
        tally: Counter[str] = Counter()
        rows: dict[str, Any] = {}
        for name, (data, pages, pairings, pictures) in prepared.items():
            result = pair_with_ais(
                pages,
                pairings,
                pictures=pictures,
                clients=clients,
                document_version_id=f"aiset{name}",
                record_attempt=ledger.record,
                spend_guard=guard,
                stored=stored if run == 1 else None,
            )
            by_position = {(q.position, q.vendor_view.number): q for q in result.question_list}
            geometry_right = sum(
                verdicts.get(name, {}).get(_tag(p), {}).get("pair") == "right"
                for p in pairings
                if p.status is PairStatus.PAIRED
            )
            set_rows = []
            counts: Counter[str] = Counter()
            for position, p in enumerate(result.pairings):
                if p.paired_by != "two AIs":
                    continue
                verdict = grade(p, verdicts.get(name, {}), old.get(name, {}), eye.get(name, {}))
                counts[verdict] += 1
                chosen = by_position.get((position, p.vendor_view or 0))
                picture = None if chosen is None else str(_check_picture(out, name, chosen, p))
                set_rows.append(
                    {
                        "tag": _tag(p),
                        "architect": p.architect.text,
                        "axis": None if p.vendor is None else p.vendor.axis.value,
                        "vendor_view": p.vendor_view,
                        "vendor_inches": None if p.vendor_inches is None else str(p.vendor_inches),
                        "vendor_inches_reason": p.vendor_inches_reason,
                        "grade": verdict,
                        "key": _key_note(p, keys.get(name, {})),
                        "picture": picture,
                    }
                )
                print(f"run {run} set {name} {_tag(p)} {p.architect.text!r}: {verdict}")
            unpaired = Counter(
                p.reason.split("AI pair check: ", 1)[-1].split(":")[0][:60]
                for p in result.pairings
                if p.status is PairStatus.UNPAIRED and "AI pair check" in p.reason
            )
            right_total = geometry_right + counts["right"]
            if counts["wrong"] or counts["unchecked"] or right_total <= geometry_right:
                passed = False
            tally.update(counts)
            rows[name] = {
                "asked": result.asked,
                "stopped": result.stopped,
                "geometry_right": geometry_right,
                "ai_pairs": dict(counts),
                "unpaired_why": dict(unpaired.most_common()),
                "pairs": set_rows,
            }
            print(
                f"run {run} set {name}: asked {result.asked}; AI pairs {dict(counts)}; geometry "
                f"right {geometry_right}; stopped {result.stopped}"
            )
        report["runs"].append({"tally": dict(tally), "sets": rows})
    report["spent_usd"] = str(ledger.spent)
    report["calls"] = ledger.calls
    report["reused"] = ledger.reused
    report["pass"] = passed
    (out / "report.json").write_text(json.dumps(report, indent=1))
    print(
        f"paid calls {ledger.calls} (${ledger.spent:.4f}), reused {ledger.reused}; "
        + ("PASS" if passed else "not a pass" + ("" if args.allow_paid else " (no paid run)"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
