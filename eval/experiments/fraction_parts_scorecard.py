"""The fraction-parts route's scorecard: what it pre-filled, against what people read (#865).

**The bar for switching the route on is 0 wrong pre-fills** (the admin's decision on the plan for
#756, step 3). A pre-fill is a value a person is shown ready to tick, so a wrong one is a value a
person has to catch; this scorecard counts them first, and says plainly whether the bar is met. It
switches nothing on.

**What a pre-fill is judged against**, in this order:

1. **The person's key**, where one of its stacked crops holds the whole label the pre-fill was read
   from — every corner inside the crop. The person read the crop, so a label that runs past its
   edge is a label they saw part of: their value is not this label's, and it judges nothing. That
   is decided from where the two sit on the page, never from either value.
2. **#848's by-eye labels**, where the pre-fill sits on one: its box's centre inside the label's.
3. Otherwise **nothing judges it**, and it is listed as unscored rather than counted right.

**What was not pre-filled** is counted per label a person or the by-eye check read: each by-eye
label, and each crop of the key with a value that no by-eye label overlaps. A label with no
pre-fill is a label a person types — the safe side, and the route's cost.

Every value is compared exactly, as a `Fraction` of an inch. Every box is in the pixel frame of the
run being scored; the key's and the by-eye file's are scaled into it exactly from their own.

Source: issue #865 · Verification: `tests/eval/test_fraction_parts_scorecard.py`
"""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from pathlib import Path

from eval.experiments.agent_scorecard import KeyCrop, Kind, ScorecardError
from units.measurement import Unit
from units.normalise import UnitNormalisationError, normalise_to_inches
from units.notation import canonical_notation

__all__ = [
    "ByEyeLabel",
    "Judged",
    "KeyLabel",
    "Outcome",
    "PreFill",
    "Scorecard",
    "key_labels",
    "load_by_eye",
    "render_markdown",
    "results_json",
    "score",
]

#: A box on one page: left, top, right, bottom, in the run's pixel frame. Exact, because a key's
#: crop scaled from 600 dpi to 300 lands on half pixels.
type Box = tuple[Fraction, Fraction, Fraction, Fraction]


class Outcome(StrEnum):
    RIGHT = "right"
    WRONG = "wrong"
    UNSCORED = "unscored"
    """A pre-fill nothing here judges."""

    NOT_PRE_FILLED = "not pre-filled"
    """A label a person or the by-eye check read, which the route left for a person to type."""


@dataclass(frozen=True, slots=True)
class PreFill:
    """One row the route wrote: where its label sits, what it says, and its exact value."""

    page_index: int
    box: Box
    text: str
    value: Fraction
    """Inches."""


@dataclass(frozen=True, slots=True)
class KeyLabel:
    """One crop of the person's key's stacked group, in the run's frame."""

    crop_id: str
    page_index: int
    box: Box
    expected: Fraction | None
    """The person's value in inches, or `None` where they vouched for none."""

    expected_text: str
    """What the person wrote, or why there is no value."""


@dataclass(frozen=True, slots=True)
class ByEyeLabel:
    """One of #848's labels checked by eye: the label's own box, in the run's frame, and its value."""

    label_id: str
    page_index: int
    box: Box
    expected: Fraction
    expected_text: str


@dataclass(frozen=True, slots=True)
class Judged:
    """One pre-fill, or one label left unread, and what it came to."""

    outcome: Outcome
    page_index: int
    pre_filled: str | None
    expected: str | None
    judged_by: str
    """`key <crop id>`, `by eye <label id>`, or `nothing`."""


@dataclass(frozen=True, slots=True)
class Scorecard:
    judged: tuple[Judged, ...]
    past_the_crop: tuple[str, ...]
    """Key crops with a value whose label runs past their edge: the person saw part of it."""

    conflicts: tuple[str, ...]
    """Labels the person's key and the by-eye check give different values: listed, never resolved."""

    def count(self, outcome: Outcome) -> int:
        return sum(1 for item in self.judged if item.outcome is outcome)

    @property
    def meets_the_bar(self) -> bool:
        """0 wrong pre-fills — the admin's bar for switching the route on."""
        return self.count(Outcome.WRONG) == 0


def _scaled(box: tuple[int, int, int, int], *, source_dpi: int, run_dpi: int) -> Box:
    scale = Fraction(run_dpi, source_dpi)
    left, top, right, bottom = box
    return (left * scale, top * scale, right * scale, bottom * scale)


def _holds(outer: Box, inner: Box) -> bool:
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and inner[2] <= outer[2]
        and inner[3] <= outer[3]
    )


