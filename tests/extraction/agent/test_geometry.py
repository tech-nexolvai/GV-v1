"""Whether a crop cut a label off, and which way the label runs, from the file's paths (#757).

Verification for: `extraction/agent/geometry.py`.

The one that matters most is `test_a_crop_that_cuts_the_first_digit_off_is_cut`: the #641 failure —
a three-digit label cropped to its last two — seen from the paths alone, before any reader looks.

The labels are drawn in the made-up font of `tests/extraction/test_glyph_reader.py`; no client
drawing is read.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from extraction.agent.geometry import Box, LabelReach, label_geometry
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
