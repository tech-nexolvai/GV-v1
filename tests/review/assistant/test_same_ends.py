"""Same end points from the drawn run (`same_ends.py`): pure code, hand-made line work.

Each drawing is one elevation at its own scale (the architect's 2 pt per inch, the vendor's 3.6):
a floor line, heights dimensioned from the floor with slash ticks, dashed extension lines to what
they measure, receptacles drawn as a box holding two plugs' strokes, and labels in `dims.json`'s
shape. Made-up values only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from app.review.assistant import same_ends as se

type Point = tuple[float, float]
type Segment = tuple[Point, Point]

WIDTH_IN = 100.0
HEIGHT_IN = 96.0


@dataclass
class Elevation:
    side: str
    x0: float
    floor: float
    scale: float
    first_id: int
    segments: list[Segment] = field(default_factory=list)
    labels: list[dict[str, Any]] = field(default_factory=list)

    def x(self, inches: float) -> float:
        return self.x0 + inches * self.scale

    def y(self, inches: float) -> float:
        return self.floor - inches * self.scale

    @property
    def box(self) -> tuple[float, float, float, float]:
        return (self.x0 - 200, self.y(HEIGHT_IN) - 60, self.x(WIDTH_IN) + 80, self.floor + 80)

    def room(self) -> None:
        top = self.y(HEIGHT_IN)
        self.segments.append(((self.x(0), self.floor), (self.x(WIDTH_IN), self.floor)))
        for at in (0.0, WIDTH_IN):
            self.segments.append(((self.x(at), top), (self.x(at), self.floor)))

    def shelf(self, height_in: float, start_in: float = 10, end_in: float = 60) -> None:
        """A long solid line across the elevation (a counter top's edge)."""
        y = self.y(height_in)
        self.segments.append(((self.x(start_in), y), (self.x(end_in), y)))

    def outlet(self, along_in: float, height_in: float) -> None:
        w, h = (5.4, 8.9) if self.side == "architect" else (8.1, 12.4)
        cx, cy = self.x(along_in), self.y(height_in)
        x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
        self.segments += [
            ((x0, y0), (x1, y0)),
            ((x0, y1), (x1, y1)),
            ((x0, y0), (x0, y1)),
            ((x1, y0), (x1, y1)),
        ]
        for py in (cy - h / 5, cy + h / 5):
            ring = [
                (cx + 1.2 * math.cos(k * math.pi / 8), py + 1.2 * math.sin(k * math.pi / 8))
                for k in range(17)
            ]
            self.segments += list(pairwise(ring))

    def _tick(self, x: float, y: float) -> None:
        self.segments.append(((x - 1.2, y + 1.2), (x + 1.2, y - 1.2)))

    def up(
        self,
        top_in: float,
        column_in: float,
        *,
        bottom_in: float = 0.0,
        printed: float | None = None,
        to_x_in: float | None = None,
    ) -> str:
        """A height from `bottom_in` to `top_in` at `column_in` (left of the wall when negative),
        its label left of the line (`printed` typed instead of the drawn value), and a dashed
        extension line from its top to `to_x_in`'s place."""
        col = self.x(column_in)
        low, top = self.y(bottom_in), self.y(top_in)
        self.segments.append(((col, low), (col, top)))
        self._tick(col, low)
        self._tick(col, top)
        mid = (low + top) / 2
        value = printed if printed is not None else top_in - bottom_in
        number = self.first_id + len(self.labels)
        self.labels.append(
            {
                "id": f"D{number}",
                "text": f'{value:g}"',
                "inches": str(value),
                "box": [col - 8, mid - 6, col - 2, mid + 6],
                "side": self.side,
                "orientation": "v",
                "route": "text",
            }
        )
        if to_x_in is not None:
            self.dashes(top, col, self.x(to_x_in))
        return f"D{number}"

    def across(self, start_in: float, end_in: float, row_in: float) -> None:
        """A width below the floor (`row_in` inches under it), its label above its line."""
        row = self.floor + row_in * self.scale
        a, b = self.x(start_in), self.x(end_in)
        self.segments.append(((a, row), (b, row)))
        self._tick(a, row)
        self._tick(b, row)
        mid = (a + b) / 2
        number = self.first_id + len(self.labels)
        self.labels.append(
            {
                "id": f"D{number}",
                "text": f'{end_in - start_in:g}"',
                "inches": str(end_in - start_in),
                "box": [mid - 5, row - 8, mid + 5, row - 2],
                "side": self.side,
                "orientation": "h",
                "route": "text",
            }
        )

    def dashes(self, y: float, start: float, end: float) -> None:
        lo, hi = sorted((start, end))
        place = lo
        while place < hi:
            stop = min(place + 3, hi)
            self.segments.append(((place, y), (stop, y)))
            place = stop + 2

    def side_of(self) -> se.Side:
        found = se.side(self.side, self.labels, self.segments, self.box)
        assert found is not None
        return found


def _pair() -> tuple[Elevation, Elevation]:
    """Two elevations of one wall: heights of 36" and 46" from the floor on both, and the wall's
    width under the floor (so `dim_anchors` checks the floor line against the run)."""
    return _scaled(2.0, 3.6)


def test_a_moved_end_with_nothing_else_drawn_near_it_is_the_same_point() -> None:
    """Floor to a J-box: 29" on the architect, 30" on the vendor. The floor is tied (both
    drawings end two heights on a floor line that runs the wall's width); nothing else is drawn
    near 29" or 30" on either."""
    arch, vendor = _pair()
    a_id = arch.up(29, -10, to_x_in=20)
    v_id = vendor.up(30, -10, to_x_in=20)
    state, why = se.prove(vendor.side_of(), arch.side_of(), v_id, a_id)
    assert state == "same_extent", why
    assert "the floor" in why


def test_underside_against_top_of_a_counter_is_never_the_same_point() -> None:
    """Rule R8's trap: the vendor's 34" runs to the underside of a 1" top, the architect's 35" to
    its top. Both drawings draw both edges: the top's upper face lies exactly where the
    architect's value would end on the vendor's drawing, so different points (#1837; it was
    "cannot tell" before, which left the pair to a blind pick)."""
    arch, vendor = _pair()
    for drawing in (arch, vendor):
        drawing.shelf(34)
        drawing.shelf(35)
    a_id = arch.up(35, -10, to_x_in=10)
    v_id = vendor.up(34, -10, to_x_in=10)
    state, why = se.prove(vendor.side_of(), arch.side_of(), v_id, a_id)
    assert state == "different_extent", why
    assert "different faces" in why


def test_a_difference_too_big_to_be_one_point_is_not_proved() -> None:
    arch, vendor = _pair()
    a_id = arch.up(20, -10)
    v_id = vendor.up(24, -10)
    state, why = se.prove(vendor.side_of(), arch.side_of(), v_id, a_id)
    assert state == "cannot_tell"
    assert "more than" in why


def test_a_value_not_drawn_to_its_size_is_not_proved() -> None:
    """The vendor typed 28" over a line drawn 27" long: where its end lands proves nothing."""
    arch, vendor = _pair()
    a_id = arch.up(29, -10)
    v_id = vendor.up(27, -10, printed=28)
    state, why = se.prove(vendor.side_of(), arch.side_of(), v_id, a_id)
    assert state == "cannot_tell"
    assert "not drawn to its value" in why


def test_an_end_on_another_tied_place_is_a_different_point() -> None:
    """The vendor's 36" ends on the level a 10" piece starts from on both drawings; the
    architect's 30" does not end there: the two dimensions end at different places (only ever a block).
    """
    arch, vendor = _pair()
    for drawing in (arch, vendor):
        drawing.up(46, -25, bottom_in=36)  # a 10" piece from the 36" level up: ties that level
    a_id = arch.up(30, -10)
    v_id = vendor.up(36, -10)
    state, why = se.prove(vendor.side_of(), arch.side_of(), v_id, a_id)
    assert state == "different_extent"
    assert "another place" in why


def test_both_ends_on_the_same_outlet_and_the_floor_are_the_same_points() -> None:
    """Floor to an outlet's centre: 46" on the architect, 47" on the vendor, the outlet the same
    one on both (the only one in its row): both ends are tied."""
    arch = Elevation("architect", 100, 300, 2.0, 1)
    vendor = Elevation("vendor", 400, 760, 3.6, 30)
    for drawing, height in ((arch, 46.0), (vendor, 47.0)):
        drawing.room()
        drawing.up(HEIGHT_IN, -40)
        drawing.up(36, -20)
        drawing.across(0, WIDTH_IN, 10)  # wall to wall: the run the outlets are found along
        drawing.outlet(40, height)
    a_id = arch.up(46, -10, to_x_in=40)
    v_id = vendor.up(47, -10, to_x_in=40)
    state, why = se.prove(vendor.side_of(), arch.side_of(), v_id, a_id)
    assert state == "same_extent", why
    assert "outlet" in why


def test_nothing_tied_proves_nothing() -> None:
    """No floor line and no shared value: neither end is tied, so code cannot tell."""
    arch = Elevation("architect", 100, 300, 2.0, 1)
    vendor = Elevation("vendor", 400, 760, 3.6, 30)
    arch.up(50, -20)
    arch.up(20, -10)
    vendor.up(60, -20)
    vendor.up(21, -10)
    state, _why = se.prove(vendor.side_of(), arch.side_of(), "D31", "D2")
    assert state == "cannot_tell"


def test_a_width_against_a_height_is_different() -> None:
    arch, vendor = _pair()
    a_id = arch.up(29, -10)
    v_side = vendor.side_of()
    v_line = v_side.lines["D30"]
    v_side.lines["D30"] = se.Line("D30", "h", v_line.row, v_line.a, v_line.b, v_line.inches, "x")
    state, _why = se.prove(v_side, arch.side_of(), "D30", a_id)
    assert state == "different_extent"


def test_prove_labels_reads_each_drawing_from_the_page_map_half() -> None:
    arch, vendor = _pair()
    a_id = arch.up(29, -10, to_x_in=20)
    v_id = vendor.up(30, -10, to_x_in=20)
    page_map = {
        "pages": [
            {
                "file": "set.pdf",
                "page": 3,
                "halves": [
                    {"role": "architect", "box": list(arch.box)},
                    {"role": "vendor", "box": list(vendor.box)},
                ],
            }
        ]
    }
    labels = [*arch.labels, *vendor.labels]
    segments = [*arch.segments, *vendor.segments]

    def label(ref: str) -> dict[str, Any]:
        return {**next(x for x in labels if x["id"] == ref), "file": "set.pdf", "page": 3}

    cache: dict[Any, Any] = {}
    state, why = se.prove_labels(
        label(v_id),
        label(a_id),
        labels_of=lambda _file, _page: labels,
        segments_of=lambda _file, _page: segments,
        page_map=page_map,
        cache=cache,
    )
    assert state == "same_extent", why
    assert len(cache) == 2
    # a label outside every half of its role: not proved, never an error
    state, why = se.prove_labels(
        {**label(v_id), "box": [0, 0, 1, 1]},
        label(a_id),
        labels_of=lambda _file, _page: labels,
        segments_of=lambda _file, _page: segments,
        page_map=page_map,
    )
    assert state == "cannot_tell" and "page map" in why


def _scaled(a_scale: float, v_scale: float) -> tuple[Elevation, Elevation]:
    arch = Elevation("architect", 100, 300, a_scale, 1)
    vendor = Elevation("vendor", 400, 760, v_scale, 30)
    for drawing in (arch, vendor):
        drawing.room()
        drawing.up(HEIGHT_IN, -40)
        drawing.up(46, -30)
        drawing.up(36, -20)
        drawing.across(0, WIDTH_IN, 10)
    return arch, vendor


def test_an_edge_too_thin_to_see_at_the_drawings_scale_is_never_the_same_point() -> None:
    """Rule R8's trap on a small drawing (#1808 review): a 1/2" edge at 1/4"=1'-0" (1.5 pt per
    inch) or a 1/4" edge at 2 pt per inch is under a point or two apart, so the other edge cannot
    be told from the end itself: code cannot tell."""
    for a_scale, v_scale, low in ((2.0, 3.6, 34.75), (1.5, 1.5, 34.5)):
        arch, vendor = _scaled(a_scale, v_scale)
        for drawing in (arch, vendor):
            drawing.shelf(low)
            drawing.shelf(35)
        a_id = arch.up(35, -10, to_x_in=10)
        v_id = vendor.up(low, -10, to_x_in=10)
        state, why = se.prove(vendor.side_of(), arch.side_of(), v_id, a_id)
        assert state == "cannot_tell", (a_scale, low, why)


def test_a_counter_line_is_never_taken_for_the_floor() -> None:
    """A vendor drawing of the upper zone only, measured from the counter top: its lowest long
    line is the counter, not the floor, so a backsplash is not tied to the architect's toe kick."""
    arch = Elevation("architect", 100, 300, 2.0, 1)
    arch.room()
    arch.up(HEIGHT_IN, -40)
    arch.up(46, -30)
    arch.up(36, -20)
    a_id = arch.up(4.5, -10, to_x_in=10)
    vendor = Elevation("vendor", 400, 760, 3.6, 30)
    vendor.shelf(0, 0, 60)
    vendor.up(54, -40)
    vendor.up(18, -30)
    v_id = vendor.up(4, -10, to_x_in=10)
    state, why = se.prove(vendor.side_of(), arch.side_of(), v_id, a_id)
    assert state != "same_extent", why


def test_one_value_printed_once_on_each_drawing_is_not_enough_without_a_floor_or_wall() -> None:
    """Widths with no wall: a 24" printed once on each drawing, at different places, does not tie
    the two drawings by itself."""
    vendor = Elevation("vendor", 400, 760, 3.6, 30)
    vendor.across(0, 24, 10)
    vendor.across(24, 54, 10)
    arch = Elevation("architect", 100, 300, 2.0, 1)
    arch.across(0, 18, 10)
    arch.across(18, 42, 10)
    arch.across(42, 73, 10)
    state, why = se.prove(vendor.side_of(), arch.side_of(), "D31", "D3")
    assert state != "same_extent", why


def test_outlets_are_matched_under_the_lock(monkeypatch: Any) -> None:
    """The roster's judges share the drawings across threads, and matching writes the outlets'
    ids onto them (#1808 review): each match, and reading its pairs, holds one lock."""
    held: list[bool] = []
    real = se.dim_anchors._match_outlets

    def match(*args: Any) -> None:
        held.append(se._OUTLETS_LOCK.locked())
        real(*args)

    monkeypatch.setattr(se.dim_anchors, "_match_outlets", match)
    arch, vendor = _pair()
    a_id = arch.up(29, -10, to_x_in=20)
    v_id = vendor.up(30, -10, to_x_in=20)
    se.prove(vendor.side_of(), arch.side_of(), v_id, a_id)
    assert held == [True]
