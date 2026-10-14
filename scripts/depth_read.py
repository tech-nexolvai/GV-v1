#!/usr/bin/env python3
"""Run the depth and sink reader (#1265) on PDFs, and write its answers for Pariksha to score.

    .venv/bin/python scripts/depth_read.py --pdf <set.pdf> [--pdf <other.pdf> ...] \\
        [--vendor-file <set.pdf>] [--key <akshara-depth key.json> --key <rekha-view key.json>] \\
        --out ~/gv-local/<folder> [--allow-paid --cap-usd 4]

**No model is called unless `--allow-paid --cap-usd X` is given.** Without them the script does
everything code does at no cost (views, spans, pictures, every question it would ask) and says how
many questions a paid run would send; nothing is sent. With them, at most X US dollars is spent in
this run: the shared spend guard reserves each call's worst case before sending it, and the reading
stops cleanly at the cap.

**Nothing is paid twice.** Every attempt is appended to `<out>/calls.jsonl` with its raw reply and
its question packet. On a later run, a question whose identity (model, prompt id and everything the
question shows, `workflow/reader_reuse.question_identity`) matches an accepted stored answer is
replayed through the same reading code instead of asked again.

**The two readers** are the decided Claude pair, through Anthropic's own API (phase 3: "do it with
anthropic for now testing"), keyed by `ANTHROPIC_API_KEY` from the environment or `--env-file`. The
calls are priced from `--rates` (the repository's price file by default).

**What it writes** (all under `--out`, which must be outside any git working tree: the readings hold
a client's drawing values):

* `readings.json`: every view, code's and the readers' judgments, every value with its reason;
* with `--key` files (`pariksha-key/1`, agents `rekha-view` and `akshara-depth`): two
  `pariksha-depth-answers/1` files per agent. `answers-<agent>-final.json` is what the reader
  hands on: a value only where both judgments agree; where the two AIs agreed but code held, both
  readers' entries are holds with code's reason. `answers-<agent>-ai.json` is the two AIs' own
  answers before code's check. **The bar (0 agreed-wrong) is the final file's.** The key only
  names the answers (which view and value a key question is about); it never feeds a reading.

Read-only on every database: it reads PDFs only. Exit codes: 0 done, 2 refused.

Source: issue #1265 · Verification: `tests/scripts/test_depth_read.py`
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from extraction.depth.read import (
    CLAUDE_PAIR,
    CT008,
    CT012,
    MEASURED_DEPTH_READER_SETTINGS,
    Answers,
    Asked,
    DepthReading,
    NoAnswer,
    ReaderSays,
    ValueReading,
    ViewReading,
    prepare_pdf_page,
    read_depth,
)
from extraction.depth.view_kind import ViewKind
from extraction.form_reader.bedrock import AttemptUsage
from extraction.geometry.rows import Box

REFUSED = 2
DEFAULT_RATES = REPO / "deploy" / "model_rates.us-east-1.json"
ANSWERS_FORMAT = "pariksha-depth-answers/1"
VIEW_AGENT = "rekha-view"
DEPTH_AGENT = "akshara-depth"
#: The scorer's kind words for the readers' (`eval/pariksha/depth.py`).
_KIND_WORD = {
    "front": "elevation",
    "plan": "plan",
    "section_or_side": "section_or_side",
    "detail": "detail",
    "other": "other",
}
_KEY_VIEW = {"plan": ViewKind.PLAN, "section": ViewKind.SECTION, "side": ViewKind.SECTION}


class Refused(Exception):
    """The script will not do what was asked; the message says why."""


# --- the local ledger: every attempt, and the stored answers it offers for reuse -------------


class Ledger:
    """Appends every attempt to `calls.jsonl` (thread-safe) and offers accepted stored answers."""

    def __init__(self, path: Path, rates: Any | None) -> None:
        self.path = path
        self.rates = rates
        self.lock = threading.Lock()
        self.spent = Decimal(0)
        self.calls = 0
        self.reused = 0
        self.stored: dict[tuple[str, str, str], tuple[str, str]] = {}
        if path.exists():
            from workflow.reader_reuse import question_identity

            for line in path.read_text().splitlines():
                row = json.loads(line)
                packet = row.get("question_packet")
                identity = question_identity(packet) if isinstance(packet, dict) else None
                if (
                    identity is None
                    or row.get("malformed")
                    or row.get("failure_kind") is not None
                    or row.get("reused_from") is not None
                    or not isinstance(row.get("raw_response_text"), str)
                ):
                    continue
                self.stored[(row["model_id"], row["prompt_id"], identity)] = (
                    row["raw_response_text"],
                    row["id"],
                )

    def cost(self, attempt: AttemptUsage) -> Decimal | None:
        if attempt.input_tokens is None or attempt.output_tokens is None or self.rates is None:
            return None
        rate = self.rates.rate_for(attempt.model_id)
        if rate is None:
            return None
        return (
            Decimal(attempt.input_tokens) * rate.input_per_1k_tokens
            + Decimal(attempt.output_tokens) * rate.output_per_1k_tokens
        ) / 1000

    def record(self, attempt: AttemptUsage) -> None:
        cost = None if attempt.reused_from is not None else self.cost(attempt)
        row = {
            "id": str(uuid.uuid4()),
            **{name: value for name, value in asdict(attempt).items() if name != "question_packet"},
            "question_packet": (
                None if attempt.question_packet is None else dict(attempt.question_packet)
            ),
            "cost_usd": None if cost is None else str(cost),
        }
        with self.lock:
            if attempt.reused_from is None:
                self.calls += 1
                self.spent += cost or Decimal(0)
            else:
                self.reused += 1
            with self.path.open("a") as out:
                out.write(json.dumps(row, default=str) + "\n")


def make_asker(
    ledger: Ledger,
    *,
    allow_paid: bool,
    paid: Mapping[str, Any] | None,
    max_tokens: int,
) -> Any:
    """The reader's `ask`: a stored answer first, then (only when allowed) the shared route."""
    from extraction.slot_reader.bedrock import CropJob, read_crops_parallel, replay_stored_answer
    from workflow.reader_reuse import question_identity

    not_sent = [0]

    def ask(questions: Sequence[Asked]) -> Answers:
        answers: dict[tuple[str, str], Any] = {}
        live: list[CropJob] = []
        for asked in questions:
            job = CropJob(
                asked.key,
                asked.model_id,
                asked.page_index,
                asked.question.pictures[0],
                question_packet=asked.packet,
                depth=asked.question,
            )
            identity = question_identity(asked.packet)
            stored = (
                None
                if identity is None
                else ledger.stored.get((asked.model_id, asked.question.prompt_id, identity))
            )
            if stored is not None:
                replayed = replay_stored_answer(
                    job, stored[0], reused_from=stored[1], max_tokens=max_tokens
                )
                if replayed is not None:
                    answer, attempt = replayed
                    ledger.record(attempt)
                    answers[(asked.key, asked.model_id)] = answer
                    continue
            live.append(job)
        if not live:
            return answers
        if not allow_paid or paid is None:
            not_sent[0] += len(live)
            for job in live:
                answers[(job.key, job.model_id)] = NoAnswer(
                    "not asked: this run allows no paid calls (--allow-paid --cap-usd X)"
                )
            return answers
        got = read_crops_parallel(live, record_attempt=ledger.record, **paid)
        stop = paid["stop"]
        for job in live:
            answer = got.get((job.key, job.model_id))
            answers[(job.key, job.model_id)] = (
                answer
                if answer is not None
                else NoAnswer(
                    "no accepted answer: "
                    + (
                        f"the reading stopped ({stop.cause}) before or while asking"
                        if stop.stopped
                        else "the reader abstained (malformed twice, or the call failed)"
                    )
                )
            )
        return answers

    ask.not_sent = not_sent  # type: ignore[attr-defined]
    return ask


