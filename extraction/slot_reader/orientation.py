"""Which way a sideways label reads, decided only where exactly one way fits (rebuild plan 2B).

**The danger.** A number drawn up or down the page is shown to the readers turned upright. Code can
tell that a label is sideways, but not always whether it reads bottom to top or top to bottom
(`extraction/geometry/labels.py` says 90 for every vertical label; the assistant's
`ink_labels.turns` gives both 90 and 270 when its strokes do not tell). Turned the wrong way, a
`68"` reads `89"`, and two readers from different makers both read `89"`: their agreement proves
nothing about the turn. Only the drawing can: shop drawings are drawn to scale, so the right turn's
value lands on its drawn length and the wrong turn's (almost always) does not.

**The rule** (the plan's five steps, as code):

1. Each turn tried is sealed on its own (`seal.seal_label`: identical text from two readers of
   different makers, a plain dimension, no guard fired). Two readers of one maker never seal: Opus
   and Sonnet are one maker, as `seal._maker` decides.
2. Each sealed value is checked against its drawn length at its view's scale
   (`view_scale.view_scale_check`); a vetoed turn does not fit.
3. **A sideways label with no scale is never sealed** ("no scale for a sideways label"): nothing
   could then catch the upside-down read.
4. Two turns that fit with **different** values: held for a person ("both orientations fit"). Two
   turns that fit with the **same** value (a rotation-symmetric `96"`) are one reading, counted
   once, at the first turn tried.
5. Exactly one value fits: sealed, `sealed_witnessed` when the scale agreed, else (an upright label
   with no scale) `sealed_unwitnessed`, which elsewhere is never more than a please-check.

A stacked fraction never seals here: `allow_stacked` is always off (#762), and code's own stacked
finding is passed as the fraction-bar flag. Every other guard of `seal_label` stands as it is.

**What it never does.** It reads nothing and picks no value between readers or between turns: it
only says which single turn's sealed value the drawing accepts, or why none counts.

Source: rebuild plan v1 (2026-11-05) section 5, 2B · Verification:
`tests/extraction/slot_reader/test_orientation.py`
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Final

from evidence.corroborate import UNKNOWN_MODEL_VENDOR
from extraction.ink import InkAt
from extraction.slot_reader.runs import PlannedLabel
from extraction.slot_reader.seal import (
    LabelOutcome,
    LabelState,
    ReaderAnswer,
    _maker,
    seal_label,
)
from extraction.slot_reader.veto import DRAWN_LENGTH_REASON
from extraction.slot_reader.view_scale import ScaleVerdict, view_scale_check
from units.measurement import Unit

__all__ = [
    "BOTH_FIT",
    "NO_SCALE_SIDEWAYS",
    "ONE_MAKER",
    "TURNS",
    "GuardOutcome",
    "GuardState",
    "TurnReading",
    "decide_turn",
    "read_turn",
]

#: The turns a crop may be shown at: the picture turned clockwise by this many degrees (as
#: `app/review/assistant/ink_labels.Turn`: 90 reads up the page, 270 down it).
TURNS: Final = (0, 90, 270)
BOTH_FIT: Final = "both orientations fit"
NO_SCALE_SIDEWAYS: Final = "no scale for a sideways label"
ONE_MAKER: Final = "the two readers are of one maker; same-maker agreement never seals"
_NOT_READ: Final = "no reader read this label"


class GuardState(StrEnum):
    """What one label's reading came to."""

    SEALED_WITNESSED = "sealed_witnessed"
    """Two makers printed the same text at exactly one turn, and its drawn length agrees."""
    SEALED_UNWITNESSED = "sealed_unwitnessed"
    """An upright label two makers printed the same, with no scale to check it."""
    HELD = "held"
    """Two makers agreed, but the drawing does not settle it (both turns fit, or neither does)."""
    UNREAD = "unread"
    """No agreed reading counts (readers differ, a guard fired, or no scale for a sideways one)."""


