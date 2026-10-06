"""When a label's reading counts, and what it is worth (#987).

**Two different sources, identical text, or a person** (plan rule). A label that is real text in the
file is sealed by the file's own text and one reader on the crop agreeing character for character;
a label drawn as paths, by two readers of different makers agreeing on the crop. Agreement is on
the printed text after only the normalisation a picture cannot carry — curly or prime quote glyphs,
a line break, a run of spaces. Nothing else is forgiven: `13 1/8"` and `131/8"` are different texts.

**The value comes from the sealed text alone** (E2 guard 2), parsed by `units/`. The readers are not
asked for whole, numerator and denominator at all: in E2 both readers once filed identical wrong
parts for a label whose text they had read right. A sealed text that is not a plain dimension —
words, a sum, a count, brackets that are not an mm [inch] pair — is held for the person, because
`units/` would read `181"(10EQ)` as `181"` and drop the rest.

**Every guard only withholds.** The reviewer's ink on the crop (E2 guard 1, decided by code's colour
check, never the readers'), a label at the drawing's edge (E2 guard 3), a stacked fraction (#762),
a crowded crop, an uncertain slot or row, a reader that abstained or was unsure: each sends the
reading to the person with a plain reason. None of them can make a reading count.

Source: issue #987 · Verification: `tests/extraction/slot_reader/test_seal.py`
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from evidence.corroborate import UNKNOWN_MODEL_VENDOR, independence_key
from extraction.form_reader.mapping import REVIEWER_MARKUP_REASON
from extraction.ink import InkAt, InkClass
from extraction.slot_reader.runs import Lane, PlannedLabel
from units.measurement import Measurement
from units.normalise import UnitNormalisationError, normalise_to_inches
from units.notation import canonical_notation, is_compound
from vocabulary.cabinet_codes import is_cabinet_code, is_finish_code

__all__ = [
    "TEXT_LAYER",
    "LabelOutcome",
    "LabelState",
    "OwnerOutcome",
    "ReaderAnswer",
    "normalise_text",
    "owner_outcome",
    "plain_dimension",
    "seal_label",
]

#: The file's own text, as a source. Its "maker" is the drawing, which no model shares.
TEXT_LAYER: Final = "pdf-text-layer"

#: A plain dimension: digits, spaces, a fraction slash, inch and foot marks, a dash, a decimal
#: point, and the brackets of an mm [inch] pair. Anything else is words, a sum or a count.
_PLAIN: Final = re.compile(r"[0-9 /\"'\-.\[\]]+")

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


def normalise_text(text: str) -> str:
    """Quote glyphs to `"` and `'`, two single quotes to `"`, any whitespace run to one space."""
    for glyph, plain in _QUOTES.items():
        text = text.replace(glyph, plain)
    text = text.replace("''", '"')
    return " ".join(text.split())


def plain_dimension(text: str) -> Measurement | None:
    """The exact value of a plain dimension's text, or `None` where it is not one.

    `None` for text with anything but the characters of a dimension, for a compound, and for text
    `units/` cannot give a unit — a bare `30` is not assumed to be inches.
    """
    if not text or _PLAIN.fullmatch(text) is None or is_compound(text):
        return None
    try:
        notation, _millimetres = canonical_notation(text)
        value = normalise_to_inches(notation)
    except (UnitNormalisationError, TypeError, ValueError, ArithmeticError):
        return None
    return Measurement(value.exact, value.unit, text)


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

    @property
    def usable(self) -> bool:
        return self.readable and not self.no_dimension and bool(self.text.strip())


class LabelState(StrEnum):
    SEALED = "sealed"
    """Two different sources gave the identical text, it is a plain dimension, no guard fired."""
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
    """The text both sources agreed on, plain dimension or not (a sealed `3"+2" Filler` is still
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
) -> LabelOutcome:
    """Seal one label's reading, or say why the person decides.

    `answers` holds the readers' answers present for this crop: for a text label, the one reader
    shown the crop (the file's text is the other source); for a glyph label, the two readers. A
    reader that abstained is simply absent. Two answers from readers of one maker are refused —
    same-maker agreement never seals (Measurements 2026-10-06: Kimi × 2 agreed on wrong values).
    `ink` is `None` when the page's ink could not be read, which holds a reading back.
    """
    sources: list[tuple[str, str, bool]] = []  # (source, text, usable)
    if label.lane is Lane.TEXT:
        if len(answers) > 1:
            raise ValueError("a text label is read by the file's text and one reader")
        if label.text is None:
            raise ValueError("a text-lane label has the file's text")
        sources.append((TEXT_LAYER, label.text, True))
    elif len(answers) == 2:
        makers = {_maker(answer.model_id) for answer in answers}
        if UNKNOWN_MODEL_VENDOR in makers or len(makers) != 2:
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
    stacked = label.text_stacked or stacked_by_bar or any(answer.stacked for answer in answers)
    if stacked:
        flags.append("stacked")
        if not allow_stacked:
            soft.append(("stacked", "stacked fraction"))
    if any(answer.combined for answer in answers):
        flags.append("reader-says-combined")

    texts = [normalise_text(text) for _, text, _ in sources]
    # The file's own text saying "a word or a tag" is a fact whatever ink it sits on; two readers
    # saying "no dimension" counts only when both were asked.
    a_word = label.lane is Lane.TEXT and _is_a_word(label)
    no_dimension = a_word or (len(answers) == 2 and all(answer.no_dimension for answer in answers))
    agreed: str | None = None
    if len(sources) == 2 and all(usable for _, _, usable in sources) and texts[0] == texts[1]:
        agreed = texts[0]

    # What the person is shown to check: the last source's text where it is a plain dimension —
    # the second reader's, or the file's own when no reader answered. Never a value.
    second = sources[-1] if sources else None
    suggestion = None
    if hard is None and second is not None and second[2]:
        suggestion = plain_dimension(normalise_text(second[1]))

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
        and plain_dimension(normalise_text(label.text or "")) is None
    ):
        return review("not-plain", "the label has words or a sum; review the value")
    if len(sources) < 2:
        return review("one-reader-missing", "only one reader")
    if not all(usable for _, _, usable in sources):
        return review("unreadable", "a reader could not read it clearly")
    if agreed is None:
        return review("readers-differ", "readers differ")
    value = plain_dimension(agreed)
    if value is None or any(answer.combined for answer in answers):
        return review("not-plain", "the label has words or a sum; review the value")
    return LabelOutcome(
        state=LabelState.SEALED,
        value=value,
        suggestion=None,
        sealed_text=agreed,
        reason_code=None,
        reason=None,
        ink=found_ink,
        flags=tuple(flags),
        reader_texts=reader_texts,
    )


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
