"""The line work's index (`segment_index.py`) and the checks that use it give the same answers as
reading every segment, every character and every tick: made-up line work and characters only.

Each check is run twice on the same made-up page: once with the index (a tuple of segments, as a
page's parse holds them) and once the old way (the same segments as a list, or the index switched
off), and the two answers must be equal, ties and order included.
"""

from __future__ import annotations

import math
import random
from typing import Any

import pytest

from app.review.assistant import dim_anchors as da
from app.review.assistant import dim_ends as de
from app.review.assistant import dim_labels as dl
from app.review.assistant import segment_index as si
from app.review.assistant import verify_value as vv

type Point = tuple[float, float]
type Segment = tuple[Point, Point]


def _page(seed: int, count: int = 3000) -> tuple[Segment, ...]:
    """A made-up sheet's line work: long walls and shelves, dimension lines with slash ticks and
    extension lines, dashed centre lines, short strokes and a few slanted lines."""
    rng = random.Random(seed)
    out: list[Segment] = []
    while len(out) < count:
        kind = rng.random()
        x, y = rng.uniform(0, 1200), rng.uniform(0, 900)
        if kind < 0.25:  # a dimension line with its ticks and extension lines
            length = rng.uniform(10, 200)
            row = round(y * 2) / 2
            out.append(((x, row), (x + length, row)))
            for end in (x, x + length):
                out.append(((end - 1.5, row + 1.5), (end + 1.5, row - 1.5)))
                out.append(((end, row - rng.uniform(0, 3)), (end, row + rng.uniform(5, 80))))
        elif kind < 0.4:  # an upright dimension line
            length = rng.uniform(10, 200)
            out.append(((x, y), (x, y + length)))
            out.append(((x - 1.5, y - 1.5), (x + 1.5, y + 1.5)))
        elif kind < 0.5:  # a dashed line
            for step in range(rng.randint(3, 12)):
                out.append(((x + 6 * step, y), (x + 6 * step + 4, y)))
        elif kind < 0.55:  # a long wall
            if rng.random() < 0.5:
                out.append(((0.0, y), (1200.0, y)))
            else:
                out.append(((x, 0.0), (x, 900.0)))
        elif kind < 0.7:  # a slanted stroke (a tick, a leader, a letter's stroke)
            angle = rng.uniform(0, math.pi)
            length = rng.uniform(1, 20)
            out.append(((x, y), (x + length * math.cos(angle), y + length * math.sin(angle))))
        else:  # a short piece of a drawn object
            if rng.random() < 0.5:
                out.append(((x, y), (x + rng.uniform(0.5, 30), y)))
            else:
                out.append(((x, y), (x, y + rng.uniform(0.5, 30))))
    return tuple(out)


def _boxes(seed: int, count: int = 60) -> list[tuple[float, float, float, float]]:
    rng = random.Random(seed)
    out = []
    for _ in range(count):
        x, y = rng.uniform(0, 1200), rng.uniform(0, 900)
        width, height = rng.uniform(6, 40), rng.uniform(4, 9)
        out.append((x, y, x + width, y + height))
    return out


def _meets(segment: Segment, box: tuple[float, float, float, float], pad: float) -> bool:
    (ax, ay), (bx, by) = segment
    return (
        max(ax, bx) >= box[0] - pad
        and min(ax, bx) <= box[2] + pad
        and max(ay, by) >= box[1] - pad
        and min(ay, by) <= box[3] + pad
    )


# ---- the index -------------------------------------------------------------------------------------


def test_near_gives_every_segment_meeting_the_box_in_order() -> None:
    segments = _page(1)
    for box in [
        *_boxes(2),
        (-math.inf, 300.0, math.inf, 310.0),
        (500.0, -math.inf, 502.0, math.inf),
    ]:
        found = si.near(segments, *box)
        assert found == [s for s in segments if _meets(s, box, si.SLACK_PT)]


def test_near_reads_whole_what_it_does_not_index() -> None:
    segments = _page(3)
    assert si.near(list(segments), 0, 0, 10, 10) == list(segments)  # a list could change
    short = segments[: si.MIN_INDEXED - 1]
    assert si.near(short, 0, 0, 10, 10) is short
    assert si.near(segments, math.nan, 0, 10, 10) is segments
    # A box at infinity meets no finite segment (a label's box that is not a number).
    assert si.near(segments, math.inf, 0, math.inf, 10) == []
    assert si.near(segments, -math.inf, -math.inf, -math.inf, 10) == []


def test_a_segment_not_finite_is_always_given() -> None:
    odd: Segment = ((math.nan, 5.0), (10.0, 5.0))
    far: Segment = ((math.inf, 5.0), (10.0, 5.0))
    segments = (*_page(4, 400), odd, far)
    found = si.near(segments, 900.0, 800.0, 901.0, 801.0)
    assert odd in found and far in found
    assert found == [s for s in segments if s in (odd, far) or _meets(s, (900, 800, 901, 801), 1)]