@dataclass(frozen=True, slots=True)
class TurnReading:
    """One turn's sealed outcome and, where it sealed a value in inches, the scale's verdict."""

    turn: int
    outcome: LabelOutcome
    inches: Fraction | None
    """The sealed value in inches; `None` when the turn did not seal (or sealed no inch value)."""
    scale: ScaleVerdict | None
    """The view-scale check of `inches`; `None` exactly when `inches` is."""

    def __post_init__(self) -> None:
        if self.turn not in TURNS:
            raise ValueError(f"a turn is one of {TURNS}: {self.turn!r}")
        if (self.inches is None) != (self.scale is None):
            raise ValueError("a turn has a scale verdict exactly when it sealed a value")


@dataclass(frozen=True, slots=True)
class GuardOutcome:
    """The label's state, why (plain words, `None` when sealed), and the one sealed reading."""

    state: GuardState
    why: str | None
    turn: int | None
    text: str | None
    """The sealed text, normalised as `seal.normalise_text` compares it."""
    inches: Fraction | None


def _one_maker(answers: Sequence[ReaderAnswer]) -> bool:
    makers = {_maker(answer.model_id) for answer in answers}
    return len(answers) == 2 and (UNKNOWN_MODEL_VENDOR in makers or len(makers) != 2)


def read_turn(
    label: PlannedLabel,
    turn: int,
    answers: Sequence[ReaderAnswer],
    *,
    ink: InkAt | None,
    stacked_by_code: bool,
    drawn: Fraction | None,
    scale: Fraction | None,
) -> TurnReading:
    """One turn: `seal_label` on the readers' answers at that turn (stacked fractions never allowed,
    no Claude-pair exception), then the view-scale check of a sealed inch value.

    Two answers of one maker are not sealed (`ONE_MAKER`) instead of being refused with an error, as
    `seal_label` refuses them: here they are a reading that does not count, never a crash."""
    if len(answers) > 2:
        raise ValueError("a drawn label is read by at most two readers")
    if _one_maker(answers):
        outcome = LabelOutcome(
            state=LabelState.REVIEW,
            value=None,
            suggestion=None,
            sealed_text=None,
            reason_code="one-maker",
            reason=ONE_MAKER,
            ink=None if ink is None else ink.ink,
            flags=("one-maker",),
            reader_texts=tuple((answer.model_id, answer.text) for answer in answers),
        )
        return TurnReading(turn, outcome, None, None)
    outcome = seal_label(
        label,
        answers=answers,
        ink=ink,
        stacked_by_bar=stacked_by_code,
        allow_stacked=False,
        row_ambiguity=None,
    )
    value = outcome.value
    if outcome.state is not LabelState.SEALED or value is None or value.unit is not Unit.INCH:
        # Millimetres never decide (Q12): a value that is not in inches is not a sealed reading.
        return TurnReading(turn, outcome, None, None)
    return TurnReading(turn, outcome, value.exact, view_scale_check(value.exact, drawn, scale))


def decide_turn(readings: Sequence[TurnReading], *, sideways: bool) -> GuardOutcome:
    """The label's state from every turn tried, in the order they were tried (see the module)."""
    if not readings:
        return GuardOutcome(GuardState.UNREAD, _NOT_READ, None, None, None)
    if len({reading.turn for reading in readings}) != len(readings):
        raise ValueError("each turn is tried once")
    if not sideways and any(reading.turn != 0 for reading in readings):
        raise ValueError("an upright label is read upright only")
    sealed = [r for r in readings if r.inches is not None]
    fits = [r for r in sealed if r.scale is not ScaleVerdict.VETOED]
    if not fits:
        if sealed:
            return GuardOutcome(GuardState.HELD, DRAWN_LENGTH_REASON, None, None, None)
        why = next((r.outcome.reason for r in readings if r.outcome.reason), None)
        return GuardOutcome(GuardState.UNREAD, why or _NOT_READ, None, None, None)
    if sideways and any(r.scale is ScaleVerdict.NO_SCALE for r in fits):
        return GuardOutcome(GuardState.UNREAD, NO_SCALE_SIDEWAYS, None, None, None)
    if len({r.inches for r in fits}) > 1:
        return GuardOutcome(GuardState.HELD, BOTH_FIT, None, None, None)
    chosen = fits[0]
    state = (
        GuardState.SEALED_WITNESSED
        if chosen.scale is ScaleVerdict.WITNESSED
        else GuardState.SEALED_UNWITNESSED
    )
    return GuardOutcome(state, None, chosen.turn, chosen.outcome.sealed_text, chosen.inches)
