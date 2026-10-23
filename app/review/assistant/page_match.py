"""The AI page matcher (#1798): which architect item a vendor drawing of a separate upload shows.

Real jobs come as two PDFs, the architect's and the vendor's. A vendor drawing with no architect
drawing of its own area (a plain CAD file, or a page of the vendor's file whose item the
architect's file holds) must be paired with the architect's drawing of the same piece before
anything can be compared. Code ranks the candidates by itself (`simple_check.pairing.rank`: GV's
typed titles, the divider pages' sections, references, tags, words, sizes), but printed values
rarely agree (that is what the check looks for), so code alone pairs only a few.

So one cheap model call per vendor drawing (Sonnet 5.5, thinking off, low effort) is shown the
vendor drawing and a labelled picture of every architect item code does not rule out (`A1`, `A2`,
... in page order, with GV's typed title and section), and answers in a closed form: the ids of
the items that show the same piece (or none), whether it is sure, and one short reason. Ids only;
no values, no verdicts. A route without structured outputs (OpenRouter) may fence its JSON: that
is read too, and an answer that is still not the form gets ONE repair turn.

**The agreement rule** (`pairing.agree`, applied by the work list): a pair is made when the
matcher's pick is code's own clear pick; or when the matcher is sure AND code either holds strong
evidence for the pick (its title on the vendor's sheet, a view reference, an item tag only it
prints...) or ranks it first on weak evidence with a clear lead. A sure pick among look-alikes
code cannot tell apart is never paired. Zero wrong pairs matters more than coverage: a wrong pair
can make false reds.

**Saved answers.** Every answer is kept in the set pack (`page_match.json`, with the candidates
it was shown, the model, the usage and the cost), so the work list is built again at $0 (a
replay, another chat on the same set, the roster's planning after the simple check's); a drawing
whose candidates changed is asked again, unless code only ruled some out and the answer's picks
are among the rest (`pairing.reuse`).

**The set index first** (`set_index.py`, `prepare_run`): each page's project, set and views, read
once by AI and checked by code, so the matcher is shown only the architect's pages of the vendor
drawing's own project, never a reference set (the brand prototype) nor a key plan.

The architect items' pictures are the same bytes for every vendor drawing shown the same items,
so they carry a prompt-cache breakpoint: the first call of each group writes them, the others
read them at a tenth of the price.

Loads `set_pack` by name (it draws with pypdfium2 and Pillow, which `app/api/` must not reach by
import).
"""

from __future__ import annotations

import base64
import io
import json
import logging
import math
import os
import re
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

from app.review.assistant.full_check_plan import CrossPairs
from app.review.assistant.reply_json import json_of_reply
from app.review.assistant.simple_check.pairing import MatchAnswer, reuse
from app.review.assistant.simple_check.schema import AcrossCandidate, AcrossPair, Drawing
from app.review.assistant.simple_check.worklist import MATCH_FILE

if TYPE_CHECKING:
    from app.review.assistant.chat_engine import ChatEngine

__all__ = [
    "MATCH_FILE",
    "PROMPT_ID",
    "PROMPT_VERSION",
    "SYSTEM",
    "AskReply",
    "MatchReport",
    "MatchRun",
    "PictureMaker",
    "Pictures",
    "engine_asker",
    "load_answers",
    "match_pairs",
    "parse",
    "prepare_run",
    "question",
    "reply_schema",
    "run_for_engine",
    "to_ask",
    "wait_ready",
]

_log = logging.getLogger(__name__)

#: Changes when the question changes: an answer to an older question is asked again.
PROMPT_VERSION: Final = 1
#: The call's prompt id on the Usage page.
PROMPT_ID: Final = "review-assistant-chat-page-match-{provider}-{harness}"
#: Each architect item's picture, and the vendor drawing's (visual tokens, `ceil(w/28) x ceil(h/28)`).
ITEM_TOKENS: Final = 1_100
VENDOR_TOKENS: Final = 3_200
#: The reply's room (thinking off: the form and one sentence).
MAX_TOKENS: Final = 1_200
#: The request's text (the system prompt, captions, the question) held for the runaway guard.
_TEXT_TOKENS: Final = 2_500
#: Calls at once (#1883: was 4; one call takes about 40 s, so 96 drawings took 17 minutes). A group's
#: first call still runs before the others of its group (it writes the pictures' cache entry), and
#: each call is still asked only when it fits the run's money (`fits`).
PARALLEL: Final = 8
_MAX_REASON: Final = 200
_DPI_RANGE: Final = (18, 200)
_LABEL: Final = re.compile(r"^A\d{1,3}$")

