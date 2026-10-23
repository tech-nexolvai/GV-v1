"""The supervisor: a second, cheap look at a full check's plan before money is spent.

After the plan and code's own pairing are ready and before any paid step starts (the AI page
matcher and the comparing helpers), a full check:

1. makes the pre-flight report again from the pack (`preflight.py`, code, $0), with code's pairs
   as they stand now (the matcher's saved answers included);
2. asks ONE cheap model call (Haiku 5.5 by default, or Sonnet 5.5, low effort, no thinking; about
   $0.001 with Haiku, always under `MAX_COST_MICROS`) whether the plan makes sense, given the
   plan's facts as JSON (pages, pairs, pages with no architect page, the projects each side
   prints, the report's lines, the price). It answers `ok`, `warn` or `stop` and one plain
   sentence.

**What it may do.** It can only WARN or STOP. It never makes or changes a finding, a pair or a
verdict, and its words reach nobody but the reviewer. Its STOP counts only for the kinds the owner
allows a whole-check stop for (`preflight.STOP_KINDS`: files swapped, another project, a broken
file); any other stop is kept as a warning.

**Code's own stops need no AI**: the report's stop lines (a swapped or broken file, another
project's architect file, no vendor page with any architect page that could match it in a
two-file set). The wait for the reading is the engine's own (`ChatEngine._wait_for_reading`).

**A stop pauses the check**: nothing more is spent, the answer says why, and the screen offers
"Run anyway" (the same check with `run_anyway`) and "Cancel". With `run_anyway` the check goes
on and the stops are recorded as the reviewer's choice. Every stop and warning is kept in the
answer (its `preflight` block) and shown on the screen.

**Duplicate pages**: a vendor page that is an exact copy of an earlier page (`preflight`) is taken
out of the plan and noted "the same as page N: checked there".

The supervisor is behind a setting (`GV_ASSISTANT_PREFLIGHT_SUPERVISOR`, on by default); tests use
a fake call (`ask`), never a paid one.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Any, Final

from app.review.assistant.preflight import STOP_KINDS, PairEstimate, Report, build, gather
from app.review.assistant.reply_json import json_of_reply

if TYPE_CHECKING:
    from app.review.assistant.chat_engine import ChatEngine, MessageOutcome

__all__ = [
    "MAX_COST_MICROS",
    "MAX_TOKENS",
    "SYSTEM",
    "Gate",
    "Look",
    "decide",
    "duplicate_notes",
    "for_full_check",
    "gate",
    "look",
    "paused_outcome",
    "reply_schema",
]

_log = logging.getLogger(__name__)

#: The supervisor's call may cost at most this (millionths of $): it is not made when the
#: estimate passes it.
MAX_COST_MICROS: Final = 20_000
MAX_TOKENS: Final = 400
PROMPT_ID: Final = "review-assistant-chat-plan-supervisor-{provider}-{harness}"
_MAX_WHY: Final = 240
_KINDS: Final = (*STOP_KINDS, "other")
#: An unpaired page's reason that is final: code rules out every architect item, there is none,
#: or the AI page matcher's saved answer found none.
_CERTAIN: Final = re.compile(
    r"ruled out|no architect drawing to pair it with|drawing needed", re.IGNORECASE
)
#: An unpaired page the AI page matcher answered (it and code did not agree): not sure.
_ANSWERED: Final = re.compile(r"the AI page matcher", re.IGNORECASE)

SYSTEM: Final = """You supervise GV Review, a checker that compares a millwork vendor's shop \
drawings with the architect's drawings. Before any money is spent on comparing, you get the \
plan's facts as JSON (pages, how many vendor pages have an architect page, pages with none, the \
projects each file prints, what code found, the price).

Say whether this plan makes sense, in ONE short plain sentence for a person new to drawings \
(no internal words, no page ids like p1029, no AI words):
- "ok": it makes sense.
- "warn": it can run, but something looks off (say what).
- "stop": running it would waste the money: only when the architect's file looks like a vendor \
shop drawing (files_swapped), or like another project's drawings (other_project), or a file is \
broken or empty (broken_file).

You never judge any drawing, size or mistake: only whether the plan makes sense. Answer with the \
JSON form only."""


def reply_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["ok", "warn", "stop"]},
            "kind": {"type": "string", "enum": list(_KINDS)},
            "why": {"type": "string"},
        },
        "required": ["verdict", "kind", "why"],
        "additionalProperties": False,
    }


@dataclass(frozen=True, slots=True)
class Look:
    """The supervisor's answer, as code kept it."""

    verdict: str
    """`ok`, `warn`, `stop`, or `not_run` (off, no money for it, or its call failed)."""
    why: str | None = None
    kind: str = "other"
    cost_micros: int = 0

    def out(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "why": self.why,
            "by": "ai",
            "cost_usd": f"{self.cost_micros / 1_000_000:.4f}",
        }


