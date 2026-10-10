"""When a label's reading counts, and what it is worth (#987).

**Two different sources, identical text, or a person** (plan rule). A label that is real text in the
file is sealed by the file's own text and one reader on the crop agreeing character for character;
a label drawn as paths, by two readers of different makers agreeing on the crop. Agreement is on
the printed text after only the normalisation a picture cannot carry — curly or prime quote glyphs,
a line break, a run of spaces. Nothing else is forgiven: `17 5/8"` and `175/8"` are different texts.

**The value comes from the sealed text alone** (E2 guard 2), parsed by `units/`. The readers are not
asked for whole, numerator and denominator at all: in E2 both readers once filed identical wrong
parts for a label whose text they had read right. A sealed text that is not a plain dimension is
held for the person, except the two forms the admin approved (#992), which `labels.py` expands by
exact arithmetic on that same text: `a"+b"` (one piece, a + b) and `N"(K EQ)` (one entry, N).
`INCLUDING FIELD CUT` and `VIF` never seal; they hold the whole row (`labels.row_hold`).

**Every guard only withholds.** The reviewer's ink on the crop (E2 guard 1, decided by code's colour
check, never the readers'), a label at the drawing's edge (E2 guard 3), a stacked fraction (#762),
a crowded crop, an uncertain slot or row, a reader that abstained or was unsure: each sends the
reading to the person with a plain reason. None of them can make a reading count.

**Unsure is never evidence of absence** (#1110). "No dimension here" counts only when both readers
say so explicitly: `no_dimension`, or "no, this span has no label" with an empty text. A reader
that is unsure whether the label belongs to the span sends it to the person (`unsure`); it never
becomes "no label", which is what can start the equal-shares read-through (`equal_shares.py`).

Source: issues #987, #992, #1110 · Verification: `tests/extraction/slot_reader/test_seal.py`
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Final

from evidence.corroborate import UNKNOWN_MODEL_VENDOR, independence_key
from extraction.form_reader.mapping import REVIEWER_MARKUP_REASON
from extraction.ink import InkAt, InkClass
from extraction.slot_reader.labels import expand_label, plain_dimension, row_hold
from extraction.slot_reader.runs import Lane, PlannedLabel
from units.measurement import Measurement
from vocabulary.cabinet_codes import is_cabinet_code, is_finish_code

__all__ = [
    "STACKED_BY_CODE",
    "TEXT_LAYER",
    "Belongs",
    "LabelOutcome",
    "LabelState",
    "OwnerOutcome",
    "ReaderAnswer",
    "normalise_text",
    "owner_outcome",
    "parse_belongs",
    "plain_dimension",
    "seal_label",
]

#: The file's own text, as a source. Its "maker" is the drawing, which no model shares.
TEXT_LAYER: Final = "pdf-text-layer"
#: The flag on a label *code* found to be a stacked fraction — the file's own text, or the
#: fraction-bar detector — beside `stacked`, which any source may set (#1107). Only this one takes a
#: piece out of its row's drawn-length scale: a reader's answer may add a hold, never remove a check.
STACKED_BY_CODE: Final = "stacked-by-code"

_QUOTES: Final = {
    "“": '"',
    "”": '"',
    "„": '"',
    "″": '"',
    "ʺ": '"',
    "‘": "'",
    "’": "'",
    "′": "'",
    "ʹ": "'",
}
_CLAUDE_PAIR: Final = frozenset({"anthropic.claude-opus-5-5", "anthropic.claude-sonnet-5-5"})


def normalise_text(text: str) -> str:
    """Quote glyphs to `"` and `'`, two single quotes to `"`, any whitespace run to one space."""
    for glyph, plain in _QUOTES.items():
        text = text.replace(glyph, plain)
    text = text.replace("''", '"')
    return " ".join(text.split())


class Belongs(StrEnum):
    """A Claude reader's answer to "does this label belong to the marked span?" (#1110)."""

    YES = "yes"
    NO = "no"
    """Sure: the marked span has no printed label of its own here."""
    UNSURE = "unsure"
    """Not sure. Never evidence that the span has no label."""


def parse_belongs(value: object) -> Belongs:
    """A stored or live `belongs` answer: `"yes"`, `"no"` or `"unsure"` (`claude-slot-span-v4`),
    or the boolean of v1 to v3, read as it always was (true is yes, false is no)."""
    if value is True:
        return Belongs.YES
    if value is False:
        return Belongs.NO
    if isinstance(value, str):
        try:
            return Belongs(value)
        except ValueError:
            pass
    raise ValueError(f"belongs must be yes, no or unsure: {value!r}")


