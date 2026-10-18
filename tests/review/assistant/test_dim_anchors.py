"""Architect against vendor dimensions paired by anchors (`dim_anchors.py`): pure code.

The line work is made by hand: two elevations of one wall at different scales (the architect's at
2 pt per inch, the vendor's at 3.6), each with its two walls, its floor line, outlets drawn as a
receptacle (a box holding two plugs' strokes), dimension lines with slash ticks, dashed extension
lines from the ticks to the outlets, and labels in `dims.json`'s shape. Made-up values only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import pairwise
from typing import Any

from app.review.assistant import dim_anchors as da
from app.review.assistant import dim_pairs as dp
from app.review.assistant import view_scales as vs
from app.review.assistant.full_check_advocate import code_pairs

type Point = tuple[float, float]
type Segment = tuple[Point, Point]

WIDTH_IN = 100.0
HEIGHT_IN = 96.0


@dataclass
class Elevation:
    """One drawing: inches to page points through its own origin and scale."""

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
    def box(self) -> list[float]:
        return [self.x0 - 80, self.y(HEIGHT_IN) - 60, self.x(WIDTH_IN) + 80, self.floor + 80]

    def room(self, *, height_in: float = HEIGHT_IN) -> None:
        """The floor line and both walls, floor to the top of the drawing."""
        top = self.y(height_in)
        self.segments.append(((self.x(0), self.floor), (self.x(WIDTH_IN), self.floor)))
        for at in (0.0, WIDTH_IN):
            self.segments.append(((self.x(at), top), (self.x(at), self.floor)))

    def outlet(self, along_in: float, height_in: float) -> None:
        """A receptacle centred at the place: its box and two plugs (16 strokes each)."""
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

    def _label(self, value: float, box: list[float], orientation: str, text: str | None) -> None:
        number = self.first_id + len(self.labels)
        self.labels.append(
            {
                "id": f"D{number}",
                "text": text or f'{value:g}"',
                "inches": str(value),
                "box": box,
                "side": self.side,
                "orientation": orientation,
                "route": "text",
            }
        )

    def across(
        self, start_in: float, end_in: float, row_in: float, value: float | None = None
    ) -> str:
        """A horizontal dimension below the floor (`row_in` inches under it), its label above the
        line; `value` printed instead of the drawn length (typed over) when given."""
        row = self.floor + row_in * self.scale
        a, b = self.x(start_in), self.x(end_in)
        self.segments.append(((a, row), (b, row)))
        self._tick(a, row)
        self._tick(b, row)
        mid = (a + b) / 2
        self._label(
            value if value is not None else end_in - start_in,
            [mid - 5, row - 8, mid + 5, row - 2],
            "h",
            None,
        )
        return str(self.labels[-1]["id"])

    def up(self, height_in: float, column_in: float, *, to_x_in: float | None = None) -> str:
        """A vertical dimension from the floor at `column_in` (left of the wall when negative),
        its label left of the line, and a dashed extension line to `to_x_in`'s place."""
        col = self.x(column_in)
        top = self.y(height_in)
        self.segments.append(((col, self.floor), (col, top)))
        self._tick(col, self.floor)
        self._tick(col, top)
        mid = (self.floor + top) / 2
        self._label(height_in, [col - 8, mid - 6, col - 2, mid + 6], "v", None)
        if to_x_in is not None:
            self.dashes("h", top, col, self.x(to_x_in))
        return str(self.labels[-1]["id"])

    def dashes(self, axis: str, at: float, start: float, end: float) -> None:
        lo, hi = sorted((start, end))
        place = lo
        while place < hi:
            stop = min(place + 3, hi)
            piece = ((at, place), (at, stop)) if axis == "v" else ((place, at), (stop, at))
            self.segments.append(piece)
            place = stop + 2

    def extension(self, along_in: float, to_height_in: float, row_in: float) -> None:
        """A dashed vertical extension line from a horizontal dimension up to an outlet."""
        self.dashes("v", self.x(along_in), self.y(to_height_in), self.floor + row_in * self.scale)


def _kitchen(*, vendor_outlets: list[float] | None = None) -> tuple[Elevation, Elevation]:
    """The architect's wall with two outlets at 46" (12" and 50" from the left wall), dimensioned
    as one chain under the floor; the vendor's with the first outlet moved to 7" and dimensioned
    piece by piece."""
    arch = Elevation("architect", 100, 300, 2.0, 1)
    vendor = Elevation("vendor", 400, 760, 3.6, 20)
    for drawing, places in ((arch, [12.0, 50.0]), (vendor, vendor_outlets or [7.0, 50.0])):
        drawing.room()
        for along in places:
            drawing.outlet(along, 46)
    arch.up(HEIGHT_IN, -14)
    arch.up(46, -7, to_x_in=50)
    for along in (12.0, 50.0):
        arch.extension(along, 46, 10)
    arch.across(0, 12, 10)
    arch.across(12, 50, 10)
    arch.across(50, 100, 10)
    vendor.up(HEIGHT_IN, -10)
    vendor.up(46, -5, to_x_in=7)
    vendor.across(0, 100, 14)
    first = (vendor_outlets or [7.0, 50.0])[0]
    vendor.extension(first, 46, 6)
    vendor.extension(50, 46, 6)
    vendor.across(0, first, 6)
    vendor.across(50, 100, 6)
    return arch, vendor


def _pair(arch: Elevation, vendor: Elevation) -> tuple[list[dict[str, Any]], list[str]]:
    a = da.drawing("architect", arch.labels, arch.segments, tuple(arch.box))  # type: ignore[arg-type]
    v = da.drawing("vendor", vendor.labels, vendor.segments, tuple(vendor.box))  # type: ignore[arg-type]
    assert a is not None and v is not None
    return da.pair_drawings(a, v, architect_page=5, vendor_page=5)