SYSTEM: Final = """You match millwork drawings for GV Review, a shop-drawing checker.

You are shown ONE vendor shop drawing and pictures of the architect's items, each labelled A1, A2, \
A3 and so on, with the title GV typed for it and its section when there is one. Say which \
architect item or items show the SAME piece of millwork as the vendor drawing: the same unit, \
run or counter of the same room, drawn as a plan, an elevation or a section.

- Sizes may differ: the vendor's mistakes are what the review looks for. Never pair on equal \
numbers alone. Look at the shape and outline, how many cabinets, doors, drawers and shelves there \
are and how they are arranged, the appliances, sinks and openings, the walls or ends it sits \
between, and the titles, room names and tags printed on both.
- Name several items only when each of them shows that same piece (a plan and an elevation of it).
- Answer an empty list when no picture shows the same piece. Never guess.
- confidence is "high" only when you are sure AND no other picture is a close look-alike of the \
one you picked; otherwise "low".
- reason: one short sentence naming what matches (or why none does). No measurements.

Answer with the JSON form only, using only the labels you were given."""


# ---- the form ----------------------------------------------------------------------------------


def reply_schema(labels: Sequence[str]) -> dict[str, Any]:
    """The closed answer form for these labels: ids only."""
    return {
        "type": "object",
        "properties": {
            "matches": {"type": "array", "items": {"type": "string", "enum": list(labels)}},
            "confidence": {"type": "string", "enum": ["high", "low"]},
            "reason": {"type": "string"},
        },
        "required": ["matches", "confidence", "reason"],
        "additionalProperties": False,
    }


def _json_of(text: str) -> object:
    """The answer's JSON (structured outputs return it bare; a route without them may fence it or
    put a sentence first): the shared reader (`reply_json`, #1811)."""
    return json_of_reply(text)


def parse(text: str, labels: Sequence[str]) -> tuple[MatchAnswer | None, str | None]:
    """The matcher's answer (labels, not ids yet), or why it is not the form."""
    try:
        document = _json_of(text)
    except ValueError:
        return None, "the answer is not JSON"
    if not isinstance(document, Mapping):
        return None, "the answer is not a JSON object"
    extra = set(document) - {"matches", "confidence", "reason"}
    if extra:
        return None, f"the answer has fields the form does not have: {', '.join(sorted(extra))}"
    matches = document.get("matches")
    if not isinstance(matches, list) or not all(isinstance(m, str) for m in matches):
        return None, "matches must be a list of labels"
    known = set(labels)
    unknown = [m for m in matches if m not in known]
    if unknown:
        return None, f"{', '.join(unknown)} is not one of the labels {', '.join(labels)}"
    confidence = document.get("confidence")
    if confidence not in ("high", "low"):
        return None, 'confidence must be "high" or "low"'
    reason = document.get("reason")
    if not isinstance(reason, str):
        return None, "reason must be a short sentence"
    return (
        MatchAnswer(
            matches=tuple(dict.fromkeys(matches)),
            confident=confidence == "high",
            reason=" ".join(reason.split())[:_MAX_REASON],
        ),
        None,
    )


def _repair_text(problem: str, labels: Sequence[str]) -> str:
    return (
        f"That answer could not be used: {problem}. Answer again with the JSON form only: "
        '{"matches": [labels from ' + ", ".join(labels) + '], "confidence": "high" or "low", '
        '"reason": "one short sentence"}.'
    )


# ---- the pictures ------------------------------------------------------------------------------