def test_flipped_mirrors_in_order_and_keeps_its_index() -> None:
    segments = _page(5)
    mirror = si.flipped(segments)
    assert mirror == tuple(((a[1], a[0]), (b[1], b[0])) for a, b in segments)
    assert si.flipped(segments) is mirror  # one mirror per page
    for box in _boxes(6):
        assert si.near(mirror, *box) == [s for s in mirror if _meets(s, box, si.SLACK_PT)]
    assert si.flipped(list(segments[:3])) == [((a[1], a[0]), (b[1], b[0])) for a, b in segments[:3]]


# ---- the text route's geometry: same answers as reading every segment --------------------------------


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_dimension_line_is_the_same_with_the_index(seed: int) -> None:
    segments = _page(seed)
    plain = list(segments)
    for box in _boxes(seed + 100, 80):
        for orientation in ("h", "v"):
            for drawn in (False, True):
                indexed = dl.dimension_line(box, orientation, segments, 6.0, drawn=drawn)
                old = dl.dimension_line(box, orientation, plain, 6.0, drawn=drawn)
                assert indexed == old
            assert dl._ticked_line(box, orientation, segments, 6.0) == dl._ticked_line(
                box, orientation, plain, 6.0
            )


def test_the_line_pieces_are_the_same_with_the_index() -> None:
    segments = _page(21)
    plain = list(segments)
    rng = random.Random(22)
    found_any = False
    for _ in range(200):
        x, y = rng.uniform(0, 1200), round(rng.uniform(0, 900) * 2) / 2
        lo, hi = x, x + rng.uniform(5, 150)
        assert dl._extension(x, y, segments, 6.0) == dl._extension(x, y, plain, 6.0)
        assert dl._boundaries(y, lo, hi, segments, 6.0) == dl._boundaries(y, lo, hi, plain, 6.0)
        assert dl._end_marks(y, lo, hi, segments, 6.0) == dl._end_marks(y, lo, hi, plain, 6.0)
        cuts = dl._boundaries(y, lo, hi, plain, 6.0)
        found_any = found_any or len(cuts) > 2
        pieces = [(lo, hi)]
        mid = (lo + hi) / 2
        assert dl._across_crossings(y, mid, cuts, pieces, segments, 6.0) == dl._across_crossings(
            y, mid, cuts, pieces, plain, 6.0
        )
    assert found_any  # the made-up page has lines cut by ticks and extension lines


def test_a_small_dimensions_inside_line_is_the_same_with_the_index() -> None:
    segments = _page(31)
    plain = list(segments)
    for box in _boxes(32, 120):
        for orientation in ("h", "v"):
            label: dict[str, Any] = {"orientation": orientation, "box": list(box)}
            for drawn in (5.0, 10.0, 20.0):
                assert dl._inside_line(label, segments, drawn, 2.0) == dl._inside_line(
                    label, plain, drawn, 2.0
                )


# ---- slash ticks sorted across -------------------------------------------------------------------------


def test_ticks_and_small_spans_are_the_same_with_the_ticks_sorted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    segments = _page(41, 6000)
    lines = da._lines(segments, de._page_box(segments))
    assert len(lines.slanted) >= da._BY_ACROSS_MIN
    rng = random.Random(42)
    asks = [
        (axis, rng.uniform(0, 900), rng.uniform(0, 600), rng.uniform(600, 1200))
        for axis in ("h", "v")
        for _ in range(100)
    ]
    labels = [
        {"id": f"D{i}", "inches": "1", "orientation": rng.choice("hv"), "box": list(box)}
        for i, box in enumerate(_boxes(43, 80))
    ]
    with_index = [da._ticks(lines, *ask) for ask in asks]
    spans = [de.small_span(label, lines, 10.0, labels) for label in labels]
    monkeypatch.setattr(da, "_BY_ACROSS_MIN", 10**9)
    assert with_index == [da._ticks(lines, *ask) for ask in asks]
    assert spans == [de.small_span(label, lines, 10.0, labels) for label in labels]
    assert any(with_index)


# ---- characters grouped into labels --------------------------------------------------------------------


def _chars(seed: int, count: int) -> list[vv._Char]:
    """Made-up characters: words and numbers in rows, some stacked, a few large titles, two
    directions."""
    rng = random.Random(seed)
    out: list[vv._Char] = []
    while len(out) < count:
        size = rng.choice([2.0, 2.5, 3.0, 3.0, 4.0, 12.0])
        direction = rng.choice([(1.0, 0.0), (1.0, 0.0), (0.0, -1.0)])
        along, across = rng.uniform(0, 1000), rng.uniform(0, 800)
        for step in range(rng.randint(1, 8)):
            gap = rng.choice([0.6, 0.8, 1.0, 1.3]) * size
            spot = along + step * gap
            out.append(
                vv._Char(
                    text=rng.choice("0123456789/\"'-ABC"),
                    box=(spot, across, spot + 0.6 * size, across + size),
                    direction=direction,
                    along=spot,
                    across=across + rng.choice([0.0, 0.0, 0.5 * size, -0.95 * size]),
                    size=size,
                    length=0.6 * size,
                )
            )
    rng.shuffle(out)
    return out[:count]


