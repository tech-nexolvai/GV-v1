"""The AI pair check for the direct compare: two AIs choose the vendor line for an unpaired span.

The geometry pass (`pair.py`) pairs an architect dimension with a vendor dimension line only when
code can line the two views up exactly; most spans stay "?" (unpaired). This module asks the two AIs
of the decided pair, **independently**, which numbered vendor line measures the same part as one
highlighted architect dimension, and code decides whether their answer stands.

**Off by default** (`GV_ARCH_COMPARE_AI_PAIR`). Nothing is asked unless it is switched on.

**What is asked (code, $0).** For every UNPAIRED architect dimension that has a span, a value and a
view of a printed kind, and for every vendor view on the sheet of **the same printed kind** that
draws at least one real dimension line (slash ticks at both ends and a label of its own,
`pair.real_lines`): one picture. On the left the architect's view with that dimension's span
highlighted in blue and marked `A`; on the right the vendor's view with every real dimension line
marked in orange and numbered `1..k`. The page is rendered vendor-only (GV's markup annotations are
not painted). The question: which numbered line measures the same physical span on the same wall,
a number or `none` (or `unsure`), and the vendor's inch number printed on it as read.

**Code decides** (`decide`). A pair is accepted only when ALL of these hold:

1. both AIs chose the same numbered line (not `none`, not `unsure`, not a failed call);
2. that line is a real dimension line of the architect dimension's own axis;
3. its label is the vendor's black ink (GV's coloured markup there refuses it: never a source);
4. code's geometry does not contradict it: after a coarse left/right (or top/bottom) alignment of
   the two views' dimensioned runs, the vendor line lies in the same region of the run as the
   architect span, within `region_slack` of the run. **Never by value**: no printed number is used;
5. no other architect dimension was paired with the same line (by geometry or by the AIs), and the
   AIs chose a line in only one vendor view.

Otherwise the dimension stays unpaired with the reason in plain words ("the two AIs disagree",
"both AIs said no vendor line measures this span", "the AI budget was reached", ...).

**The vendor's number** on an accepted pair: a stored reading of that label when the caller has one
(`stored_vendor_inches`); else the two AIs' inch readings when they agree exactly; else unread.
Inches only: a millimetre-only reading is refused. Code compares nothing here.

**The calls.** Each attempt goes to `record_attempt` (the call ledger, so `model_invocations` gets
its cost); a stored answer to the identical question is replayed through `stored`, never paid twice.
One spend guard: each call's worst case is reserved before it is sent, and a call the cap refuses
stops the asking; every question not asked says so.

Source: issue #1427. Verification: `tests/extraction/compare/test_ai_pair.py`.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from decimal import Decimal
from fractions import Fraction
from time import monotonic
from typing import Any, Final, Protocol

from extraction.compare.dimensions import ArchitectDimension, Axis, DrawnDimension
from extraction.compare.page import ArchitectViewDimensions, ComparePage, VendorViewDimensions
from extraction.compare.pair import (
    MEASURED_PAIR_SETTINGS,
    Pairing,
    PairSettings,
    PairStatus,
    real_lines,
)
from extraction.form_reader.bedrock import (
    AttemptUsage,
    MalformedFormAnswer,
    _response_text,
    _response_usage,
)
from extraction.geometry.rows import Box, TickSource
from extraction.ink import InkClass
from extraction.slot_reader.claude_output import (
    DEFAULT_CLAUDE_EFFORT,
    OUTPUT_CONFIG_KEY,
    ClaudeEffort,
    _conforms,
    claude_answer,
    output_config,
    png_size,
    require_picture_fits,
    visual_tokens,
)

__all__ = [
    "AI_PAIR_ENV",
    "AI_PAIR_PROMPT",
    "AI_PAIR_PROMPT_ID",
    "CLAUDE_PAIR",
    "MEASURED_AI_PAIR_SETTINGS",
    "AiPairRun",
    "AiPairSettings",
    "Answer",
    "PdfPictures",
    "Pictures",
    "Question",
    "ai_pair_enabled",
    "ai_pair_schema",
    "decide",
    "estimated_input_tokens",
    "pair_with_ais",
    "questions",
]

#: The switch. Off unless set to a yes word; anything unrecognised is refused, never read as on.
AI_PAIR_ENV: Final = "GV_ARCH_COMPARE_AI_PAIR"
#: The decided Claude pair: the same two readers as the width, depth and job values readers'.
CLAUDE_PAIR: Final = ("anthropic.claude-opus-5-5", "anthropic.claude-sonnet-5-5")
AI_PAIR_PROMPT_ID: Final = "arch-compare-pair-v1"
_READER: Final = "arch-compare-ai-pair"

NONE: Final = "none"
UNSURE: Final = "unsure"
#: Not an answer a reader gives: the call was not made, failed, or its reply was malformed twice.
NO_ANSWER: Final = "no_answer"


def ai_pair_enabled(environ: Mapping[str, str]) -> bool:
    """Whether `GV_ARCH_COMPARE_AI_PAIR` switches the AI pair check on. Unset or empty is off."""
    raw = environ.get(AI_PAIR_ENV, "").strip().lower()
    if raw in ("", "0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    raise ValueError(f"{AI_PAIR_ENV} must be true or false, not {raw!r}")


@dataclass(frozen=True, slots=True)
class AiPairSettings:
    """Every setting the AI pair check uses (no defaults: the repo's convention)."""

    pair: PairSettings
    """The geometry pass's settings: which vendor lines are real dimension lines."""
    dpi: int
    margin_pt: Decimal
    """Each view is cut out with this much room around it."""
    max_visual_tokens: int
    """A picture is shrunk until it costs at most this many visual tokens."""
    region_slack: Fraction
    """The coarse alignment check: the vendor line and the architect span, each as a share of its
    view's dimensioned run, must overlap once each is widened by this share of the run."""
    max_questions: int
    """At most this many questions per drawing set; the rest are said, never silently dropped."""
    max_tokens: int
    effort: ClaudeEffort
    prompt_tokens: int
    """The question's words, in tokens, for the budget's estimate of each call (the picture apart)."""
    concurrency: int


MEASURED_AI_PAIR_SETTINGS: Final = AiPairSettings(
    pair=MEASURED_PAIR_SETTINGS,
    dpi=200,
    margin_pt=Decimal(12),
    max_visual_tokens=3000,
    # A quarter of the run: wide enough for two drawings that show a wall with different
    # neighbours at its ends, narrow enough to refuse a line at the other end of the wall (the
    # wrong pairs of the earlier geometry proof were mostly that).
    region_slack=Fraction(1, 4),
    max_questions=400,
    # The pair's stored answers to a one-picture question used about 250 output tokens, at most
    # about 800 (job values, 2026-10-16): 2000 leaves room and is what the budget reserves.
    max_tokens=2000,
    effort=DEFAULT_CLAUDE_EFFORT,
    prompt_tokens=700,
    concurrency=4,
)

AI_PAIR_PROMPT: Final = (
    "This picture shows two drawings of the same millwork job, side by side.\n"
    "LEFT, headed ARCHITECT: the architect's drawing. ONE dimension is highlighted in blue and "
    "marked A.\n"
    "RIGHT, headed VENDOR: the vendor's shop drawing. Every vendor dimension line is marked in "
    "orange and numbered.\n\n"
    "Question: which numbered vendor line measures the SAME part as the highlighted architect "
    "dimension A, that is the same physical span between the same two things (the same edges, ends "
    "or centre lines) on the same wall or the same piece?\n"
    "Rules:\n"
    "- Decide by what the two ends of each line touch in the drawing and where it is along the "
    "wall, never by the printed numbers. The numbers may differ: that difference is what is being "
    "checked, so a line with the same number is not a reason to choose it.\n"
    "- A line that only partly covers the span, covers more than it, or measures a neighbouring "
    "part is not the same part.\n"
    "- pick: the number of that vendor line; none if no numbered vendor line measures that same "
    "span; unsure if you cannot tell which one.\n"
    "- vendor_label: the vendor's number printed on the line you picked, copied exactly as printed "
    "(for example 600 [23 5/8] or 1'-11\"); empty if you picked none or unsure.\n"
    "- vendor_inches: that number in inches with its inch or foot marks, as printed (for example "
    '23 5/8" or 1\'-11"). When the drawing prints millimetres with inches in brackets, give the '
    "inches in the brackets with an inch mark. Empty when it prints no inches, or you picked none "
    "or unsure. Never convert or work a number out.\n"
    "- why: one short sentence.\n"
    "Return the one JSON object only."
)

_STRING: Final = {"type": "string"}


def ai_pair_schema(candidates: int) -> dict[str, object]:
    """The answer shape for a picture with `candidates` numbered vendor lines: one pick from a
    closed list (`1`..`<k>`, `none`, `unsure`), the vendor's label and inches as read, and why."""
    if isinstance(candidates, bool) or candidates < 1:
        raise ValueError("an AI pair question needs at least one numbered vendor line")
    return {
        "type": "object",
        "properties": {
            "pick": {
                "type": "string",
                "enum": [*(str(k) for k in range(1, candidates + 1)), NONE, UNSURE],
            },
            "vendor_label": dict(_STRING),
            "vendor_inches": dict(_STRING),
            "why": dict(_STRING),
        },
        "required": ["pick", "vendor_label", "vendor_inches", "why"],
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# The questions, prepared by code at no cost
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Question:
    """One architect dimension against one vendor view of the same kind, with its picture."""

    position: int
    """The architect dimension's place in the pairings handed in."""
    page_index: int
    architect_view: ArchitectViewDimensions
    architect: ArchitectDimension
    vendor_view: VendorViewDimensions
    candidates: tuple[tuple[DrawnDimension, Box], ...]
    """The vendor view's real dimension lines, each with its own label: line `k` is `[k - 1]`."""
    picture: bytes
    page_sha256: str

    @property
    def question_id(self) -> str:
        return (
            f"p{self.page_index}-a{self.architect_view.annotation_index}."
            f"{self.architect.number}-v{self.vendor_view.number}"
        )


class Pictures(Protocol):
    """Where the pictures and the ink come from: the PDF (`PdfPictures`), or a test's stand-in."""

    def picture(
        self,
        page_index: int,
        architect_view: ArchitectViewDimensions,
        architect: ArchitectDimension,
        vendor_view: VendorViewDimensions,
        candidates: Sequence[tuple[DrawnDimension, Box]],
    ) -> bytes: ...

    def ink(self, page_index: int, box: Box) -> InkClass | None:
        """Whose ink a label is (`None`: not known, which refuses it)."""
        ...

    def page_sha256(self, page_index: int) -> str: ...


def _skip_reason(pairing: Pairing) -> str | None:
    dim = pairing.architect
    if pairing.status is not PairStatus.UNPAIRED:
        return "already paired"
    if dim.span is None or dim.inches is None:
        return "the architect's dimension has no span or its value is held"
    return None


def questions(
    pages: Sequence[ComparePage],
    pairings: Sequence[Pairing],
    pictures: Pictures,
    settings: AiPairSettings,
) -> tuple[list[Question], list[tuple[int, str]]]:
    """Every question to ask, and every unpaired dimension NOT asked about with why (by position).

    A dimension is asked about against each vendor view on its sheet of the same printed kind as
    its architect view that draws at least one real dimension line."""
    by_page = {page.page_index: page for page in pages}
    asked: list[Question] = []
    not_asked: list[tuple[int, str]] = []
    for position, pairing in enumerate(pairings):
        if _skip_reason(pairing) is not None:
            continue
        page = by_page.get(pairing.page_index)
        arch_view = (
            None
            if page is None
            else next(
                (v for v in page.architect if v.annotation_index == pairing.architect_view), None
            )
        )
        if page is None or arch_view is None:
            not_asked.append((position, "its sheet was not handed to the AI pair check"))
            continue
        if arch_view.kind is None:
            not_asked.append((position, "the architect's view is of no printed kind"))
            continue
        views = [
            (view, tuple(real_lines(view, settings.pair)))
            for view in page.vendor
            if view.kind == arch_view.kind
        ]
        views = [(view, lines) for view, lines in views if lines]
        if not views:
            not_asked.append(
                (
                    position,
                    f"no vendor view on the sheet is a {arch_view.kind} with a real dimension line",
                )
            )
            continue
        for view, lines in views:
            if len(asked) >= settings.max_questions:
                not_asked.append(
                    (position, f"over the cap of {settings.max_questions} AI pair questions")
                )
                break
            asked.append(
                Question(
                    position,
                    pairing.page_index,
                    arch_view,
                    pairing.architect,
                    view,
                    lines,
                    pictures.picture(pairing.page_index, arch_view, pairing.architect, view, lines),
                    pictures.page_sha256(pairing.page_index),
                )
            )
    return asked, not_asked


def estimated_input_tokens(question: Question, settings: AiPairSettings) -> int:
    """The question's size in tokens: the picture's documented tiles plus the words."""
    width, height = png_size(question.picture)
    return visual_tokens(width, height) + settings.prompt_tokens


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Answer:
    """One AI's answer to one question, as given (or why there is none)."""

    model_id: str
    pick: str
    """`1`..`k`, `none`, `unsure`, or `no_answer` (not asked, failed, or malformed twice)."""
    vendor_label: str = ""
    vendor_inches: str = ""
    why: str = ""


def question_packet(
    question: Question, settings: AiPairSettings, document_version_id: str
) -> dict[str, object]:
    """What the question shows and asks, signed: the identity a stored answer is reused by."""
    body: dict[str, object] = {
        "reader": _READER,
        "document_version_id": document_version_id,
        "page_index": question.page_index,
        "source_page_sha256": question.page_sha256,
        "question_id": question.question_id,
        "prompt_id": AI_PAIR_PROMPT_ID,
        "prompt_sha256": hashlib.sha256(AI_PAIR_PROMPT.encode()).hexdigest(),
        "schema_sha256": hashlib.sha256(
            json.dumps(
                ai_pair_schema(len(question.candidates)), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
        "effort": settings.effort,
        "images": {"picture_1": {"sha256": hashlib.sha256(question.picture).hexdigest()}},
    }
    body["packet_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return body


def _request(question: Question, model_id: str, settings: AiPairSettings) -> dict[str, Any]:
    return {
        "modelId": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"image": {"format": "png", "source": {"bytes": question.picture}}},
                    {"text": AI_PAIR_PROMPT},
                ],
            }
        ],
        "inferenceConfig": {"maxTokens": settings.max_tokens, "temperature": 0},
        OUTPUT_CONFIG_KEY: output_config(ai_pair_schema(len(question.candidates)), settings.effort),
    }


def _answer(model_id: str, parsed: Mapping[str, Any]) -> Answer:
    return Answer(
        model_id,
        str(parsed["pick"]),
        str(parsed["vendor_label"]),
        str(parsed["vendor_inches"]),
        str(parsed["why"]),
    )


class _Stop:
    def __init__(self) -> None:
        self.cause: str | None = None


def _ask(
    question: Question,
    model_id: str,
    *,
    client: Any,
    settings: AiPairSettings,
    document_version_id: str,
    record_attempt: Callable[[AttemptUsage], None],
    spend_guard: Any,
    stored: Callable[[str, Mapping[str, object]], str | None] | None,
    stop: _Stop,
) -> Answer:
    packet = question_packet(question, settings, document_version_id)
    schema = ai_pair_schema(len(question.candidates))
    index = question.page_index
    if stored is not None:
        raw = stored(model_id, packet)
        if raw is not None:
            try:
                parsed = json.loads(raw)
                if not _conforms(parsed, schema):
                    raise MalformedFormAnswer("the stored answer does not match its shape")
                record_attempt(
                    AttemptUsage(
                        model_id,
                        AI_PAIR_PROMPT_ID,
                        AI_PAIR_PROMPT_ID,
                        0,
                        0,
                        0,
                        False,
                        page_index=index,
                        raw_response_text=raw,
                        question_packet=packet,
                        reused_from="stored",
                    )
                )
                return _answer(model_id, parsed)
            except (MalformedFormAnswer, json.JSONDecodeError):
                pass
    if stop.cause is not None:
        return Answer(model_id, NO_ANSWER, why=f"not asked: the asking stopped ({stop.cause})")
    require_picture_fits([question.picture], model_id)
    for attempt in range(2):
        reservation = None
        if spend_guard is not None:
            from extraction.slot_reader.anthropic import SpendCapExceeded

            try:
                reservation = spend_guard.reserve(
                    model_id, estimated_input_tokens(question, settings), settings.max_tokens
                )
            except SpendCapExceeded:
                stop.cause = "the AI budget was reached"
                return Answer(model_id, NO_ANSWER, why="not asked: the AI budget was reached")
        request = _request(question, model_id, settings)
        if attempt:
            request["messages"][0]["content"].append(
                {"text": "Your previous reply was malformed. Return the one JSON object only."}
            )
        started = monotonic()
        try:
            response = client.converse(**request)
        except Exception as error:  # noqa: BLE001 - recorded; the reader abstains
            record_attempt(
                AttemptUsage(
                    model_id,
                    AI_PAIR_PROMPT_ID,
                    AI_PAIR_PROMPT_ID,
                    None,
                    None,
                    int((monotonic() - started) * 1000),
                    False,
                    type(error).__name__,
                    index,
                    attempt_number=attempt + 1,
                    question_packet=packet,
                )
            )
            return Answer(model_id, NO_ANSWER, why=f"the call failed: {type(error).__name__}")
        if reservation is not None:
            spend_guard.settle(reservation, response)
        input_tokens, output_tokens = _response_usage(response)
        elapsed = int((monotonic() - started) * 1000)
        raw_text: str | None = None
        try:
            raw_text = _response_text(response)
            parsed = claude_answer(response, schema)
        except MalformedFormAnswer:
            record_attempt(
                AttemptUsage(
                    model_id,
                    AI_PAIR_PROMPT_ID,
                    AI_PAIR_PROMPT_ID,
                    input_tokens,
                    output_tokens,
                    elapsed,
                    True,
                    page_index=index,
                    raw_response_text=raw_text,
                    attempt_number=attempt + 1,
                    question_packet=packet,
                )
            )
            continue
        record_attempt(
            AttemptUsage(
                model_id,
                AI_PAIR_PROMPT_ID,
                AI_PAIR_PROMPT_ID,
                input_tokens,
                output_tokens,
                elapsed,
                False,
                page_index=index,
                raw_response_text=raw_text,
                attempt_number=attempt + 1,
                question_packet=packet,
            )
        )
        return _answer(model_id, parsed)
    return Answer(model_id, NO_ANSWER, why="its answer was malformed twice")


# ---------------------------------------------------------------------------
# Code decides
# ---------------------------------------------------------------------------


def _run(lines: Sequence[DrawnDimension], axis: Axis, box: Box) -> tuple[Fraction, Fraction]:
    """The view's dimensioned run along `axis`: from the first tick to the last of its lines on
    that axis, or the view's own box when it has no length there."""
    on = [line for line in lines if line.axis is axis]
    if on:
        low = min(Fraction(line.start_pt) for line in on)
        high = max(Fraction(line.end_pt) for line in on)
        if high > low:
            return low, high
    if axis is Axis.HORIZONTAL:
        return Fraction(box.x0), Fraction(box.x1)
    return Fraction(box.top), Fraction(box.bottom)


def _share(span: DrawnDimension, run: tuple[Fraction, Fraction]) -> tuple[Fraction, Fraction]:
    low, high = run
    length = high - low
    if length <= 0:
        return Fraction(0), Fraction(1)
    return (Fraction(span.start_pt) - low) / length, (Fraction(span.end_pt) - low) / length


def same_region(question: Question, line: DrawnDimension, slack: Fraction) -> bool:
    """Whether code's coarse alignment agrees: the architect span and the vendor line, each as a
    share of its own view's dimensioned run, overlap once both are widened by `slack`. Geometry
    only: no printed number is used."""
    span = question.architect.span
    assert span is not None
    a0, a1 = _share(
        span, _run(question.architect_view.lines, span.axis, question.architect_view.box)
    )
    v0, v1 = _share(
        line,
        _run([c for c, _label in question.candidates], line.axis, question.vendor_view.extent),
    )
    return v0 - slack <= a1 and a0 - slack <= v1


_MILLIMETRES: Final = re.compile(r"\bmm\b|millimet", re.IGNORECASE)
_INCH_MARKED: Final = re.compile(r"[\"'″′”’]|\bin\b|\binch", re.IGNORECASE)


def vendor_inches(answer: Answer) -> Fraction | None:
    """An AI's inch reading of the vendor's number, exactly; `None` when it is not one dimension
    in inches (empty, millimetres, no inch or foot mark, or not a size)."""
    from units.normalise import normalise_to_inches

    text = answer.vendor_inches.strip()
    if not text or _MILLIMETRES.search(text) or not _INCH_MARKED.search(text):
        return None
    try:
        measured = normalise_to_inches(text)
    except Exception:  # noqa: BLE001 - not read, never guessed
        return None
    return measured.exact if measured.exact > 0 else None


@dataclass(frozen=True, slots=True)
class _Verdict:
    question: Question
    line: int | None
    """The accepted line's index into `question.candidates`; `None` when refused."""
    reason: str


def _picks(answer: Answer, k: int) -> int | None:
    return int(answer.pick) - 1 if answer.pick.isdigit() and 1 <= int(answer.pick) <= k else None


def decide(
    question: Question,
    first: Answer,
    second: Answer,
    *,
    pictures: Pictures,
    settings: AiPairSettings,
) -> _Verdict:
    """One question's answer by code: the line both AIs chose and every check passed, or why not."""
    k = len(question.candidates)
    a, b = _picks(first, k), _picks(second, k)
    view = question.vendor_view.number
    if first.pick == NO_ANSWER or second.pick == NO_ANSWER:
        missing = first if first.pick == NO_ANSWER else second
        return _Verdict(
            question,
            None,
            f"vendor view {view}: {missing.model_id} gave no answer "
            f"({missing.why or 'no reason given'})",
        )
    if first.pick == NONE and second.pick == NONE:
        return _Verdict(
            question, None, f"vendor view {view}: both AIs said no vendor line measures this span"
        )
    if a is None or b is None:
        return _Verdict(
            question,
            None,
            f"vendor view {view}: the AIs did not both choose a line "
            f"({first.pick} and {second.pick})",
        )
    if a != b:
        return _Verdict(
            question,
            None,
            f"vendor view {view}: the two AIs disagree (line {a + 1} and line {b + 1})",
        )
    line, label = question.candidates[a]
    span = question.architect.span
    assert span is not None
    if line.axis is not span.axis:
        return _Verdict(
            question,
            None,
            f"vendor view {view}: both AIs chose line {a + 1}, a {line.axis.value} line, but the "
            f"architect's dimension is {span.axis.value}: refused",
        )
    if line.tick_source is not TickSource.SLASH or label not in line.label_boxes:
        return _Verdict(
            question, None, f"vendor view {view}: line {a + 1} is not a real dimension line"
        )
    ink = pictures.ink(question.page_index, label)
    if ink is not InkClass.VENDOR:
        return _Verdict(
            question,
            None,
            f"vendor view {view}: line {a + 1}'s number is "
            + (
                "where GV's coloured markup is; GV's markup is never a source, so it is refused"
                if ink is not None
                else "in ink code could not check, so it is refused"
            ),
        )
    if not same_region(question, line, settings.region_slack):
        return _Verdict(
            question,
            None,
            f"vendor view {view}: both AIs chose line {a + 1}, but it lies in another part of the "
            "run than the architect's span once the two views are lined up: refused",
        )
    return _Verdict(question, a, f"both AIs chose vendor view {view}'s line {a + 1}")


def _line_key(page_index: int, view: int | None, line: DrawnDimension) -> tuple[object, ...]:
    return (page_index, view, line.axis.value, line.offset_pt, line.start_pt, line.end_pt)


@dataclass
class AiPairRun:
    """What the AI pair check did: the pairings out, and the counts for the report."""

    pairings: tuple[Pairing, ...]
    asked: int = 0
    not_asked: list[tuple[int, str]] = field(default_factory=list)
    stopped: str | None = None
    answers: dict[tuple[int, str], Answer] = field(default_factory=dict)
    """Each answer by (question number, model)."""
    question_list: list[Question] = field(default_factory=list)


def _vendor_number(
    pairing: Pairing,
    answers: Sequence[Answer],
    stored_vendor_inches: Callable[[Pairing], Fraction | None] | None,
) -> tuple[Fraction | None, str]:
    if stored_vendor_inches is not None:
        found = stored_vendor_inches(pairing)
        if found is not None:
            return found, "a stored reading of the vendor's label"
    readings = [vendor_inches(answer) for answer in answers]
    if all(reading is not None for reading in readings) and len(set(readings)) == 1:
        return readings[0], "both AIs read the vendor's number alike, in inches"
    if any(_MILLIMETRES.search(answer.vendor_inches) for answer in answers):
        return None, "unread: an AI read millimetres only; inches count"
    return None, "unread: the two AIs' readings of the vendor's number are not the same inches"


def pair_with_ais(
    pages: Sequence[ComparePage],
    pairings: Sequence[Pairing],
    *,
    pictures: Pictures,
    clients: Mapping[str, Any] | None,
    document_version_id: str,
    record_attempt: Callable[[AttemptUsage], None],
    settings: AiPairSettings = MEASURED_AI_PAIR_SETTINGS,
    spend_guard: Any = None,
    stored: Callable[[str, Mapping[str, object]], str | None] | None = None,
    stored_vendor_inches: Callable[[Pairing], Fraction | None] | None = None,
    models: Sequence[str] = CLAUDE_PAIR,
) -> AiPairRun:
    """The pairings again, every one in its place: an unpaired dimension both AIs and code agree on
    becomes paired (`paired_by="two AIs"`), every other keeps its status, with the AI check's reason
    added to an unpaired one's. `clients` maps each model id to a client with `converse(**request)`;
    `None` asks nothing (the $0 run: every question reported as not asked)."""
    out = list(pairings)
    asked, not_asked = questions(pages, pairings, pictures, settings)
    run = AiPairRun(pairings=(), not_asked=list(not_asked), question_list=asked)
    if clients is None:
        run.not_asked += [(q.position, "no model may be asked in this run") for q in asked]
        asked = []
    stop = _Stop()
    first_model, second_model = models
    jobs = [(n, q, m) for n, q in enumerate(asked) for m in (first_model, second_model)]

    def one(job: tuple[int, Question, str]) -> None:
        number, question, model = job
        assert clients is not None
        run.answers[(number, model)] = _ask(
            question,
            model,
            client=clients[model],
            settings=settings,
            document_version_id=document_version_id,
            record_attempt=record_attempt,
            spend_guard=spend_guard,
            stored=stored,
            stop=stop,
        )

    if jobs:
        with ThreadPoolExecutor(max_workers=max(1, settings.concurrency)) as pool:
            list(pool.map(one, jobs))
    run.asked = len(asked)
    run.stopped = stop.cause

    verdicts: dict[int, list[tuple[int, _Verdict]]] = {}
    for number, question in enumerate(asked):
        verdict = decide(
            question,
            run.answers[(number, first_model)],
            run.answers[(number, second_model)],
            pictures=pictures,
            settings=settings,
        )
        verdicts.setdefault(question.position, []).append((number, verdict))

    # Lines already taken by the geometry pass, and the AIs' choices: a line chosen for two
    # architect dimensions pairs neither, and so does a dimension paired in two vendor views.
    taken = Counter(
        _line_key(p.page_index, p.vendor_view, p.vendor)
        for p in pairings
        if p.status is PairStatus.PAIRED and p.vendor is not None
    )
    chosen: dict[int, tuple[int, _Verdict]] = {}
    reasons: dict[int, list[str]] = {}
    for position, found in verdicts.items():
        accepted = [(n, v) for n, v in found if v.line is not None]
        reasons[position] = [v.reason for _n, v in found if v.line is None]
        if len(accepted) == 1:
            chosen[position] = accepted[0]
        elif accepted:
            reasons[position].append(
                f"both AIs chose a line in {len(accepted)} vendor views, so which one is the same "
                "span is not known"
            )
    uses = Counter(
        _line_key(
            v.question.page_index, v.question.vendor_view.number, v.question.candidates[v.line][0]
        )
        for _n, v in chosen.values()
        if v.line is not None
    )
    for position, (number, verdict) in list(chosen.items()):
        assert verdict.line is not None
        line, label = verdict.question.candidates[verdict.line]
        key = _line_key(verdict.question.page_index, verdict.question.vendor_view.number, line)
        if taken[key]:
            reasons[position].append(
                "both AIs chose a vendor line the geometry already pairs with another of the "
                "architect's dimensions: refused"
            )
            del chosen[position]
        elif uses[key] > 1:
            reasons[position].append(
                f"both AIs chose the same vendor line for {uses[key]} of the architect's "
                "dimensions, so which span it draws is not known"
            )
            del chosen[position]

    for position, why in run.not_asked:
        reasons.setdefault(position, []).append(why)
    for position, why_list in reasons.items():
        if position in chosen:
            continue
        pairing = out[position]
        joined = "; ".join(dict.fromkeys(why_list)) or "no answer"
        out[position] = replace(pairing, reason=f"{pairing.reason}; AI pair check: {joined}")
    for position, (number, verdict) in chosen.items():
        assert verdict.line is not None
        question = verdict.question
        line, label = question.candidates[verdict.line]
        ppi = question.vendor_view.points_per_inch
        paired = replace(
            out[position],
            status=PairStatus.PAIRED,
            vendor_view=question.vendor_view.number,
            vendor=line,
            vendor_inches_drawn=None if ppi is None else Fraction(line.length_pt) / Fraction(ppi),
            how="both AIs chose this line; code found it in the same part of the run",
            reason=verdict.reason,
            vendor_label=label,
            paired_by="two AIs",
        )
        value, where = _vendor_number(
            paired,
            [run.answers[(number, first_model)], run.answers[(number, second_model)]],
            stored_vendor_inches,
        )
        out[position] = replace(paired, vendor_inches=value, vendor_inches_reason=where)
    run.pairings = tuple(out)
    return run


# ---------------------------------------------------------------------------
# The pictures, from the PDF
# ---------------------------------------------------------------------------

_BLUE: Final = (0, 90, 255)
_ORANGE: Final = (235, 110, 0)


class PdfPictures:
    """The pictures and ink of one PDF's pages: rendered vendor-only (GV's markup annotations are
    not painted), each page once, at `settings.dpi`."""

    def __init__(self, data: bytes, settings: AiPairSettings) -> None:
        self._data = data
        self._settings = settings
        self._pages: dict[int, tuple[Any, Callable[[object, object], Any], str]] = {}
        self._ink: dict[int, Any] = {}

    def page_sha256(self, page_index: int) -> str:
        return self._page(page_index)[2]

    def _page(self, page_index: int) -> tuple[Any, Callable[[object, object], Any], str]:
        if page_index not in self._pages:
            import pdfplumber
            from PIL import Image

            from extraction.rasterise import render_page
            from extraction.reader import pixel_placement, read_pages

            sha = hashlib.sha256(read_pages(self._data)[page_index].content).hexdigest()
            with pdfplumber.open(io.BytesIO(self._data)) as document:
                place = pixel_placement(document.pages[page_index], self._settings.dpi)
            from uuid import UUID

            rendered = render_page(
                self._data,
                page_index,
                document_version_id=UUID(int=0),
                page_content_hash=sha,
                dpi=self._settings.dpi,
                maximum_pixels=100_000_000,
                vendor_only=True,
            )
            image = Image.frombytes(
                "RGB", (rendered.width_px, rendered.height_px), rendered.rgb_bytes
            )
            self._pages[page_index] = (image, place, sha)
        return self._pages[page_index]

    def _pixels(
        self, place: Callable[[object, object], Any], box: Box
    ) -> tuple[int, int, int, int]:
        first, second = place(box.x0, box.top), place(box.x1, box.bottom)
        return (
            min(first.x, second.x),
            min(first.y, second.y),
            max(first.x, second.x),
            max(first.y, second.y),
        )

    def ink(self, page_index: int, box: Box) -> InkClass | None:
        from extraction.ink import read_page_ink
        from extraction.reader import UnreadablePdf

        if page_index not in self._ink:
            try:
                self._ink[page_index] = read_page_ink(
                    self._data, page_index, dpi=self._settings.dpi
                )
            except UnreadablePdf:
                self._ink[page_index] = None
        ink = self._ink[page_index]
        if ink is None:
            return None
        _image, place, _sha = self._page(page_index)
        return ink.at(self._pixels(place, box)).ink  # type: ignore[no-any-return]

    def _crop(
        self,
        page_index: int,
        box: Box,
        marks: Sequence[tuple[DrawnDimension, str, tuple[int, int, int]]],
        title: str,
    ) -> Any:
        from PIL import Image, ImageDraw, ImageFont

        image, place, _sha = self._page(page_index)
        margin = self._settings.margin_pt
        x0, y0, x1, y1 = self._pixels(
            place, Box(box.x0 - margin, box.top - margin, box.x1 + margin, box.bottom + margin)
        )
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(image.width, x1), min(image.height, y1)
        crop = image.crop((x0, y0, max(x0 + 1, x1), max(y0 + 1, y1))).convert("RGBA")
        overlay = Image.new("RGBA", crop.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        scale = self._settings.dpi / 72
        font = ImageFont.load_default(size=max(12, round(9 * scale)))
        width = max(3, round(1.6 * scale))
        bar = max(6, round(4 * scale))
        for line, tag, colour in marks:
            px = self._pixels(place, line.box)
            a = (px[0] - x0, px[1] - y0)
            b = (px[2] - x0, px[3] - y0)
            draw.line([a, b], fill=(*colour, 150), width=width)
            for end in (a, b):
                if line.axis is Axis.HORIZONTAL:
                    draw.line(
                        [(end[0], end[1] - bar), (end[0], end[1] + bar)],
                        fill=(*colour, 220),
                        width=width,
                    )
                else:
                    draw.line(
                        [(end[0] - bar, end[1]), (end[0] + bar, end[1])],
                        fill=(*colour, 220),
                        width=width,
                    )
            # The tag sits just before the line's first tick, clear of the label in its middle.
            if line.axis is Axis.HORIZONTAL:
                spot = (a[0] - bar, a[1] - bar * 2)
            else:
                spot = (a[0] + bar, a[1] - bar * 2)
            left, top, right, bottom = draw.textbbox(spot, tag, font=font)
            draw.rectangle(
                (left - 2, top - 2, right + 2, bottom + 2),
                fill=(255, 255, 255, 235),
                outline=(*colour, 255),
            )
            draw.text(spot, tag, fill=(*colour, 255), font=font)
        crop = Image.alpha_composite(crop, overlay).convert("RGB")
        heading = Image.new("RGB", (crop.width, round(16 * scale)), (255, 255, 255))
        ImageDraw.Draw(heading).text((4, 2), title, fill=(0, 0, 0), font=font)
        both = Image.new("RGB", (crop.width, crop.height + heading.height), (255, 255, 255))
        both.paste(heading, (0, 0))
        both.paste(crop, (0, heading.height))
        return both

    def picture(
        self,
        page_index: int,
        architect_view: ArchitectViewDimensions,
        architect: ArchitectDimension,
        vendor_view: VendorViewDimensions,
        candidates: Sequence[tuple[DrawnDimension, Box]],
    ) -> bytes:
        from PIL import Image

        from extraction.slot_reader.claude_output import picture_fits

        assert architect.span is not None
        left = self._crop(
            page_index, architect_view.box, [(architect.span, "A", _BLUE)], "ARCHITECT"
        )
        right = self._crop(
            page_index,
            vendor_view.extent,
            [(line, str(k), _ORANGE) for k, (line, _label) in enumerate(candidates, start=1)],
            "VENDOR",
        )
        gap = 24
        image = Image.new(
            "RGB", (left.width + gap + right.width, max(left.height, right.height)), (255, 255, 255)
        )
        image.paste(left, (0, 0))
        image.paste(right, (left.width + gap, 0))
        factor = 1.0
        while True:
            w, h = max(1, int(image.width / factor)), max(1, int(image.height / factor))
            if visual_tokens(w, h) <= self._settings.max_visual_tokens and all(
                picture_fits(w, h, model) for model in CLAUDE_PAIR
            ):
                break
            factor *= 1.05
        if factor > 1:
            image = image.resize((w, h), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        return out.getvalue()