def _union(boxes: Sequence[Sequence[float]]) -> tuple[float, float, float, float]:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _dpi_for(box: Sequence[float], tokens: int) -> int:
    width = max(1.0, (box[2] - box[0]) / 72)
    height = max(1.0, (box[3] - box[1]) / 72)
    dpi = math.sqrt(tokens * 28 * 28 / (width * height))
    return int(max(_DPI_RANGE[0], min(_DPI_RANGE[1], dpi)))


class PictureMaker(Protocol):
    """What draws the matcher's pictures (`Pictures`; a fake in tests)."""

    def picture(self, drawings: Sequence[Drawing], tokens: int) -> tuple[bytes, int, int]: ...


class Pictures:
    """Draws the matcher's pictures from the set pack's drawings-only copies (GV's markup
    hidden), through the pack's hi-res page cache; each picture once."""

    def __init__(self, folder: Path, uploads: Path) -> None:
        self.folder = folder
        self.uploads = uploads
        self._pack = import_module("app.review.assistant.set_pack")
        self._sizes = import_module("app.review.assistant.simple_check.compare")
        self._lock = threading.Lock()
        self._made: dict[tuple[str, int, tuple[float, ...], int], tuple[bytes, int, int]] = {}

    def picture(self, drawings: Sequence[Drawing], tokens: int) -> tuple[bytes, int, int]:
        """One picture of these drawings (of one page): PNG bytes, width, height."""
        first = drawings[0]
        box = _union([d.crop_box for d in drawings])
        key = (first.file, first.own_page, tuple(round(v, 1) for v in box), tokens)
        with self._lock:
            if key in self._made:
                return self._made[key]
        from PIL import Image

        pdf = self._pack.drawings_only(self.folder, self.uploads, first.file)
        crop = self._pack.crop_picture(
            pdf, first.own_page, box, _dpi_for(box, tokens), cache=self.folder / self._pack.HIRES
        )
        png, width, height = crop.png, crop.width, crop.height
        target = self._sizes.resized_size(width, height, tokens)
        if target != (width, height):
            with Image.open(io.BytesIO(png)) as opened:
                shrunk = opened.convert("RGB").resize(target, Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            shrunk.save(buffer, format="PNG", compress_level=3)
            png, (width, height) = buffer.getvalue(), target
        made = (png, width, height)
        with self._lock:
            self._made[key] = made
        return made


def _image(png: bytes) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(png).decode("ascii"),
        },
    }


def _labels(pair: AcrossPair) -> dict[str, AcrossCandidate]:
    """The items the matcher is shown, labelled A1, A2... in page order."""
    shown = [c for c in pair.candidates if c.id in set(pair.asked)]
    shown.sort(key=lambda c: (c.file, c.page, c.id))
    return {f"A{n}": item for n, item in enumerate(shown, start=1)}


def _item_caption(label: str, item: AcrossCandidate) -> str:
    words = f"Architect item {label}: the architect's file, page {item.page}"
    if item.title:
        words += f'; GV\'s title "{item.title}"'
    if item.section:
        words += f'; section "{item.section}"'
    return words + "."


def question(
    pair: AcrossPair, labels: Mapping[str, AcrossCandidate], pictures: PictureMaker
) -> tuple[list[dict[str, Any]], int]:
    """The user message's content and its picture tokens: every architect item's picture after
    its caption (a cache breakpoint after the last: the same bytes for every vendor drawing
    shown these items), then the vendor drawing, then the question."""
    content: list[dict[str, Any]] = [
        {"type": "text", "text": f"The architect's items ({len(labels)}):"}
    ]
    tokens = 0
    for label, item in labels.items():
        png, width, height = pictures.picture(item.drawings, ITEM_TOKENS)
        tokens += math.ceil(width / 28) * math.ceil(height / 28)
        content.append({"type": "text", "text": _item_caption(label, item)})
        content.append(_image(png))
    content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
    png, width, height = pictures.picture((pair.vendor,), VENDOR_TOKENS)
    tokens += math.ceil(width / 28) * math.ceil(height / 28)
    where = f"the vendor's file, page {pair.vendor.own_page}"
    if pair.section:
        where += f', section "{pair.section}"'
    content.append({"type": "text", "text": f"The vendor drawing ({where}):"})
    content.append(_image(png))
    content.append(
        {
            "type": "text",
            "text": (
                "Which architect item or items show the same piece of millwork as this vendor "
                f"drawing? Labels: {', '.join(labels)}. Answer with the JSON form only."
            ),
        }
    )
    return content, tokens