def _overlaps(first: Box, second: Box) -> bool:
    return (
        first[0] < second[2]
        and second[0] < first[2]
        and first[1] < second[3]
        and second[1] < first[3]
    )


def _centre_in(box: Box, outer: Box) -> bool:
    x = (box[0] + box[2]) / 2
    y = (box[1] + box[3]) / 2
    return outer[0] <= x <= outer[2] and outer[1] <= y <= outer[3]


def _inches(text: str, *, where: str) -> Fraction:
    """A value written in inches with its inch mark, as exact inches; refused unless it is one."""
    canonical, millimetres = canonical_notation(text)
    if millimetres is not None or not canonical.rstrip().endswith('"'):
        raise ScorecardError(f'{where}: {text!r} is not written in inches with its inch mark (")')
    try:
        return normalise_to_inches(canonical).exact
    except UnitNormalisationError as error:
        raise ScorecardError(f"{where}: {text!r} is not a value in inches: {error}") from error


def key_labels(crops: Sequence[KeyCrop], *, key_dpi: int, run_dpi: int) -> tuple[KeyLabel, ...]:
    """The key's stacked group — crops picked as stacked fractions or ticked `stacked` — in the run's
    frame, each with the person's value or the reason it has none."""
    labels: list[KeyLabel] = []
    for crop in crops:
        if not crop.stacked:
            continue
        if crop.kind is Kind.SCORED and crop.expected is not None:
            if crop.expected.unit is not Unit.INCH:
                raise ScorecardError(f"crop {crop.crop_id}'s value is not in inches")
            expected: Fraction | None = crop.expected.exact
            text = crop.expected.raw_text or str(crop.expected.exact)
        else:
            expected = None
            text = "cut off" if crop.cut_off else crop.kind.value
        labels.append(
            KeyLabel(
                crop_id=crop.crop_id,
                page_index=crop.page_index,
                box=_scaled(crop.crop_px, source_dpi=key_dpi, run_dpi=run_dpi),
                expected=expected,
                expected_text=text,
            )
        )
    return tuple(labels)


#: The by-eye file's columns: a label, its page (1-based, as the key's are), its box and the frame it
#: is in, and its value as the drawing writes it.
BY_EYE_COLUMNS = ("label_id", "page", "left_px", "top_px", "right_px", "bottom_px", "dpi", "value")


def load_by_eye(path: Path, *, run_dpi: int) -> tuple[ByEyeLabel, ...]:
    """#848's by-eye labels from a CSV kept with the client's data, never in the repository."""
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except OSError as error:
        raise ScorecardError(f"the by-eye labels in {path} could not be read: {error}") from error
    labels: list[ByEyeLabel] = []
    for number, row in enumerate(rows, start=2):
        missing = [column for column in BY_EYE_COLUMNS if not (row.get(column) or "").strip()]
        if missing:
            raise ScorecardError(f"{path} line {number} states no {', '.join(missing)}")
        where = f"{path} line {number}"
        try:
            box = tuple(int(row[name]) for name in ("left_px", "top_px", "right_px", "bottom_px"))
            dpi = int(row["dpi"])
            page = int(row["page"])
        except ValueError as error:
            raise ScorecardError(f"{where}: {error}") from error
        labels.append(
            ByEyeLabel(
                label_id=row["label_id"].strip(),
                page_index=page - 1,
                box=_scaled((box[0], box[1], box[2], box[3]), source_dpi=dpi, run_dpi=run_dpi),
                expected=_inches(row["value"].strip(), where=where),
                expected_text=row["value"].strip(),
            )
        )
    if len({label.label_id for label in labels}) != len(labels):
        raise ScorecardError(f"{path} names a label twice")
    return tuple(labels)