#: One call: (system, the user's text, the answer form) -> (the reply's text, its cost in
#: millionths of $). Raises on a failed call.
type Call = Callable[[str, str, Mapping[str, Any]], tuple[str, int]]


def decide(text: str, cost_micros: int = 0) -> Look:
    """The reply in code's terms: a stop of a kind the owner does not allow is a warning; a reply
    that is not the form is `not_run` (the check goes on as planned)."""
    try:
        found = json_of_reply(text)
    except ValueError:
        found = None
    if not isinstance(found, Mapping):
        return Look("not_run", "The second look gave no usable answer.", cost_micros=cost_micros)
    verdict = str(found.get("verdict") or "")
    kind = str(found.get("kind") or "other")
    why = " ".join(str(found.get("why") or "").split())[:_MAX_WHY] or None
    if verdict not in ("ok", "warn", "stop"):
        return Look("not_run", "The second look gave no usable answer.", cost_micros=cost_micros)
    if verdict == "stop" and kind not in STOP_KINDS:
        verdict = "warn"
    return Look(verdict, why, kind if kind in _KINDS else "other", cost_micros)


def look(facts: Mapping[str, Any], call: Call) -> Look:
    """Asks the supervisor about `facts` (one call)."""
    text = "The plan's facts:\n" + json.dumps(facts, ensure_ascii=False, indent=1)
    try:
        reply, cost = call(SYSTEM, text, reply_schema())
    except Exception as failed:  # noqa: BLE001 - the check goes on as planned
        _log.warning("plan supervisor call failed: %s", type(failed).__name__)
        return Look("not_run", "The second look could not be made.")
    return decide(reply, cost)


@dataclass(frozen=True, slots=True)
class Gate:
    """What the gate decided: the report, the supervisor's look, whether the check pauses."""

    report: Report
    supervisor: Look
    paused: bool
    run_anyway: bool

    def block(self) -> dict[str, Any]:
        return {
            "type": "preflight",
            "preflight": {
                **self.report.out(),
                "state": "ready",
                "supervisor": self.supervisor.out(),
                "paused": self.paused,
                "run_anyway": self.run_anyway,
            },
        }

    def reasons(self) -> list[str]:
        """Why it stops, in plain words (code's lines first, then the supervisor's)."""
        out = [line.words for line in self.report.stops]
        if self.supervisor.verdict == "stop" and self.supervisor.why:
            out.append(self.supervisor.why)
        return out


def _engine_call(engine: ChatEngine, model_name: str) -> tuple[Call, Callable[[int], bool]]:
    """The supervisor's call through the engine's own path (charged to the answer, on the Usage
    page), and whether a call of so many prompt tokens fits the money and `MAX_COST_MICROS`."""
    from app.review.assistant.chat_engine import HARNESS_ID
    from app.review.assistant.chat_request import MODELS, thinking_config

    model = MODELS[model_name]
    prompt_id = PROMPT_ID.format(provider=engine.provider.name, harness=HARNESS_ID)

    def fits(tokens: int) -> bool:
        estimate = engine._price(model, tokens, MAX_TOKENS)
        return estimate is not None and estimate <= MAX_COST_MICROS and engine._fits(estimate)

    def call(system: str, text: str, schema: Mapping[str, Any]) -> tuple[str, int]:
        before = engine.spent
        reply = engine._call(
            model=model,
            effort="low",
            thinking=thinking_config(model, False),
            system=system,
            tools=(),
            messages=[{"role": "user", "content": [{"type": "text", "text": text}]}],
            final=False,
            max_tokens=MAX_TOKENS,
            prompt_id=prompt_id,
            display=None,
            output_format=schema,
        )
        return reply.text(), max(0, engine.spent - before)

    return call, fits


def _pairs_now(engine: ChatEngine, matching: Any) -> PairEstimate | None:
    """Code's pairs as the check holds them now (its own picks and the saved matcher answers),
    with the pages still waiting for the matcher."""
    cross = getattr(engine, "_cross", None)
    if cross is None:
        return None
    waiting = set(matching.to_answer) if matching is not None else set()
    paired = set(cross.pairs)
    vendor_pages = paired | set(cross.unpaired) | waiting
    if not vendor_pages:
        return None
    # Only a page code rules out entirely, or the matcher's saved answer left, is certain; any
    # other unpaired page may still pair (counted with the pages to ask).
    unsure = {
        page
        for page, why in cross.unpaired.items()
        if not _CERTAIN.search(str(why or "")) and page not in paired
    }
    answered = {
        page
        for page, why in cross.unpaired.items()
        if _ANSWERED.search(str(why or "")) and page not in paired and page not in waiting
    }
    unsure -= answered
    to_ask = (waiting | unsure) - paired
    return PairEstimate(
        vendor_pages=len(vendor_pages),
        paired=len(paired),
        to_ask=len(to_ask),
        none_possible=tuple(sorted(vendor_pages - paired - to_ask - answered)),
        unsure=tuple(sorted(answered)),
        confirmed=not waiting,
    )