@pytest.mark.parametrize("seed", [51, 52, 53])
def test_characters_group_into_the_same_labels_in_the_same_order(
    seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    chars = _chars(seed, 900)
    grouped = vv._labels(chars)
    monkeypatch.setattr(vv, "_GRID_LABELS_MIN", 10**9)
    assert grouped == vv._labels(chars)
    assert any(len(label) > 3 for label in grouped)


def test_characters_with_a_place_not_finite_are_grouped_the_plain_way() -> None:
    chars = _chars(54, 100)
    odd = vv._Char("1", (0, 0, 1, 1), (1.0, 0.0), math.nan, 0.0, 2.0, 1.0)
    assert vv._grid_labels([*chars, odd]) is None


# ---- the drawn route's ink: same paths and the same covered boxes ---------------------------------------


def _ink(seed: int, count: int) -> tuple[vv._Path, ...]:
    rng = random.Random(seed)
    out = []
    for _ in range(count):
        x, y = rng.uniform(0, 1000), rng.uniform(0, 800)
        w, h = rng.uniform(0.2, 9), rng.uniform(0.2, 9)
        out.append(vv._Path((x, y, x + w, y + h), (((x, y), (x + w, y + h)),), False, 0.3))
    return tuple(out)


def test_paths_near_a_spot_are_the_same_read_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _ink(61, 4000)
    spots = [
        (x, y, x + rng_w, y + 8) for x, y, rng_w in [(10, 10, 30), (500, 400, 5), (990, 790, 40)]
    ]
    spots += [box for box in _boxes(62, 50)]
    at_once = [vv._paths_within(paths, spot, 3.0) for spot in spots]
    monkeypatch.setattr(vv, "_PATH_BOXES_MIN", 10**9)
    assert at_once == [vv._paths_within(paths, spot, 3.0) for spot in spots]
    assert any(at_once)


def test_covered_boxes_are_the_same_read_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    boxes = [path.box for path in _ink(63, 5000)]
    skip = [(x0, y0, x0 + (x1 - x0) * 3, y1) for x0, y0, x1, y1 in _boxes(64, 120)]
    at_once = dl._covered(boxes, skip)
    monkeypatch.setattr(dl, "_COVERED_PAIRS", 10**12)
    assert at_once == dl._covered(boxes, skip)
    assert any(at_once) and not all(at_once)


# ---- an architect's hand lettering: the same characters and rows --------------------------------------


def _lettering(seed: int, rows: int) -> list[vv._Path]:
    """Made-up hand lettering: rows of digit-sized strokes with small marks between some, in
    columns down a sheet (so many rows share each place along), plus loose specks."""
    rng = random.Random(seed)
    out: list[vv._Path] = []
    for _ in range(rows):
        x, y = rng.uniform(0, 600), rng.uniform(0, 3000)
        size = rng.choice([2.5, 3.0, 4.0])
        for step in range(rng.randint(2, 6)):
            left = x + step * size * rng.choice([0.8, 1.0, 1.4, 2.0])
            for _stroke in range(rng.randint(1, 3)):
                x0 = left + rng.uniform(0, 0.4 * size)
                y0 = y + rng.uniform(-0.1, 0.1) * size
                box = (x0, y0, x0 + rng.uniform(0.1, 0.6) * size, y0 + size * rng.uniform(0.9, 1.0))
                out.append(vv._Path(box, (((box[0], box[1]), (box[2], box[3])),), False, 0.3))
            if rng.random() < 0.3:  # a small mark after the digit
                mark = (left + 0.9 * size, y, left + 1.0 * size, y + 0.3 * size)
                out.append(vv._Path(mark, (((mark[0], mark[1]), (mark[2], mark[3])),), False, 0.3))
    for _ in range(rows):
        x, y = rng.uniform(0, 600), rng.uniform(0, 3000)
        box = (x, y, x + rng.uniform(0.1, 3), y + rng.uniform(0.1, 3))
        out.append(vv._Path(box, (((box[0], box[1]), (box[2], box[3])),), False, 0.3))
    rng.shuffle(out)
    return out


@pytest.mark.parametrize("seed", [71, 72])
def test_hand_lettering_rows_are_the_same_read_by_columns(
    seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.review.assistant import hand_labels as hl

    paths = _lettering(seed, 900)
    by_columns = {turn: hl._rows(hl._characters(paths, turn)) for turn in (0, 90, 270)}
    found = hl.lettered(paths)
    monkeypatch.setattr(hl, "_COLUMNS_MIN", 10**9)
    assert by_columns == {turn: hl._rows(hl._characters(paths, turn)) for turn in (0, 90, 270)}
    assert found == hl.lettered(paths)
    assert any(len(row) > 2 for row in by_columns[0])