@dataclass(frozen=True, slots=True)
class ReaderAnswer:
    """What one reader said about one crop. Only `text` can become a value; the flags only hold
    a reading back."""

    model_id: str
    text: str
    readable: bool
    no_dimension: bool
    stacked: bool
    combined: bool
    belongs: Belongs = Belongs.YES
    """A reader not asked the ownership question (every non-Claude crop reader) counts as yes."""

    def __post_init__(self) -> None:
        # A v1 to v3 caller may still pass the old boolean; it means what it always meant.
        object.__setattr__(self, "belongs", parse_belongs(self.belongs))

    @property
    def usable(self) -> bool:
        return (
            self.belongs is Belongs.YES
            and self.readable
            and not self.no_dimension
            and bool(self.text.strip())
        )

    @property
    def says_no_dimension(self) -> bool:
        """An explicit "nothing to read here": `no_dimension`, or a sure "no" with no text.
        Never an unsure answer."""
        if self.belongs is Belongs.UNSURE:
            return False
        return self.no_dimension or (self.belongs is Belongs.NO and not self.text.strip())


class LabelState(StrEnum):
    SEALED = "sealed"
    """Two different sources gave the identical text, it is a plain dimension, no guard fired."""
    PROVISIONAL = "provisional"
    """The approved Claude pair agrees; workflow still needs a drawn-length witness."""
    NOT_A_DIMENSION = "not_a_dimension"
    """A word, a tag or a symbol: both sources say there is no dimension here."""
    REVIEW = "review"
    """The person decides; `reason` says why."""


@dataclass(frozen=True, slots=True)
class LabelOutcome:
    """What one label run came to."""

    state: LabelState
    value: Measurement | None
    """Only when sealed."""
    suggestion: Measurement | None
    """For the person, from the second reader's text where it is a plain dimension; never where
    the label is on the reviewer's ink, cut by an edge, or crowded."""
    sealed_text: str | None
    """The text both sources agreed on, plain dimension or not (a sealed `4"+1" Filler` is still
    evidence of the word `Filler`)."""
    reason_code: str | None
    reason: str | None
    ink: InkClass | None
    flags: tuple[str, ...]
    reader_texts: tuple[tuple[str, str], ...]
    """Each source's text as given, `(maker, text)`."""


def _is_a_word(label: PlannedLabel) -> bool:
    """Text in the file that is not a dimension: no digit at all, or a cabinet or finish code
    (#868's shapes) — a tag beside the label, evidence of the piece's kind, never its width."""
    text = label.text or ""
    return (
        not label.has_digit
        or is_cabinet_code(text)
        or is_finish_code(text)
        or all(is_cabinet_code(word) or is_finish_code(word) for word in text.split())
    )


def _maker(source: str) -> str:
    if source == TEXT_LAYER:
        return TEXT_LAYER
    return independence_key("bedrock-slot-reader", source)