def gate(
    engine: ChatEngine,
    plan: Any,
    matching: Any,
    *,
    model_name: str | None,
    run_anyway: bool,
    price_micros: int,
    texts: Mapping[tuple[str, str | int], str] | Callable[[], Mapping[Any, str]] | None = None,
) -> Gate | None:
    """The pre-flight report, code's stops and the supervisor's look, before any paid step; the
    `preflight` block added to the answer. `None` when the set has no pack (nothing to say)."""
    folder = engine.tools.workspace.set_pack
    if folder is None:
        return None
    main_file = engine.book.main_file
    listing = list(getattr(engine.tools, "uploads", None) or [])
    read_texts: Mapping[Any, str] = {}
    try:
        read_texts = dict((texts() if callable(texts) else texts) or {})
    except Exception as failed:  # noqa: BLE001 - the report is made without the printed words
        _log.warning("plan supervisor: page texts not read: %s", type(failed).__name__)
    grouped = None
    try:
        # The projects exactly as the pairing groups the set index (loaded by name: it reaches
        # the work list's own helpers).
        grouped = import_module("app.review.assistant.preflight_pairs").project_summary(folder)
    except Exception as failed:  # noqa: BLE001 - the report is made without the project lines
        _log.warning("plan supervisor: projects not grouped: %s", type(failed).__name__)
    inputs = gather(
        folder,
        listing=listing,
        main_file=main_file,
        texts=read_texts,
        projects=grouped,
        pairs=_pairs_now(engine, matching),
        skipped=tuple(getattr(plan, "skipped", ()) or ()),
        products=tuple(engine.settings.products or ()),
    )
    report = build(inputs)
    supervisor = Look("not_run", None)
    if model_name:
        call, fits = _engine_call(engine, model_name)
        facts = {**report.facts, "price_usd": f"{price_micros / 1_000_000:.2f}"}
        tokens = len(SYSTEM) // 3 + len(json.dumps(facts)) // 3 + 200
        if fits(tokens):
            with engine.timings.phase("planning"):
                supervisor = look(facts, call)
        else:
            supervisor = Look("not_run", "The second look was skipped: no money left for it.")
    stops = bool(report.stops) or supervisor.verdict == "stop"
    found = Gate(report, supervisor, paused=stops and not run_anyway, run_anyway=run_anyway)
    block = found.block()
    index = engine._add_block(block)
    engine.emit(
        "preflight", {"index": index, "preflight": json.loads(json.dumps(block["preflight"]))}
    )
    return found


def paused_outcome(engine: ChatEngine, found: Gate) -> MessageOutcome:
    """The stored answer of a check that paused before spending: why, in plain words."""
    from app.review.assistant.chat_engine import MessageOutcome

    reasons = found.reasons()
    text = (
        "Paused before comparing anything: "
        + " ".join(reasons)
        + " Nothing was compared. Fix the upload, or choose Run anyway."
    )
    index = engine._add_block({"type": "text", "text": text, "by": "code"})
    engine.emit("text_delta", {"index": index, "text": text, "by": "code"})
    return MessageOutcome(
        text=text,
        blocks=engine.stored_blocks(),
        api_content=[],
        cost_micros=engine.spent,
        stopped="cancelled",
    )


def duplicate_notes(found: Gate, pages: Sequence[int]) -> list[tuple[int, str]]:
    """The planned pages that are exact copies of an earlier page, with their coverage note."""
    planned = set(pages)
    return [
        (page, f"the same as page {first}: checked there (an exact copy)")
        for page, first in sorted(found.report.duplicates.items())
        if page in planned and first in planned and first != page
    ]


def for_full_check(engine: ChatEngine, plan: Any, matching: Any) -> Gate | None:
    """The gate as a roster full check runs it (`full_check_roster`): the supervisor's model and
    Run anyway from the engine's settings, the price the check will hold, the printed words of
    the set's pages (kept in the pack). Never stops the check by failing: `None` then."""
    from app.review.assistant.full_check_roster import price_bound
    from app.review.assistant.vendor_consistency import page_texts

    settings = engine.settings
    folder = engine.tools.workspace.set_pack
    try:
        page_map = (
            json.loads((folder / "page_map.json").read_text(encoding="utf-8")) if folder else {}
        )
        files = [str(name) for name in (page_map or {}).get("files") or [] if name]
        price = settings.price_rule.price(getattr(plan, "pages", 0), price_bound(engine))
        return gate(
            engine,
            plan,
            matching,
            model_name=getattr(settings, "preflight_model", None),
            run_anyway=bool(getattr(settings, "run_anyway", False)),
            price_micros=price,
            texts=(lambda: page_texts(folder, files)) if folder is not None and files else None,
        )
    except Exception:  # noqa: BLE001 - the check goes on as planned, without the report
        _log.exception("plan supervisor: the pre-flight gate failed")
        return None
