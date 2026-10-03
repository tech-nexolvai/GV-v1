"""The fraction-parts scorecard's judging, on boxes and values written here (#865).

Verification for: `eval/experiments/fraction_parts_scorecard.py`. No drawing is read and no model is
called: the run the script makes is the stage's, tested in `tests/workflow/`; what is tested here is
how its pre-fills are judged, and that a wrong one is never counted anything but wrong.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from eval.experiments.agent_scorecard import KeyCrop, Kind, ScorecardError
from eval.experiments.fraction_parts_scorecard import (
    ByEyeLabel,
    KeyLabel,
    Outcome,
    PreFill,
    key_labels,
    load_by_eye,
    render_markdown,
    score,
)
from units.measurement import Measurement, Unit


def _box(left: int, top: int, right: int, bottom: int) -> tuple[Fraction, ...]:
    return (Fraction(left), Fraction(top), Fraction(right), Fraction(bottom))


def _pre_fill(box: tuple[Fraction, ...], value: Fraction, *, page: int = 1) -> PreFill:
    return PreFill(page_index=page, box=box, text=f"{value}", value=value)  # type: ignore[arg-type]


def _key(
    crop_id: str, box: tuple[Fraction, ...], expected: Fraction | None, *, page: int = 1
) -> KeyLabel:
    return KeyLabel(
        crop_id=crop_id,
        page_index=page,
        box=box,  # type: ignore[arg-type]
        expected=expected,
        expected_text="cut off" if expected is None else str(expected),
    )


def _eye(
    label_id: str, box: tuple[Fraction, ...], expected: Fraction, *, page: int = 1
) -> ByEyeLabel:
    return ByEyeLabel(
        label_id=label_id,
        page_index=page,
        box=box,  # type: ignore[arg-type]
        expected=expected,
        expected_text=str(expected),
    )


THREE_QUARTERS = Fraction(3, 4)
TWELVE_AND_A_HALF = Fraction(25, 2)


# ---------------------------------------------------------------------------
# A wrong pre-fill is counted wrong, whoever judges it
# ---------------------------------------------------------------------------


def test_a_pre_fill_the_persons_crop_holds_is_judged_by_the_person() -> None:
    """**The person's value first**, exactly: `3/8"` where they read `3/4"` is wrong, and the bar is
    not met."""
    label = _box(110, 110, 140, 160)
    card = score(
        [_pre_fill(label, Fraction(3, 8))],
        [_key("c1", _box(100, 100, 200, 200), THREE_QUARTERS)],
        [_eye("l1", label, THREE_QUARTERS)],
    )

    (judged,) = card.judged
    assert judged.outcome is Outcome.WRONG
    assert judged.judged_by == "key c1"
    assert card.meets_the_bar is False


def test_the_same_value_is_right_and_meets_the_bar() -> None:
    label = _box(110, 110, 140, 160)
    card = score(
        [_pre_fill(label, THREE_QUARTERS)],
        [_key("c1", _box(100, 100, 200, 200), THREE_QUARTERS)],
        [],
    )

    assert [item.outcome for item in card.judged] == [Outcome.RIGHT]
    assert card.meets_the_bar is True


def test_a_crop_the_label_runs_past_judges_nothing() -> None:
    """**The person saw part of the label.** Their `2 1/2"` for a crop that cuts `12 1/2"` is not this
    label's value: the crop is listed, and the by-eye label judges the pre-fill instead."""
    label = _box(90, 110, 160, 160)
    card = score(
        [_pre_fill(label, TWELVE_AND_A_HALF)],
        [_key("c2", _box(100, 100, 200, 200), Fraction(5, 2))],
        [_eye("l0", label, TWELVE_AND_A_HALF)],
    )

    (judged,) = card.judged
    assert (judged.outcome, judged.judged_by) == (Outcome.RIGHT, "by eye l0")
    assert card.past_the_crop == ("c2",)


def test_a_pre_fill_nothing_judges_is_unscored_never_right() -> None:
    card = score([_pre_fill(_box(500, 500, 520, 540), THREE_QUARTERS)], [], [])

    assert [item.outcome for item in card.judged] == [Outcome.UNSCORED]
    assert card.meets_the_bar is True, "unscored is reported beside the bar, not counted wrong"
    assert "| unscored | 1 |" in render_markdown(card, header="h", refusals={}, spend={})


def test_a_crop_the_person_vouched_no_value_for_judges_nothing() -> None:
    label = _box(110, 110, 140, 160)
    card = score(
        [_pre_fill(label, THREE_QUARTERS)], [_key("c0", _box(100, 100, 200, 200), None)], []
    )

    assert [item.outcome for item in card.judged] == [Outcome.UNSCORED]


def test_a_pre_fill_on_another_page_is_not_judged_by_this_one() -> None:
    label = _box(110, 110, 140, 160)
    card = score(
        [_pre_fill(label, Fraction(3, 8), page=2)],
        [_key("c1", _box(100, 100, 200, 200), THREE_QUARTERS, page=1)],
        [_eye("l1", label, THREE_QUARTERS, page=1)],
    )

    assert [item.outcome for item in card.judged[:1]] == [Outcome.UNSCORED]