# --- answers for Pariksha ------------------------------------------------------------------


def _reader_entry(says: ReaderSays, *, view: bool) -> dict[str, Any]:
    entry: dict[str, Any] = {"model_id": says.model_id}
    if says.says == "kind":
        entry["kind"] = _KIND_WORD[says.text]
    elif says.says == "value" and not view:
        entry["value"] = says.text
        if says.start_edge is not None:
            entry["start_edge"] = says.start_edge
        if says.outline is not None:
            entry["outline"] = says.outline
    elif says.says == "unsure":
        entry["unsure"] = True
    elif says.says == "held":
        entry["held"] = says.text or "held"
    else:
        entry["invalid"] = says.text or "no accepted answer"
    return entry


def _same_value(readers: Sequence[ReaderSays]) -> bool:
    from eval.pariksha.depth import inches

    values = [inches(says.text) for says in readers if says.says == "value"]
    return len(values) == len(readers) and None not in values and len(set(values)) == 1


def value_entries(value: ValueReading) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The two AIs' own answers, and what the reader hands on (a hold where code held)."""
    ai = [_reader_entry(says, view=False) for says in value.readers]
    if value.usable:
        final = [
            {
                "model_id": says.model_id,
                "value": value.text,
                **({"start_edge": value.start_edge} if value.start_edge else {}),
                **({"outline": value.outline} if value.outline else {}),
            }
            for says in value.readers
        ]
    elif _same_value(value.readers):
        final = [{"model_id": says.model_id, "held": value.reason} for says in value.readers]
    else:
        final = ai
    return ai, final