# ---- asking ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AskReply:
    """One call's reply: its text, why it stopped, its usage, the model that answered, its cost."""

    text: str
    stop_reason: str | None
    usage: Mapping[str, Any]
    model: str
    cost_usd: float = 0.0


#: One call: (messages, the answer form) -> the reply. Raises on a failed call.
type Asker = Callable[[list[dict[str, Any]], Mapping[str, Any]], AskReply]


@dataclass(slots=True)
class MatchReport:
    """What one matcher run did."""

    asked: int = 0
    answered: int = 0
    reused: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    """(vendor drawing, why) for each drawing with no answer."""
    cost_usd: float = 0.0
    skipped_for_money: int = 0


def _add_usage(total: dict[str, int], usage: Mapping[str, Any]) -> None:
    for key, value in usage.items():
        if isinstance(value, int) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value


def ask_one(
    pair: AcrossPair, ask: Asker, pictures: PictureMaker
) -> tuple[dict[str, Any] | None, str, float]:
    """One vendor drawing's question, with its one repair turn: the entry to save (ids, not
    labels), or `None` and why; and what it cost."""
    labels = _labels(pair)
    if not labels:
        return None, "code rules out every architect item", 0.0
    content, _tokens = question(pair, labels, pictures)
    schema = reply_schema(list(labels))
    messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
    usage: dict[str, int] = {}
    cost = 0.0
    repaired = False
    model = ""
    while True:
        reply = ask(messages, schema)
        _add_usage(usage, reply.usage)
        cost += reply.cost_usd
        model = reply.model
        if reply.stop_reason == "refusal":
            return None, "the model declined", cost
        if reply.stop_reason == "max_tokens":
            problem: str | None = "the answer was cut off"
            answer = None
        else:
            answer, problem = parse(reply.text, list(labels))
        if answer is not None:
            break
        if repaired:
            return None, f"the answer was not the form after one repair turn ({problem})", cost
        repaired = True
        messages = [
            messages[0],
            {"role": "assistant", "content": [{"type": "text", "text": reply.text or "{}"}]},
            {
                "role": "user",
                "content": [{"type": "text", "text": _repair_text(str(problem), list(labels))}],
            },
        ]
    return (
        {
            "candidates": sorted(pair.asked),
            "labels": {label: item.id for label, item in labels.items()},
            "matches": [labels[m].id for m in answer.matches],
            "confident": answer.confident,
            "reason": answer.reason,
            "model": model,
            "usage": usage,
            "cost_usd": round(cost, 6),
            "repaired": repaired,
            "prompt_version": PROMPT_VERSION,
            "asked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
        "",
        cost,
    )


# ---- the saved answers -------------------------------------------------------------------------

_FILE_LOCK = threading.Lock()


def load_answers(folder: Path) -> dict[str, dict[str, Any]]:
    """The saved answers in a set pack, per vendor drawing (empty when there are none)."""
    try:
        document = json.loads((folder / MATCH_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    answers = document.get("answers") if isinstance(document, Mapping) else None
    return {str(k): dict(v) for k, v in (answers or {}).items() if isinstance(v, Mapping)}


def _save(folder: Path, ref: str, entry: Mapping[str, Any]) -> None:
    with _FILE_LOCK:
        answers = load_answers(folder)
        answers[ref] = dict(entry)
        path = folder / MATCH_FILE
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
        temporary.write_text(
            json.dumps({"version": 1, "answers": answers}, indent=1, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)


def _still_good(entry: Mapping[str, Any] | None, pair: AcrossPair) -> bool:
    """A saved answer still answers this drawing's question: the same question, and the same
    candidates or fewer with its picks among them (`pairing.reuse`: code since rules some out)."""
    if entry is None or entry.get("prompt_version") != PROMPT_VERSION:
        return False
    answer = MatchAnswer(
        tuple(str(m) for m in entry.get("matches") or []), bool(entry.get("confident"))
    )
    shown = [str(c) for c in entry.get("candidates") or []]
    return reuse(shown, answer, pair.asked) is not None


def to_ask(pairs: Sequence[AcrossPair], folder: Path) -> tuple[list[AcrossPair], int]:
    """The vendor drawings the matcher must be asked about (shown some candidates, no good saved
    answer), and how many have a good saved answer."""
    saved = load_answers(folder)
    todo: list[AcrossPair] = []
    reused = 0
    for pair in pairs:
        if not pair.asked:
            continue
        if _still_good(saved.get(pair.vendor.ref), pair):
            reused += 1
            continue
        todo.append(pair)
    return todo, reused


def match_pairs(
    pairs: Sequence[AcrossPair],
    folder: Path,
    ask: Asker,
    pictures: PictureMaker,
    *,
    fits: Callable[[int], bool] | None = None,
    stop: threading.Event | None = None,
    parallel: int = PARALLEL,
    first: Callable[[AcrossPair], Any] | None = None,
    on_done: Callable[[str], None] | None = None,
) -> MatchReport:
    """Ask the matcher for every vendor drawing paired across the uploads that has no good saved
    answer, and save each answer in the pack. `fits(tokens)`: whether a call of about this many
    input tokens still fits the run's money (else it is not asked: code decides alone). The first
    call of each group of drawings shown the same items runs before the others of its group (it
    writes the pictures' cache entry); the others follow as soon as it has answered, `parallel`
    calls at a time in all. `first(pair)`: the order to ask in (a full check asks first about the
    pages its first helpers compare, #1883); `on_done(ref)`: called once for every drawing asked
    about, as soon as it has its answer or is left to code (failed, money, stop)."""
    report = MatchReport()
    todo, report.reused = to_ask(pairs, folder)
    if first is not None:
        todo.sort(key=first)
    lock = threading.Lock()

    def one(pair: AcrossPair) -> None:
        if stop is not None and stop.is_set():
            with lock:
                report.failed.append((pair.vendor.ref, "the check was stopped"))
            return
        tokens = _TEXT_TOKENS + VENDOR_TOKENS + ITEM_TOKENS * len(pair.asked)
        if fits is not None and not fits(tokens):
            with lock:
                report.skipped_for_money += 1
                report.failed.append((pair.vendor.ref, "the run's money would be passed"))
            return
        with lock:
            report.asked += 1
        try:
            entry, why, cost = ask_one(pair, ask, pictures)
        except Exception as failed:  # noqa: BLE001 - code then decides alone, said in the reason
            _log.warning("page matcher: %s failed: %s", pair.vendor.ref, type(failed).__name__)
            with lock:
                report.failed.append(
                    (pair.vendor.ref, f"the call failed ({type(failed).__name__})")
                )
            return
        with lock:
            report.cost_usd += cost
            if entry is None:
                report.failed.append((pair.vendor.ref, why))
                return
            report.answered += 1
        _save(folder, pair.vendor.ref, entry)

    _dispatch(todo, one, max(1, parallel), on_done)
    return report


def _dispatch(
    todo: Sequence[AcrossPair],
    one: Callable[[AcrossPair], None],
    parallel: int,
    on_done: Callable[[str], None] | None,
) -> None:
    """Runs `one` for each drawing in `todo`'s order, `parallel` at a time; a group's later
    drawings (shown the same items) wait until its first has answered (the cache entry)."""

    def group(pair: AcrossPair) -> tuple[str, ...]:
        return tuple(sorted(pair.asked))

    leader: dict[tuple[str, ...], str] = {}
    for pair in todo:
        leader.setdefault(group(pair), pair.vendor.ref)
    opened: set[tuple[str, ...]] = set()
    queue = list(todo)
    running = 0
    ready = threading.Condition()

    def task(pair: AcrossPair) -> None:
        nonlocal running
        try:
            one(pair)
        finally:
            with ready:
                running -= 1
                opened.add(group(pair))  # its first has answered (or was left to code)
                ready.notify_all()
            if on_done is not None:
                try:
                    on_done(pair.vendor.ref)
                except Exception:  # noqa: BLE001 - the answer is saved; only the news is lost
                    _log.exception("page matcher: the answer's follow-up failed")

    with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="gv-page-match") as pool:
        while True:
            with ready:
                while True:
                    if not queue and running == 0:
                        return
                    chosen = None
                    if running < parallel:
                        chosen = next(
                            (
                                p
                                for p in queue
                                if group(p) in opened or leader[group(p)] == p.vendor.ref
                            ),
                            None,
                        )
                    if chosen is not None:
                        queue.remove(chosen)
                        running += 1
                        break
                    ready.wait()
            pool.submit(task, chosen)


# ---- in the app --------------------------------------------------------------------------------


def engine_asker(engine: ChatEngine, model_name: str, effort: str = "low") -> Asker:
    """The matcher's call through the engine's own call path (`ChatEngine._call`: streamed, asked
    once more after a failure worth it, recorded on the Usage page, charged to the answer)."""
    from app.review.assistant.chat_engine import HARNESS_ID
    from app.review.assistant.chat_request import MODELS, thinking_config

    model = MODELS[model_name]
    prompt_id = PROMPT_ID.format(provider=engine.provider.name, harness=HARNESS_ID)
    estimate = import_module("app.review.assistant.simple_check.compare").estimate_cost_usd

    def ask(messages: list[dict[str, Any]], schema: Mapping[str, Any]) -> AskReply:
        reply = engine._call(
            model=model,
            effort=effort,
            thinking=thinking_config(model, False),
            system=SYSTEM,
            tools=(),
            messages=messages,
            final=False,
            max_tokens=MAX_TOKENS,
            prompt_id=prompt_id,
            display=None,
            output_format=schema,
        )
        usage = dict(reply.usage)
        answered = reply.model or model.id
        return AskReply(reply.text(), reply.stop_reason, usage, answered, estimate(answered, usage))

    return ask


#: How long a full check waits for the set pack (its labels) before pairing across the uploads.
PACK_WAIT_SECONDS: Final = 900.0


def wait_ready(
    folder: Path, stop: threading.Event, timeout: float = PACK_WAIT_SECONDS, poll: float = 2.0
) -> bool:
    """Whether the set pack is made (its labels too), waiting up to `timeout`; False when it
    failed, `stop` is set or the time passes."""
    from time import monotonic

    from app.review.assistant.full_check_plan import pack_ready

    ends = monotonic() + timeout
    while True:
        if pack_ready(folder):
            return True
        try:
            state = json.loads((folder / "status.json").read_text(encoding="utf-8")).get("state")
        except (OSError, ValueError, AttributeError):
            state = None
        if state == "failed" or monotonic() >= ends:
            return False
        if stop.wait(poll):
            return False


def _live(engine: Any, step: str, *counts: int) -> None:
    """The matcher's progress on the live screen (`live_check.LiveCheck.matching`), when there is
    one; a screen that is gone never stops the matcher."""
    live = getattr(engine, "live", None)
    hook = getattr(live, step, None)
    if hook is None:
        return
    try:
        hook(*counts)
    except Exception:  # noqa: BLE001 - only the screen misses it
        _log.debug("page matcher: the live screen was not told")


def run_for_engine(
    engine: ChatEngine,
    folder: Path,
    main_file: str | None,
    *,
    work: Any = None,
    first: Callable[[AcrossPair], Any] | None = None,
    on_done: Callable[[str], None] | None = None,
) -> MatchReport | None:
    """A full check's page matcher (every mode): every vendor drawing the work list pairs across
    the uploads is asked about once (answers saved in the pack), within the answer's runaway
    guard, the live screen counting them ("Matching pages", #1883). `work`: the work list
    already built (else it is built here); `first`, `on_done`: as `match_pairs`. `None` when it is
    off, there is nothing to pair, or no provider."""
    model_name = getattr(engine.settings, "page_match_model", None)
    if not model_name or folder is None:
        return None
    if work is None:
        _read_index(engine, folder)
        worklist = import_module("app.review.assistant.simple_check.worklist")
        try:
            work = worklist.build_worklist(folder, main_file)
        except Exception:  # noqa: BLE001 - nothing to pair: the check goes on without the matcher
            _log.warning("page matcher: no work list")
            return None
    pairs = [pair for pair in work.across if pair.asked]
    if not pairs:
        return None
    from app.review.assistant.chat_request import MODELS

    model = MODELS[model_name]

    def fits(tokens: int) -> bool:
        return bool(engine._fits(engine._price(model, tokens, MAX_TOKENS) or 0))

    total = len(to_ask(pairs, folder)[0])
    done = [0]
    count_lock = threading.Lock()

    def answered(ref: str) -> None:
        with count_lock:
            done[0] += 1
            now = done[0]
        _live(engine, "matching", now, total)
        if on_done is not None:
            on_done(ref)

    if total:
        _live(engine, "matching", 0, total)
    pictures = Pictures(folder, engine.tools.workspace.uploads)
    try:
        report = match_pairs(
            pairs,
            folder,
            engine_asker(engine, model_name),
            pictures,
            fits=fits,
            stop=engine.cancel,
            first=first,
            on_done=answered,
        )
    finally:
        if total:
            _live(engine, "matched")
    _log.info(
        "page matcher: %d asked, %d answered, %d reused, %d without an answer, $%.3f",
        report.asked,
        report.answered,
        report.reused,
        len(report.failed),
        report.cost_usd,
    )
    return report


class MatchRun:
    """A full check's page matcher running beside its helpers (#1883).

    The matcher took 17 minutes on a 96-drawing two-file set, and no helper started until every
    answer was in. Now code's pairs (its own ranking and the pack's saved answers) are known at
    once (`cross`); the matcher runs in its own thread (`start`), asking first about the pages the
    first helpers compare; each helper waits only for its own pages (`wait_pages`): a page is
    decided once its drawings' answers are in, or the matcher left them to code (a failed call,
    the money, a stop), exactly as when the whole matcher had ended. The agreement rule is the
    same (`pairing.agree`, in the work list), so a page is paired with the same architect pages
    as before, or Not compared with code's reason. `finish` waits for the last answer (pages no
    helper compares are asked last) and gives the final pairs."""

    def __init__(
        self,
        engine: ChatEngine,
        folder: Path,
        main_file: str | None,
        work: Any,
        memo: dict[Any, Any],
    ) -> None:
        self._engine = engine
        self.folder = folder
        self.main_file = main_file
        self._work = work
        self._memo = memo
        self._cross = CrossPairs.of(work)
        asked = [pair for pair in work.across if pair.asked]
        todo = (
            to_ask(asked, folder)[0] if getattr(engine.settings, "page_match_model", None) else []
        )
        #: The drawings still waiting for the matcher -> their page in the vendor's file (or None).
        self._pending: dict[str, int | None] = {
            pair.vendor.ref: (pair.vendor.own_page if pair.vendor.file == work.file else None)
            for pair in todo
        }
        self.total = len(self._pending)
        #: The vendor's pages the matcher is asked about (fixed): a helper comparing any of them
        #: waits for their answers (`wait_pages`); every other page is decided already.
        self.to_answer: frozenset[int] = frozenset(
            page for page in self._pending.values() if page is not None
        )
        self._cond = threading.Condition()
        self._fresh = True
        self._build_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.report: MatchReport | None = None

    @property
    def cross(self) -> CrossPairs:
        """The pairs as they stand now (code's, and the answers in so far)."""
        return self._cross

    def waiting(self) -> frozenset[int]:
        """The vendor's pages still waiting for a matcher answer."""
        with self._cond:
            return frozenset(p for p in self._pending.values() if p is not None)

    def start(self, first_pages: Sequence[int] = ()) -> None:
        """Starts the matcher (nothing to ask: nothing runs). `first_pages`: the order its
        drawings are asked in (the first helpers' pages first); other pages after them."""
        if not self._pending or self._thread is not None:
            return
        order = {page: n for n, page in enumerate(dict.fromkeys(first_pages))}
        main = self._work.file

        def first(pair: AcrossPair) -> tuple[int, int, str]:
            page = pair.vendor.own_page if pair.vendor.file == main else 0
            return (order.get(page, len(order)), page, pair.vendor.ref)

        self._thread = threading.Thread(
            target=self._run, args=(first,), name="gv-page-match-run", daemon=True
        )
        self._thread.start()

    def _run(self, first: Callable[[AcrossPair], Any]) -> None:
        timings = getattr(self._engine, "timings", None)
        try:
            with timings.phase("page_match") if timings is not None else _no_phase():
                self.report = run_for_engine(
                    self._engine,
                    self.folder,
                    self.main_file,
                    work=self._work,
                    first=first,
                    on_done=self._answered,
                )
        except Exception:  # noqa: BLE001 - code then pairs alone (its saved answers, if any)
            _log.exception("assistant full check: the page matcher failed")
        finally:
            with self._cond:
                if self._pending:
                    self._pending.clear()
                    self._fresh = False
                self._cond.notify_all()

    def _answered(self, ref: str) -> None:
        with self._cond:
            if self._pending.pop(ref, False) is not False:
                self._fresh = False
            self._cond.notify_all()

    def wait_pages(self, pages: Sequence[int], cancel: threading.Event | None = None) -> CrossPairs:
        """The pairs once none of `pages` waits for the matcher any more (or the check stops)."""
        wanted = set(pages)
        with self._cond:
            while any(page in wanted for page in self._pending.values()):
                if cancel is not None and cancel.is_set():
                    break
                self._cond.wait(1.0)
        return self.current()

    def current(self) -> CrossPairs:
        """The pairs with every answer saved so far: the work list is built again (code's ranking
        is kept, `worklist.RankMemo`: only the agreement rule runs again)."""
        with self._build_lock:
            with self._cond:
                if self._fresh:
                    return self._cross
                # An answer landing while this builds marks it stale again.
                self._fresh = True
            worklist = import_module("app.review.assistant.simple_check.worklist")
            try:
                work = worklist.build_worklist(self.folder, self.main_file, memo=self._memo)
            except Exception:  # noqa: BLE001 - the pairs so far stand
                _log.exception("assistant full check: the cross-file pairs were not built again")
                with self._cond:
                    self._fresh = False
                return self._cross
            self._cross = CrossPairs.of(work)
            return self._cross

    def finish(self) -> CrossPairs:
        """The final pairs, once the matcher has asked about every drawing."""
        if self._thread is not None:
            self._thread.join()
        return self.current()


def _read_index(engine: ChatEngine, folder: Path) -> None:
    """The set index first (`set_index.py`: each page's project, set and views, read once and
    kept in the pack): a vendor drawing is then paired only within its own project, never with a
    reference set. A failed read leaves the index as it is (no project known for its pages)."""
    try:
        import_module("app.review.assistant.set_index").run_for_engine(engine, folder)
    except Exception:  # noqa: BLE001 - pairing goes on with what the index holds
        _log.exception("assistant full check: the set index was not read")


@contextmanager
def _no_phase() -> Iterator[None]:
    yield


def prepare_run(engine: ChatEngine, page_map: Mapping[str, Any] | None) -> MatchRun | None:
    """A full check's page matching, not started yet (`MatchRun`): when the set has an
    architect's upload apart from the vendor's, the whole pack is waited for (its labels) and the
    work list gives code's pairs at once. `None` when the set has no separate architect's upload
    or the pack is not there."""
    folder = engine.tools.workspace.set_pack
    main = engine.book.main_file
    roles = (page_map or {}).get("upload_roles") or {}
    separate = any(
        role == "architect" and name != main
        for name, role in (roles.items() if isinstance(roles, Mapping) else ())
    )
    if folder is None or not separate:
        return None
    if not wait_ready(folder, engine.cancel):
        _log.warning("assistant full check: the set pack is not ready; no cross-file pairs")
        return None
    # The set index first (each page's project, set and views, read once and kept in the pack):
    # a vendor drawing is paired only within its own project, never with a reference set.
    _read_index(engine, folder)
    worklist = import_module("app.review.assistant.simple_check.worklist")
    memo: dict[Any, Any] = {}
    try:
        work = worklist.build_worklist(folder, main, memo=memo)
    except Exception:  # noqa: BLE001 - the helpers are planned without the pairs
        _log.exception("assistant full check: no work list for the cross-file pairs")
        return None
    return MatchRun(engine, folder, main, work, memo)