def test_a_disagreement_between_the_key_and_the_eye_is_listed_never_resolved() -> None:
    label = _box(110, 110, 140, 160)
    card = score(
        [_pre_fill(label, THREE_QUARTERS)],
        [_key("c1", _box(100, 100, 200, 200), THREE_QUARTERS)],
        [_eye("l1", label, Fraction(1, 4))],
    )

    assert card.conflicts == ("key c1 says 3/4, by eye l1 says 1/4",)
    assert card.judged[0].judged_by == "key c1"


# ---------------------------------------------------------------------------
# What the route left for a person
# ---------------------------------------------------------------------------


def test_every_read_label_left_unread_is_counted_once() -> None:
    """A by-eye label with no pre-fill, and a key crop with a value that no by-eye label overlaps; a
    key crop on a by-eye label is that label, and a crop with no value is no label anyone read."""
    card = score(
        [],
        [
            _key("c1", _box(100, 100, 200, 200), THREE_QUARTERS),  # on l1
            _key("c9", _box(100, 100, 200, 200), Fraction(3, 2), page=14),  # nothing by eye
            _key("c0", _box(300, 300, 400, 400), None),  # cut off
        ],
        [_eye("l1", _box(110, 110, 140, 160), THREE_QUARTERS)],
    )

    assert sorted(item.judged_by for item in card.judged) == ["by eye l1", "key c9"]
    assert {item.outcome for item in card.judged} == {Outcome.NOT_PRE_FILLED}
    assert card.meets_the_bar is True


def test_the_markdown_puts_the_bar_and_the_wrong_pre_fills_first() -> None:
    label = _box(110, 110, 140, 160)
    card = score(
        [_pre_fill(_box(500, 500, 520, 540), THREE_QUARTERS), _pre_fill(label, Fraction(3, 8))],
        [_key("c1", _box(100, 100, 200, 200), THREE_QUARTERS)],
        [],
    )

    text = render_markdown(
        card, header="## h", refusals={"a reason": 2}, spend={"spent (USD)": "0.001"}
    )

    assert "**Meets the bar of 0 wrong pre-fills: no** (1 wrong)." in text
    lines = text.splitlines()
    table = lines[lines.index("|---|---|---|---|---|") + 1 :]
    assert table[0].startswith("| wrong | 2 | 3/8 |"), "the wrong pre-fill is listed first"
    assert table[1].startswith("| unscored |")
    assert "- 2 × a reason" in text and "- spent (USD): 0.001" in text


# ---------------------------------------------------------------------------
# The two kinds of truth, read in
# ---------------------------------------------------------------------------


def _crop(crop_id: str, *, stacked: bool, kind: Kind, expected: str | None) -> KeyCrop:
    return KeyCrop(
        crop_id=crop_id,
        page_index=1,
        crop_px=(200, 400, 371, 564),
        kind=kind,
        expected=(
            None
            if expected is None
            else Measurement(exact=Fraction(expected), unit=Unit.INCH, raw_text=f'{expected}"')
        ),
        stratum="stacked_fraction" if stacked else "dimension_label",
        rotated=False,
        cut_off=kind is Kind.UNREADABLE,
        stacked=stacked,
    )


def test_the_key_gives_its_stacked_group_scaled_exactly_into_the_runs_frame() -> None:
    """600 dpi into 300 lands on half pixels, and they are kept, not rounded."""
    labels = key_labels(
        [
            _crop("c1", stacked=True, kind=Kind.SCORED, expected="3/4"),
            _crop("c0", stacked=True, kind=Kind.UNREADABLE, expected=None),
            _crop("c4", stacked=False, kind=Kind.SCORED, expected="2"),
        ],
        key_dpi=600,
        run_dpi=300,
    )

    assert [label.crop_id for label in labels] == ["c1", "c0"]
    assert labels[0].box == (Fraction(100), Fraction(200), Fraction(371, 2), Fraction(282))
    assert labels[0].expected == THREE_QUARTERS
    assert (labels[1].expected, labels[1].expected_text) == (None, "cut off")


def _by_eye(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "by_eye.csv"
    path.write_text(
        "label_id,page,left_px,top_px,right_px,bottom_px,dpi,value\n" + "".join(rows),
        encoding="utf-8",
    )
    return path


def test_the_by_eye_labels_are_read_exactly(tmp_path: Path) -> None:
    path = _by_eye(tmp_path, 'l0,2,100,200,170,250,300,"12 1/2"""\n')

    (label,) = load_by_eye(path, run_dpi=300)

    assert (label.page_index, label.expected) == (1, TWELVE_AND_A_HALF)
    assert label.box == _box(100, 200, 170, 250)


@pytest.mark.parametrize(
    "row",
    [
        "l0,2,100,200,170,250,300,\n",  # no value
        'l0,2,100,200,170,250,,"12 1/2"""\n',  # no frame
        "l0,2,100,200,170,250,300,12 1/2\n",  # no inch mark
        "l0,2,100,200,170,250,300,333 mm\n",  # not inches
        'l0,2,100.5,200,170,250,300,"12 1/2"""\n',  # not a pixel
    ],
)
def test_a_by_eye_row_that_does_not_state_everything_exactly_is_refused(
    tmp_path: Path, row: str
) -> None:
    with pytest.raises(ScorecardError):
        load_by_eye(_by_eye(tmp_path, row), run_dpi=300)


def test_a_by_eye_label_named_twice_is_refused(tmp_path: Path) -> None:
    row = 'l0,2,100,200,170,250,300,"12 1/2"""\n'
    with pytest.raises(ScorecardError, match="twice"):
        load_by_eye(_by_eye(tmp_path, row, row), run_dpi=300)