def seal_label(
    label: PlannedLabel,
    *,
    answers: Sequence[ReaderAnswer],
    ink: InkAt | None,
    stacked_by_bar: bool,
    allow_stacked: bool,
    row_ambiguity: str | None,
    allow_claude_pair: bool = False,
    held_before_reading: str | None = None,
) -> LabelOutcome:
    """Seal one label's reading, or say why the person decides.

    `answers` holds the readers' answers present for this crop: for a text label, the one reader
    shown the crop (the file's text is the other source); for a glyph label, the two readers. A
    reader that abstained is simply absent. Two answers from readers of one maker are refused —
    same-maker agreement never seals (Measurements 2026-10-06: Kimi × 2 agreed on wrong values).
    `ink` is `None` when the page's ink could not be read, which holds a reading back.

    `held_before_reading` is the page's own hold reason when the whole row was held before any
    reader was asked (#1114). It changes only the words of a label no reader answered — `not-asked`
    with that reason, never "only one reader" — and never what is held or sealed.
    """
    sources: list[tuple[str, str, bool]] = []  # (source, text, usable)
    claude_pair = allow_claude_pair and {answer.model_id for answer in answers} == _CLAUDE_PAIR
    if label.lane is Lane.TEXT:
        if len(answers) > 1 and not claude_pair:
            raise ValueError("a text label is read by the file's text and one reader")
        if label.text is None:
            raise ValueError("a text-lane label has the file's text")
        sources.append((TEXT_LAYER, label.text, True))
    elif len(answers) == 2:
        makers = {_maker(answer.model_id) for answer in answers}
        if not claude_pair and (UNKNOWN_MODEL_VENDOR in makers or len(makers) != 2):
            raise ValueError(
                "a glyph label is sealed only by two readers of known, different makers"
            )
    elif len(answers) > 2:
        raise ValueError("a glyph label is read by exactly two readers")
    for answer in answers:
        sources.append((answer.model_id, answer.text, answer.usable))
    reader_texts = tuple((source, text) for source, text, _ in sources)

    flags: list[str] = [f"lane:{label.lane.value}"]
    hard: tuple[str, str] | None = None
    soft: list[tuple[str, str]] = []
    if label.crowded:
        hard = ("crowded", "another label overlaps this one, so it cannot be shown alone")
    if label.touches_edge:
        hard = hard or ("edge", "the label is cut by the drawing's edge")
    found_ink: InkClass | None = None
    if ink is None:
        soft.append(("ink-unchecked", "couldn't check this label for reviewer markup"))
    else:
        found_ink = ink.ink
        if ink.ink is not InkClass.VENDOR:
            reason = REVIEWER_MARKUP_REASON
            if ink.reviewer_text:
                reason = f"{reason}; reviewer wrote {ink.reviewer_text}"
            hard = ("reviewer-markup", reason)
    if row_ambiguity is not None:
        soft.append(("row-ambiguous", f"not sure which row is the countertop: {row_ambiguity}"))
    if label.ambiguous_slot:
        soft.append(("slot-ambiguous", "the label sits between two pieces"))
    stacked_by_code = label.text_stacked or stacked_by_bar
    stacked = stacked_by_code or any(answer.stacked for answer in answers)
    if stacked:
        flags.append("stacked")
        if stacked_by_code:
            flags.append(STACKED_BY_CODE)
        if not allow_stacked:
            soft.append(("stacked", "stacked fraction"))
    if any(answer.combined for answer in answers):
        flags.append("reader-says-combined")

    texts = [normalise_text(text) for _, text, _ in sources]
    # The file's own text saying "a word or a tag" is a fact whatever ink it sits on; two readers
    # saying "no dimension" counts only when both were asked.
    a_word = label.lane is Lane.TEXT and _is_a_word(label)
    unsure = any(answer.belongs is Belongs.UNSURE for answer in answers)
    no_dimension = a_word or (
        len(answers) == 2 and not unsure and all(answer.says_no_dimension for answer in answers)
    )
    agreed: str | None = None
    if len(sources) >= 2 and all(usable for _, _, usable in sources) and len(set(texts)) == 1:
        agreed = texts[0]

    # What the person is shown to check: the last source's text where it is a plain dimension —
    # the second reader's, or the file's own when no reader answered. Never a value.
    second = sources[-1] if sources else None
    suggestion = None
    if hard is None and second is not None and second[2]:
        suggestion = plain_dimension(normalise_text(second[1]), allow_explicit_mm=claude_pair)

    def review(code: str, reason: str, keep_suggestion: bool = True) -> LabelOutcome:
        return LabelOutcome(
            state=LabelState.REVIEW,
            value=None,
            suggestion=suggestion if keep_suggestion else None,
            sealed_text=agreed,
            reason_code=code,
            reason=reason,
            ink=found_ink,
            flags=(*flags, code),
            reader_texts=reader_texts,
        )

    if hard is not None and not a_word:
        return review(*hard, keep_suggestion=False)
    if unsure and not a_word:
        return review("unsure", "a reader was not sure this label belongs to the marked span")
    if no_dimension:
        return LabelOutcome(
            state=LabelState.NOT_A_DIMENSION,
            value=None,
            suggestion=None,
            sealed_text=None,
            reason_code="no-dimension",
            reason="no dimension in this label",
            ink=found_ink,
            flags=(*flags, "no-dimension"),
            reader_texts=reader_texts,
        )
    if soft:
        return review(*soft[0])
    if (
        label.lane is Lane.TEXT
        and not answers
        and plain_dimension(normalise_text(label.text or ""), allow_explicit_mm=claude_pair) is None
    ):
        return review("not-plain", "the label has words or a sum; review the value")
    if not answers and held_before_reading is not None:
        return review(
            "not-asked", f"the readers were not asked; this row waits: {held_before_reading}"
        )
    if not sources:
        # A drawn label no reader answered: never asked, or every reader abstained (#1114).
        return review("not-asked", "no reader read this label")
    if len(sources) < 2:
        return review("one-reader-missing", "only one reader")
    if not all(usable for _, _, usable in sources):
        return review("unreadable", "a reader could not read it clearly")
    if agreed is None:
        return review("readers-differ", "readers differ")
    hold = row_hold([agreed])
    if hold is not None:
        return review(hold.code, hold.reason, keep_suggestion=False)
    if claude_pair and mm_corroborates_inch(agreed) is False:
        return review("mm-inch-disagree", "the label's millimetre and inch halves disagree")
    value = plain_dimension(agreed, allow_explicit_mm=claude_pair)
    if (
        value is not None
        and any(answer.combined for answer in answers)
        and not (claude_pair and _is_explicit_mm_dual(agreed))
    ):
        # A reader saw words or a sum the agreed text does not show: the crop holds more than the
        # label both copied, and which is the piece's width is the person's call.
        value = None
    elif value is None:
        # Words or a sum both sources printed identically: only the two forms the admin approved
        # are expanded, by exact arithmetic on that text (#992). Anything else waits for a person.
        expanded = expand_label(agreed)
        if expanded is not None:
            value = expanded.value
            flags.append(f"expanded:{expanded.how.value}")
    if value is None:
        return review("not-plain", "the label has words or a sum; review the value")
    return LabelOutcome(
        state=LabelState.PROVISIONAL if claude_pair else LabelState.SEALED,
        value=None if claude_pair else value,
        suggestion=value if claude_pair else None,
        sealed_text=agreed,
        reason_code=None,
        reason=None,
        ink=found_ink,
        flags=tuple(flags),
        reader_texts=reader_texts,
    )