def _by_vendor(pairs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {p["vendor"][0]["id"]: p for p in pairs}


def test_anchors_pair_a_moved_outlet_with_no_scale_and_compare_exactly() -> None:
    arch, vendor = _kitchen()
    pairs, _notes = _pair(arch, vendor)
    found = _by_vendor(pairs)
    # wall to the first outlet: the architect's 12" against the vendor's 7" (the outlet moved)
    wall_to_first = found["D23"]
    assert wall_to_first["architect"]["id"] == "D3"
    assert (wall_to_first["architect_in"], wall_to_first["vendor_in"]) == ('12"', '7"')
    assert wall_to_first["status"] == "different"
    assert wall_to_first["paired_by"] == "code" and wall_to_first["method"] == "anchors"
    assert wall_to_first["evidence"] == {"from": ["wall_L"], "to": ["b46#1:c"]}
    assert "the left wall to outlet 1" in wall_to_first["how"]
    # second outlet to the right wall: 50" on both
    assert found["D24"]["architect"]["id"] == "D5" and found["D24"]["status"] == "same"
    # floor to the outlets: 46" on both
    assert found["D21"]["architect"]["id"] == "D2" and found["D21"]["status"] == "same"
    # wall to wall: the vendor's overall against the architect's chain, summed
    overall = found["D22"]
    assert overall["kind"] == "sum" and overall["architect"]["ids"] == ["D3", "D4", "D5"]
    assert overall["architect_in"] == '100"' and overall["status"] == "same"


def test_a_row_with_an_added_outlet_pairs_none_of_its_outlets() -> None:
    arch, vendor = _kitchen(vendor_outlets=[7.0, 50.0])
    vendor.outlet(80, 46)  # the vendor adds a third outlet in the row
    pairs, notes = _pair(arch, vendor)
    found = _by_vendor(pairs)
    assert set(found) == {"D22"}  # only the wall-to-wall overall, by the walls
    assert any("architect draws 2, vendor 3" in note for note in notes)


def test_an_outlet_moved_more_than_a_foot_is_not_the_same_outlet() -> None:
    arch, vendor = _kitchen(vendor_outlets=[30.0, 50.0])
    pairs, notes = _pair(arch, vendor)
    assert "D23" not in _by_vendor(pairs)
    assert any("moved more than" in note for note in notes)


def test_a_dimension_to_an_outlet_edge_never_pairs_with_one_to_its_centre() -> None:
    """Rule R8: floor to the outlet's bottom is not floor to its centre."""
    arch, vendor = _kitchen()
    vendor.labels = [label for label in vendor.labels if label["id"] != "D21"]
    edge = 46 - 12.4 / 2 / vendor.scale  # the vendor's outlet box's low edge, in inches
    vendor.up(edge, -5, to_x_in=7)
    pairs, _notes = _pair(arch, vendor)
    assert all(p["architect"]["id"] != "D2" for p in pairs)


def test_a_counter_height_drawing_has_no_walls() -> None:
    """A cabinet's end is never taken for a wall: under 72" tall, no wall anchors at all."""
    arch, vendor = _kitchen()
    vendor.labels = [label for label in vendor.labels if label["id"] != "D20"]
    vendor.up(40, -10)
    pairs, _notes = _pair(arch, vendor)
    assert pairs  # the floor and the outlets still pair
    assert all("wall" not in str(p["evidence"]) for p in pairs)


def test_a_typed_over_value_has_no_trusted_span() -> None:
    arch, vendor = _kitchen()
    typed = vendor.across(0, 7, 20, value=11)  # drawn 7", printed 11": its ticks are not its value
    pairs, _notes = _pair(arch, vendor)
    assert typed not in _by_vendor(pairs)
    assert "D23" in _by_vendor(pairs)  # the same span drawn to its value still pairs


def test_pairs_json_carries_anchor_pairs_the_full_check_reads() -> None:
    arch, vendor = _kitchen()
    labels = arch.labels + vendor.labels
    dims = {"pages": [{"file": "S.pdf", "page": 5, "labels": labels}]}
    page_map = {
        "pages": [
            {
                "file": "S.pdf",
                "page": 5,
                "area": "Kitchen",
                "halves": [
                    {"role": "architect", "box": arch.box},
                    {"role": "vendor", "box": vendor.box},
                ],
            }
        ]
    }
    segments = arch.segments + vendor.segments
    anchored = da.set_anchor_pairs(dims, page_map, lambda file, page: segments)
    document = dp.set_pairs(dims, vs.set_scales(dims), anchored)
    mapping = code_pairs(document)
    assert mapping["p5.D23"] == "p5.D3"
    assert mapping["p5.D24"] == "p5.D5"
    assert "p5.D22" not in mapping  # the summed chain stays in the page's list only
    flat = {p["vendor"].split()[0]: p for p in document["pairs"]}
    assert flat["p5.D23"]["method"] == "anchors" and flat["p5.D23"]["paired_by"] == "code"


def test_a_vendor_label_paired_two_ways_is_refused() -> None:
    pair = {
        "page": 5,
        "vendor": [{"id": "D9", "text": '10"'}],
        "architect": {"id": "D1", "text": '8"'},
        "status": "different",
    }
    other = {**pair, "architect": {"id": "D2", "text": '9"'}}
    anchored = {("S.pdf", 5): {"pairs": [pair, other], "notes": []}}
    document = dp.set_pairs({"pages": []}, {"pages": []}, anchored)
    assert document["pairs"] == []
    assert any("two different architect labels" in note for note in document["pages"][0]["notes"])


def test_the_sum_is_exact() -> None:
    assert da.SAME_IN == Fraction(1, 32)