def kind_entries(view: ViewReading) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ai = [_reader_entry(says, view=True) for says in view.kinds]
    agreed = {says.text for says in view.kinds if says.says == "kind"}
    if view.kind is None and len(agreed) == 1 and all(s.says == "kind" for s in view.kinds):
        return ai, [{"model_id": says.model_id, "held": view.kind_reason} for says in view.kinds]
    return ai, ai


def _containing(views: Sequence[ViewReading], region: Sequence[float]) -> ViewReading | None:
    x = Decimal(str((region[0] + region[2]) / 2))
    y = Decimal(str((region[1] + region[3]) / 2))
    holding = [
        view
        for view in views
        if view.extent.x0 <= x <= view.extent.x1 and view.extent.top <= y <= view.extent.bottom
    ]
    if not holding:
        return None
    return min(holding, key=lambda view: view.extent.width * view.extent.height)


def answers_for_keys(
    reading: DepthReading, keys: Sequence[Mapping[str, Any]], run: str
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Each key's answers files (`<agent>-ai`, `<agent>-final`), and every key value not answered."""
    by_page: dict[str, list[ViewReading]] = {}
    for view in reading.views:
        by_page.setdefault(view.page_sha256, []).append(view)
    regions: dict[tuple[str, int], list[float]] = {}
    for key in keys:
        if key.get("agent") == VIEW_AGENT:
            for case in key["cases"]:
                if case.get("region_pt") is not None:
                    regions[(case["page_sha256"], case["statement_id"])] = case["region_pt"]
    files: dict[str, dict[str, Any]] = {}
    missed: list[str] = []
    for key in keys:
        agent = key.get("agent")
        ai_entries: list[dict[str, Any]] = []
        final_entries: list[dict[str, Any]] = []
        for case in key["cases"]:
            page = case["page_sha256"]
            views = by_page.get(page, [])
            where = f"{case.get('set')} p{case.get('page')} {case['question_id']}"
            region = case.get("region_pt") or regions.get((page, case["statement_id"]))
            found = _containing(views, region) if region is not None else None
            if found is None and agent == DEPTH_AGENT:
                kind = _KEY_VIEW.get(str(case.get("view")))
                candidates = [view for view in views if (view.kind or view.code_kind.kind) is kind]
                found = candidates[0] if len(candidates) == 1 else None
            if found is None:
                missed.append(f"{where}: no view the reader read is at this place")
                continue
            if agent == VIEW_AGENT:
                ai, final = kind_entries(found)
                identity = {"page_sha256": page, "question_id": case["question_id"]}
                ai_entries.append({**identity, "field": "view", "readers": ai})
                final_entries.append({**identity, "field": "view", "readers": final})
                continue
            fields = (CT012, CT008) if case["field"] == f"{CT012}+{CT008}" else (case["field"],)
            for field_name in fields:
                value = next((v for v in found.values if v.field == field_name), None)
                if value is None:
                    reason = found.values_reason or (
                        f"{field_name} is not read on a {found.kind.value if found.kind else '?'}"
                    )
                    held = [{"model_id": model, "held": reason} for model in CLAUDE_PAIR]
                    ai, final = held, held
                else:
                    ai, final = value_entries(value)
                identity = {"page_sha256": page, "question_id": case["question_id"]}
                ai_entries.append({**identity, "field": field_name, "readers": ai})
                final_entries.append({**identity, "field": field_name, "readers": final})
        prompts = ["view-kind-v1"] + (
            ["depth-span-role-v1", "claude-depth-span-v1"] if agent == DEPTH_AGENT else []
        )
        for kind, entries in (("ai", ai_entries), ("final", final_entries)):
            if entries:
                files[f"{agent}-{kind}"] = {
                    "format": ANSWERS_FORMAT,
                    "agent": agent,
                    "run": f"{run} ({kind})",
                    "prompt_ids": prompts,
                    "answers": entries,
                }
    return files, missed


# --- readings, for a person ---------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if isinstance(value, Fraction):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Box):
        return [str(value.x0), str(value.top), str(value.x1), str(value.bottom)]
    if isinstance(value, ViewKind):
        return value.value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value


def reading_rows(reading: DepthReading) -> list[dict[str, Any]]:
    rows = []
    for view in reading.views:
        rows.append(
            _jsonable(
                {
                    "page": view.page_index + 1,
                    "page_sha256": view.page_sha256,
                    "panel": view.panel,
                    "view": view.view_number,
                    "extent_pt": view.extent,
                    "heading": view.heading,
                    "code_kind": view.code_kind.kind,
                    "code_reason": view.code_kind.reason,
                    "readers_kind": [asdict(says) for says in view.kinds],
                    "kind": view.kind,
                    "kind_reason": view.kind_reason,
                    "values_reason": view.values_reason,
                    "values": [asdict(value) for value in view.values],
                }
            )
        )
    return rows


# --- main ---------------------------------------------------------------------------------


def _env_key(env_file: Path | None) -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    if env_file is None or not env_file.exists():
        return None
    for line in env_file.read_text().splitlines():
        name, _, value = line.strip().partition("=")
        if name.strip() == "ANTHROPIC_API_KEY" and value.strip():
            return value.strip().strip('"').strip("'")
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--pdf", type=Path, action="append", required=True)
    parser.add_argument("--vendor-file", type=Path, action="append", default=[])
    parser.add_argument("--pages", type=int, nargs="*", help="1-based pages (one PDF only)")
    parser.add_argument("--key", type=Path, action="append", default=[])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--cap-usd", type=Decimal)
    parser.add_argument("--rates", type=Path, default=DEFAULT_RATES)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--rpm", type=int, default=30, help="calls per minute per reader")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--run", default="depth-reader measuring run")
    args = parser.parse_args(argv)

    from scripts.depth_dry_run import Refused as DryRefused
    from scripts.depth_dry_run import outside_git

    try:
        out = outside_git(args.out)
        if args.allow_paid and (args.cap_usd is None or args.cap_usd <= 0):
            raise Refused("--allow-paid needs a spend cap: --cap-usd X (more than 0)")
        if args.cap_usd is not None and not args.allow_paid:
            raise Refused("--cap-usd is only for a paid run: add --allow-paid")
        if args.pages and len(args.pdf) != 1:
            raise Refused("--pages is for one PDF only")
    except (Refused, DryRefused) as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return REFUSED

    from app.runs.rates import load_model_rates
    from eval.pariksha.depth import load_depth_key
    from extraction.reader import read_pages

    keys = []
    for path in args.key:
        data = json.loads(path.read_text())
        load_depth_key(data)  # refuses a key it does not know
        keys.append(data)
    keyed_pages = {case["page_sha256"] for key in keys for case in key["cases"]}
    rates = load_model_rates(args.rates)
    out.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(out / "calls.jsonl", rates)
    settings = MEASURED_DEPTH_READER_SETTINGS
    vendor_files = {path.resolve() for path in args.vendor_file}

    pages = []
    for pdf in args.pdf:
        data = pdf.read_bytes()
        document = hashlib.sha256(data).hexdigest()
        raw = read_pages(data)
        hashes = [hashlib.sha256(page.content).hexdigest() for page in raw]
        if args.pages:
            wanted = [number - 1 for number in args.pages]
        elif keys:
            wanted = [index for index, digest in enumerate(hashes) if digest in keyed_pages]
        else:
            wanted = list(range(len(raw)))
        for index in wanted:
            page = prepare_pdf_page(
                data,
                index,
                page_sha256=hashes[index],
                document_version_id=document,
                vendor_file=pdf.resolve() in vendor_files,
                settings=settings,
            )
            pages.append(page)
            print(f"{pdf.name} p{index + 1}: {len(page.views)} view(s)", flush=True)
            for note in page.notes:
                print(f"  note: {note}")

    paid: dict[str, Any] | None = None
    if args.allow_paid:
        from extraction.slot_reader.anthropic import BatchSpendGuard, ThreadLocalAnthropicClients
        from extraction.slot_reader.stop import ReadingStop

        api_key = _env_key(args.env_file)
        if api_key is None:
            print(
                "refused: ANTHROPIC_API_KEY is not set (environment or --env-file)", file=sys.stderr
            )
            return REFUSED
        paid = {
            "clients": ThreadLocalAnthropicClients(api_key, timeout_seconds=180),
            "rates": rates,
            "calls_per_minute": dict.fromkeys(CLAUDE_PAIR, args.rpm),
            "max_concurrent_calls": args.concurrency,
            "max_tokens": settings.max_tokens,
            "max_throttle_retries": 3,
            "retry_backoff_seconds": 2.0,
            "spend_guard": BatchSpendGuard(args.cap_usd, rates),
            "stop": ReadingStop(),
            "claude_effort": settings.effort,
        }
    ask = make_asker(ledger, allow_paid=args.allow_paid, paid=paid, max_tokens=settings.max_tokens)
    reading = read_depth(pages, ask=ask, settings=settings)
    (out / "readings.json").write_text(json.dumps(reading_rows(reading), indent=1))

    usable = sum(value.usable for view in reading.views for value in view.values)
    print(
        f"views {len(reading.views)}; kinds settled "
        f"{sum(view.kind is not None for view in reading.views)}; values usable {usable}"
    )
    print(
        f"questions {reading.questions_asked}: paid calls {ledger.calls} (${ledger.spent:.4f}), "
        f"reused {ledger.reused}, not sent {ask.not_sent[0]}"
        + (f"; cap ${args.cap_usd}" if args.allow_paid else " (no paid calls allowed)")
    )
    if keys:
        files, missed = answers_for_keys(reading, keys, args.run)
        for name, document_ in files.items():
            (out / f"answers-{name}.json").write_text(json.dumps(document_, indent=1))
            print(f"wrote answers-{name}.json ({len(document_['answers'])} answers)")
        for line in missed:
            print(f"  not answered: {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