#: A metric label with its inch value in brackets, as these vendors print it: `305 [12]`,
#: `38 [1 1/2]`, `18 [3/4]`, with or without `mm` and an inch mark.
_BARE_DUAL: Final = re.compile(
    r"\s*(?P<mm>[0-9]+(?:\.[0-9]+)?)\s*(?:mm)?\s*\[\s*"
    r"(?P<inch>[0-9]+(?:\s+[0-9]+/[0-9]+)?|[0-9]+/[0-9]+)\s*\"?\s*\]\s*",
    re.IGNORECASE,
)
_MM_PER_INCH: Final = Fraction(254, 10)


def _is_explicit_mm_dual(text: str) -> bool:
    """A printed mm [inch] dual label is a supported dimension, not an ambiguous worded label.

    Both forms count: `305 mm [12"]` and the bare `305 [12]` the vendors actually print. Two
    numbers in one label is what makes a reader call it "combined"; for this form that is no reason
    to hold it — the inch half is the value (the client lead's Q12) and the mm half is checked against it.
    """
    from extraction.slot_reader.labels import _MM_DUAL

    return _MM_DUAL.fullmatch(text) is not None or _BARE_DUAL.fullmatch(text) is not None


def mm_corroborates_inch(text: str) -> bool | None:
    """Whether a dual label's mm half agrees with its inch half; `None` if it is not a dual label.

    The inch is authoritative and the mm only corroborates it (CLAUDE.md, the client lead's Q12). The vendor's
    inch is the mm rounded to its own fraction, so the allowance is half that fraction's step plus
    half a millimetre: `562 [22 1/8]` (561.98 mm) agrees, `305 [10]` does not. A disagreement is a
    misread or a vendor slip, and either way the person looks.
    """
    match = _BARE_DUAL.fullmatch(text)
    if match is None:
        return None
    millimetres = Fraction(match.group("mm"))
    inch_text = match.group("inch").split()
    inches = Fraction(0)
    step = Fraction(1)
    for part in inch_text:
        if "/" in part:
            numerator, denominator = part.split("/")
            if int(denominator) == 0:
                return False
            inches += Fraction(int(numerator), int(denominator))
            step = Fraction(1, int(denominator))
        else:
            inches += Fraction(int(part))
    allowance = _MM_PER_INCH * step / 2 + Fraction(1, 2)
    return abs(millimetres - inches * _MM_PER_INCH) <= allowance


@dataclass(frozen=True, slots=True)
class OwnerOutcome:
    """What a slot (or the overall) came to: one sealed value, or why not."""

    state: LabelState
    value: Measurement | None
    label_index: int | None
    """Which of the owner's labels carries the reading; `None` when none does."""
    reason_code: str | None
    reason: str | None


def owner_outcome(outcomes: Sequence[LabelOutcome]) -> OwnerOutcome:
    """One slot's reading from its labels' outcomes — never a choice between two readings.

    No label, or only words: no reading ("no dimension label found"). Exactly one label that may be
    a dimension: its outcome. Two or more: the person decides which is the piece's width, even when
    one of them sealed — choosing would be picking a reading.
    """
    candidates = [
        (index, outcome)
        for index, outcome in enumerate(outcomes)
        if outcome.state is not LabelState.NOT_A_DIMENSION
    ]
    if not candidates:
        return OwnerOutcome(
            LabelState.REVIEW, None, None, "no-label", "no dimension label found for this piece"
        )
    if len(candidates) > 1:
        return OwnerOutcome(
            LabelState.REVIEW, None, None, "two-labels", "more than one label in this piece"
        )
    index, outcome = candidates[0]
    return OwnerOutcome(outcome.state, outcome.value, index, outcome.reason_code, outcome.reason)