def score(
    pre_fills: Sequence[PreFill],
    key: Sequence[KeyLabel],
    by_eye: Sequence[ByEyeLabel],
) -> Scorecard:
    """Judge every pre-fill, then count every read label left unread. See the module docstring."""
    judged: list[Judged] = []
    past: set[str] = set()
    conflicts: set[str] = set()
    covered_key: set[str] = set()
    covered_eye: set[str] = set()
    for pre_fill in pre_fills:
        on_page = [label for label in key if label.page_index == pre_fill.page_index]
        holding = [
            label
            for label in on_page
            if label.expected is not None and _holds(label.box, pre_fill.box)
        ]
        past.update(
            label.crop_id
            for label in on_page
            if label.expected is not None
            and _overlaps(label.box, pre_fill.box)
            and not _holds(label.box, pre_fill.box)
        )
        eyed = [
            label
            for label in by_eye
            if label.page_index == pre_fill.page_index and _centre_in(pre_fill.box, label.box)
        ]
        covered_eye.update(label.label_id for label in eyed)
        covered_key.update(label.crop_id for label in holding)
        for person in holding:
            for eye in eyed:
                if person.expected != eye.expected:
                    conflicts.add(
                        f"key {person.crop_id} says {person.expected_text}, "
                        f"by eye {eye.label_id} says {eye.expected_text}"
                    )
        judge: tuple[str, Fraction, str] | None = None
        if holding:
            person = holding[0]
            assert person.expected is not None
            judge = (f"key {person.crop_id}", person.expected, person.expected_text)
        elif eyed:
            judge = (f"by eye {eyed[0].label_id}", eyed[0].expected, eyed[0].expected_text)
        if judge is None:
            judged.append(
                Judged(Outcome.UNSCORED, pre_fill.page_index, pre_fill.text, None, "nothing")
            )
            continue
        name, expected, expected_text = judge
        judged.append(
            Judged(
                Outcome.RIGHT if pre_fill.value == expected else Outcome.WRONG,
                pre_fill.page_index,
                pre_fill.text,
                expected_text,
                name,
            )
        )
    for eye in by_eye:
        if eye.label_id not in covered_eye:
            judged.append(
                Judged(
                    Outcome.NOT_PRE_FILLED,
                    eye.page_index,
                    None,
                    eye.expected_text,
                    f"by eye {eye.label_id}",
                )
            )
    for person in key:
        if person.expected is None or person.crop_id in covered_key or person.crop_id in past:
            continue
        # A crop with a value that holds no by-eye label is a label only the key read.
        if any(
            eye.page_index == person.page_index and _overlaps(eye.box, person.box) for eye in by_eye
        ):
            continue
        judged.append(
            Judged(
                Outcome.NOT_PRE_FILLED,
                person.page_index,
                None,
                person.expected_text,
                f"key {person.crop_id}",
            )
        )
    return Scorecard(
        judged=tuple(judged), past_the_crop=tuple(sorted(past)), conflicts=tuple(sorted(conflicts))
    )


def _counted(spend: Mapping[str, object]) -> list[str]:
    return [f"- {name}: {value}" for name, value in spend.items()]


def render_markdown(
    card: Scorecard,
    *,
    header: str,
    refusals: Mapping[str, int],
    spend: Mapping[str, object],
) -> str:
    """The scorecard as a person reads it: the bar first, wrong pre-fills before right ones."""
    wrong = card.count(Outcome.WRONG)
    lines = [
        header,
        "",
        (
            f"**Meets the bar of 0 wrong pre-fills: {'yes' if card.meets_the_bar else 'no'}** "
            f"({wrong} wrong)."
        ),
        "",
        "| outcome | count |",
        "|---|---|",
        *(f"| {outcome.value} | {card.count(outcome)} |" for outcome in Outcome),
        "",
        "| outcome | page | pre-filled | read by a person or by eye | judged by |",
        "|---|---|---|---|---|",
    ]
    order = (Outcome.WRONG, Outcome.UNSCORED, Outcome.RIGHT, Outcome.NOT_PRE_FILLED)
    for outcome in order:
        for item in card.judged:
            if item.outcome is outcome:
                lines.append(
                    f"| {item.outcome.value} | {item.page_index + 1} | {item.pre_filled or '—'} "
                    f"| {item.expected or '—'} | {item.judged_by} |"
                )
    lines += [
        "",
        (
            "**Key crops whose label runs past the crop** (the person read part of it, so their "
            f"value judges nothing): {', '.join(card.past_the_crop) or 'none'}."
        ),
        "",
        f"**Key and by-eye disagreements:** {'; '.join(card.conflicts) or 'none'}.",
        "",
        "**Why the route read no label, as the page results count it:**",
        "",
        *(f"- {count} × {reason}" for reason, count in refusals.items()),
        "",
        "**The second reader's calls:**",
        "",
        *_counted(spend),
        "",
    ]
    return "\n".join(lines)


def results_json(card: Scorecard) -> dict[str, object]:
    return {
        "meets_the_bar": card.meets_the_bar,
        "counts": {outcome.value: card.count(outcome) for outcome in Outcome},
        "judged": [
            {
                "outcome": item.outcome.value,
                "page": item.page_index + 1,
                "pre_filled": item.pre_filled,
                "expected": item.expected,
                "judged_by": item.judged_by,
            }
            for item in card.judged
        ],
        "past_the_crop": list(card.past_the_crop),
        "conflicts": list(card.conflicts),
    }
