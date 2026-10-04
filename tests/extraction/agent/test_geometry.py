"""Whether a crop cut a label off, and which way the label runs, from the file's paths (#757, #918).

Verification for: `extraction/agent/geometry.py`.

The one that matters most is `test_a_crop_that_cuts_the_first_digit_off_is_cut`: the #641 failure —
a three-digit label cropped to its last two — seen from the paths alone, before any reader looks.

The labels are drawn in the made-up font of `tests/extraction/test_glyph_reader.py`; no client
drawing is read.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from extraction.agent.geometry import (
    BOTH_WAYS,
    NO_PATHS,
    NO_RUN,
    NOT_CLOSED,
    Box,
    LabelReach,
    label_direction,
    label_geometry,
)
from extraction.annotations import VectorPath
from tests.extraction.test_glyph_reader import _fraction, _row, _turned

REACH = LabelReach(label_gap_pt=Decimal(4), maximum_label_pt=Decimal(80), glyph_gap_pt=Decimal(4))


def _box(paths: list[VectorPath]) -> Box:
    xs = [x for path in paths for x, _ in path.points]
    ys = [y for path in paths for _, y in path.points]
    return (min(xs), min(ys), max(xs), max(ys))


def _grown(box: Box, by: int) -> Box:
    return (box[0] - by, box[1] - by, box[2] + by, box[3] + by)


# ---------------------------------------------------------------------------
# Cut off
# ---------------------------------------------------------------------------


def test_a_crop_round_the_whole_label_is_not_cut() -> None:
    paths, _ = _row('192"', 0, 0)
    region = _box(paths)

    geometry = label_geometry(region, _grown(region, 2), paths, REACH)

    assert geometry.label_box == region
    assert geometry.closed and not geometry.cut_at_edge
    assert geometry.rotation_degrees == 0


def test_a_crop_that_cuts_the_first_digit_off_is_cut() -> None:
    """**The #641 failure.** Outcome: the region holds `92"`, the label is `192"`; the crop round the
    region cuts it, and the label's whole run reaches back to the `1`."""
    paths, _ = _row('192"', 0, 0)
    region = _box(paths[1:])

    geometry = label_geometry(region, _grown(region, 2), paths, REACH)

    assert geometry.cut_at_edge
    assert geometry.label_box == _box(paths)


def test_a_label_touching_the_crop_edge_is_cut() -> None:
    """Outcome: the points meet the edge, so half the stroke is outside it."""
    paths, _ = _row("12", 0, 0)
    region = _box(paths)

    assert label_geometry(region, region, paths, REACH).cut_at_edge


def test_a_label_that_runs_into_more_text_is_unclosed() -> None:
    """Outcome: sixteen characters half a point apart run for over 100 pt — more than one label
    holds — so where it ends is not settled, and a crop round one piece of it is cut."""
    paths, _ = _row("8888888888888888", 0, 0)
    region = _box(paths[:2])

    geometry = label_geometry(region, _grown(region, 2), paths, REACH)

    assert not geometry.closed
    assert geometry.cut_at_edge


def test_a_region_with_no_paths_says_nothing() -> None:
    """Outcome: a label drawn as font text (#738) is not seen, and nothing is claimed about it."""
    paths, _ = _row("12", 0, 0)
    far: Box = (Decimal(500), Decimal(500), Decimal(510), Decimal(510))

    geometry = label_geometry(far, _grown(far, 2), paths, REACH)

    assert geometry.label_box is None
    assert not geometry.cut_at_edge and geometry.rotation_degrees == 0


def test_a_neighbouring_label_beyond_the_gap_is_not_gathered() -> None:
    near, _ = _row("12", 0, 0)
    other, _ = _row("34", 30, 0)
    region = _box(near)

    geometry = label_geometry(region, _grown(region, 2), near + other, REACH)

    assert geometry.label_box == region and not geometry.cut_at_edge


# ---------------------------------------------------------------------------
# Which way it runs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("degrees", [90, 270])
def test_a_label_running_up_or_down_the_page_is_turned_by_the_convention(degrees: int) -> None:
    """Outcome: 90 either way — a box is the same either way up, so the drafting convention decides
    (the module docstring says what happens where a drawing breaks it)."""
    paths, _ = _row('45"', 0, 0)
    turned = _turned(paths, degrees)
    region = _box(turned)

    geometry = label_geometry(region, _grown(region, 2), turned, REACH)

    assert geometry.rotation_degrees == 90


def test_one_character_has_no_direction() -> None:
    paths, _ = _row("1", 0, 0)
    turned = _turned(paths, 90)
    region = _box(turned)

    assert label_geometry(region, _grown(region, 2), turned, REACH).rotation_degrees == 0


def test_a_stacked_fraction_is_read_as_it_stands() -> None:
    """Outcome: the numerator above the denominator is a column, but the whole number beside them is
    a row, so the label has runs both ways and is not called sideways."""
    paths, _ = _fraction("1", "3", "4")
    region = _box(paths)

    assert label_geometry(region, _grown(region, 2), paths, REACH).rotation_degrees == 0


def test_reach_refuses_a_float_or_a_missing_length() -> None:
    with pytest.raises(TypeError):
        LabelReach(label_gap_pt=4.0, maximum_label_pt=Decimal(80), glyph_gap_pt=Decimal(4))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        LabelReach(label_gap_pt=Decimal(0), maximum_label_pt=Decimal(80), glyph_gap_pt=Decimal(4))


def test_leaving_out_characters_too_far_to_join_changes_no_result() -> None:
    """Outcome: on a sheet of labels, one long unclosed run among them, every region's label is the
    one `gather_label` finds over the whole sheet — the shortcut only saves the scan."""
    from extraction.glyph_reader import gather_label

    sheet: list[VectorPath] = []
    for row in range(4):
        for column in range(4):
            paths, _ = _row('192"', column * 40, row * 20, scale=0.9)
            sheet += paths
    long_run, _ = _row("8888888888888888", 0, 100)
    sheet += long_run

    for region in [_box(sheet[:1]), _box(sheet[9:10]), _box(long_run[3:4]), _box(sheet[-40:-38])]:
        seeds = [
            path
            for path in sheet
            if _box([path])[0] <= region[2]
            and region[0] <= _box([path])[2]
            and _box([path])[1] <= region[3]
            and region[1] <= _box([path])[3]
        ]
        members, unclosed = gather_label(seeds, sheet, settings=REACH)

        geometry = label_geometry(region, _grown(region, 2), sheet, REACH)

        assert geometry.label_box == _box(list(members))
        assert geometry.closed is (unclosed is None)


def test_a_character_past_the_labels_end_is_seen_when_the_run_gap_is_the_wider_one() -> None:
    """**The shortcut keeps up with `gather_label` (#756, #773).** A label nearly as long as a label
    may be, and one more character on its line 3.6 pt past its end — inside the 4 pt run gap,
    outside the 2 pt label gap. Outcome: the end is not settled, as `gather_label` over the whole
    sheet says. Before the window grew by the wider gap, the shortcut left that character out and
    called the label settled, so the agent would have widened to a label that may go on."""
    from extraction.glyph_reader import gather_label

    label, _ = _row("8888", 4, 0)
    tail, _ = _row("8", 33.1, 0)
    sheet = label + tail
    reach = LabelReach(
        label_gap_pt=Decimal(2), maximum_label_pt=Decimal(26), glyph_gap_pt=Decimal(4)
    )
    region: Box = (Decimal(0), Decimal(0), Decimal(5), Decimal(10))
    _, unclosed = gather_label([label[0]], sheet, settings=reach)

    geometry = label_geometry(region, _grown(region, 9), sheet, reach)

    assert unclosed is not None
    assert not geometry.closed


# ---------------------------------------------------------------------------
# Which way it runs: the longest run, on a settled label (#783)
# ---------------------------------------------------------------------------


def test_a_turned_label_with_pieces_side_by_side_is_still_sideways() -> None:
    """**The labels the old rule missed.** A two-line label — `102` above `[4]`, the shape of the
    client's millimetre-and-inch labels — turned to read up the page puts the two lines side by side,
    so some characters do form a run across. Outcome: sideways, because its longest run goes up."""
    top, _ = _row("102", 0, 12)
    bottom, _ = _row("[4]", 3, 0)
    turned = _turned(top + bottom, 90)
    region = _box(turned)

    geometry = label_geometry(region, _grown(region, 2), turned, REACH)

    assert geometry.closed
    assert geometry.rotation_degrees == 90


def test_the_same_label_upright_reads_as_it_stands() -> None:
    top, _ = _row("102", 0, 12)
    bottom, _ = _row("[4]", 3, 0)
    region = _box(top + bottom)

    assert label_geometry(region, _grown(region, 2), top + bottom, REACH).rotation_degrees == 0


def test_an_unsettled_label_has_no_direction() -> None:
    """**The #778 false alarm.** Characters stacked up the page for longer than one label may run
    leave where the label ends unsettled. Outcome: no direction — a "label" whose extent is not
    settled has no settled direction either."""
    column, _ = _row("8888888888888888", 0, 0)
    turned = _turned(column, 90)
    region = _box(turned[:2])

    geometry = label_geometry(region, _grown(region, 2), turned, REACH)

    assert not geometry.closed
    assert geometry.rotation_degrees == 0


# ---------------------------------------------------------------------------
# Which way it runs, where a wrong answer costs more than none (#918)
# ---------------------------------------------------------------------------


def test_a_label_on_one_line_across_the_page_runs_across() -> None:
    """Outcome: 0, with the label's extent and the extent of the characters in the region."""
    paths, _ = _row('12"', 0, 0)
    region = _box(paths[:1])

    found = label_direction(region, paths, REACH)

    assert (found.degrees, found.unsettled) == (0, None)
    assert found.label_box == _box(paths)
    assert found.in_region == region


@pytest.mark.parametrize("degrees", [90, 270])
def test_a_label_on_one_line_up_or_down_the_page_runs_up(degrees: int) -> None:
    """Outcome: 90 either way, the drafting convention `label_geometry` turns a crop by."""
    turned = _turned(_row('45"', 0, 0)[0], degrees)

    assert label_direction(_box(turned), turned, REACH).degrees == 90


def test_a_stacked_fraction_has_no_direction() -> None:
    """**Where the longest-run rule turns a crop, this one refuses.** The numerator above the
    denominator is a run up the page beside the whole number's run across it. Outcome: no
    direction, and why — a stacked label's runs say nothing about which way it reads."""
    paths, _ = _fraction("1", "3", "4")

    found = label_direction(_box(paths), paths, REACH)

    assert found.degrees is None and found.unsettled == BOTH_WAYS


def test_a_two_line_label_turned_sideways_has_no_direction() -> None:
    """**The label the longest-run rule calls sideways** (`test_a_turned_label_with_pieces_side_by_
    side_is_still_sideways`): its two lines side by side make runs across as well as up. Outcome:
    turned for a reader by `label_geometry`, but no direction to attach it by."""
    top, _ = _row("102", 0, 12)
    bottom, _ = _row("[4]", 3, 0)
    turned = _turned(top + bottom, 90)
    region = _box(turned)

    assert label_geometry(region, _grown(region, 2), turned, REACH).rotation_degrees == 90
    found = label_direction(region, turned, REACH)
    assert found.degrees is None and found.unsettled == BOTH_WAYS


def test_the_same_two_line_label_upright_has_no_direction_either() -> None:
    top, _ = _row("102", 0, 12)
    bottom, _ = _row("[4]", 3, 0)

    found = label_direction(_box(top + bottom), top + bottom, REACH)

    assert found.degrees is None and found.unsettled == BOTH_WAYS


def test_one_character_shows_no_direction() -> None:
    paths, _ = _row("1", 0, 0)

    found = label_direction(_box(paths), paths, REACH)

    assert found.degrees is None and found.unsettled == NO_RUN


def test_an_unsettled_label_has_no_direction_to_attach_by() -> None:
    """Outcome: sixteen characters in a row run past where a label may end, so even a perfectly
    straight run across says nothing."""
    paths, _ = _row("8888888888888888", 0, 0)

    found = label_direction(_box(paths[:2]), paths, REACH)

    assert found.degrees is None and found.unsettled == NOT_CLOSED


def test_a_region_with_no_paths_has_no_direction() -> None:
    paths, _ = _row("12", 0, 0)
    far: Box = (Decimal(500), Decimal(500), Decimal(510), Decimal(510))

    found = label_direction(far, paths, REACH)

    assert (found.degrees, found.label_box, found.in_region) == (None, None, None)
    assert found.unsettled == NO_PATHS


def test_label_geometry_is_unchanged_by_the_direction_rule() -> None:
    """The crop turn keeps the longest-run rule (#783): a turned two-line label still turns, and a
    stacked fraction still reads as it stands."""
    paths, _ = _fraction("1", "3", "4")
    region = _box(paths)

    assert label_geometry(region, _grown(region, 2), paths, REACH).rotation_degrees == 0
