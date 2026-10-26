"""Heights' ends named from the drawing (`dim_ends`, 2026-10-26): a symbol's centre (an outlet),
the floor and a counter's top, and the same-ends proof they give. Made-up line work, no client data.

Each drawing is an elevation at 2 points to the inch: a floor line 120" long with a wall standing
on it, a base cabinet with a counter top 1 1/2" thick (its top 36" above the floor), an outlet (a
2 3/4" x 4 1/2" box with its two plugs drawn inside) and three widths printed under the floor (the
view's fit). Heights are printed beside upright lines with ticks, their extension lines dashed to
what they measure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from app.review.assistant import dim_ends, scale_fit

type Point = tuple[float, float]
type Segment = tuple[Point, Point]

PPI = 2.0
X0, FLOOR = 100.0, 400.0


def _x(inches: float) -> float:
    return X0 + inches * PPI


def _y(inches: float) -> float:
    return FLOOR - inches * PPI


@dataclass
class Elevation:
    side: str
    file: str
    segments: list[Segment] = field(default_factory=list)
    labels: list[dict[str, Any]] = field(default_factory=list)

    def line(self, a: Point, b: Point) -> None:
        self.segments.append((a, b))

    def rect(self, x0: float, y0: float, x1: float, y1: float) -> None:
        for a, b in (
            ((x0, y0), (x1, y0)),
            ((x1, y0), (x1, y1)),
            ((x1, y1), (x0, y1)),
            ((x0, y1), (x0, y0)),
        ):
            self.line(a, b)

    def dashed(self, a: Point, b: Point) -> None:
        """A dashed line (4 pt dashes, 2 pt gaps), level or upright."""
        (x0, y0), (x1, y1) = a, b
        length = abs(x1 - x0) + abs(y1 - y0)
        at = 0.0
        while at < length:
            stop = min(at + 4.0, length)
            t0, t1 = at / length, stop / length
            self.line(
                (x0 + (x1 - x0) * t0, y0 + (y1 - y0) * t0),
                (x0 + (x1 - x0) * t1, y0 + (y1 - y0) * t1),
            )
            at += 6.0

    def label(
        self, value: float, box: tuple[float, ...], line: list[list[float]], axis: str
    ) -> str:
        key = f"D{len(self.labels) + 1}"
        self.labels.append(
            {
                "id": key,
                "text": f'{value:g}"',
                "inches": str(value),
                "box": list(box),
                "side": self.side,
                "orientation": axis,
                "file": self.file,
                "page": 1,
                "dim_line": line,
                "scale_ok": True,
            }
        )
        return key

    def floor(self) -> None:
        """The floor line, and the room's left wall standing on it."""
        self.line((_x(-12), FLOOR), (_x(120), FLOOR))
        self.line((_x(-12), _y(96)), (_x(-12), FLOOR))

    def counter(self, from_in: float = 0, to_in: float = 60, top_in: float = 36) -> None:
        """A base cabinet under a counter top 1 1/2" thick."""
        self.rect(_x(from_in), _y(top_in - 1.5), _x(to_in), FLOOR - 4 * PPI)
        self.line((_x(from_in - 1), _y(top_in)), (_x(to_in + 1), _y(top_in)))
        self.line((_x(from_in - 1), _y(top_in - 1.5)), (_x(to_in + 1), _y(top_in - 1.5)))
        self.line((_x(from_in - 1), _y(top_in)), (_x(from_in - 1), _y(top_in - 1.5)))
        self.line((_x(to_in + 1), _y(top_in)), (_x(to_in + 1), _y(top_in - 1.5)))

    def outlet(self, along_in: float, height_in: float, *, plugs: bool = True) -> None:
        cx, cy = _x(along_in), _y(height_in)
        w, h = 2.75 * PPI / 2, 4.5 * PPI / 2
        self.rect(cx - w, cy - h, cx + w, cy + h)
        if plugs:
            self.rect(cx - 0.8, cy - 3.0, cx + 0.8, cy - 1.0)
            self.rect(cx - 0.8, cy + 1.0, cx + 0.8, cy + 3.0)

    def widths(self) -> None:
        """Three widths under the floor, with the reader's own lines: the view's fit."""
        row = FLOOR + 20
        at = 0.0
        for value in (24.0, 30.0, 36.0):
            a, b = _x(at), _x(at + value)
            self.line((a, row), (b, row))
            for end in (a, b):
                self.line((end - 1.2, row + 1.2), (end + 1.2, row - 1.2))
                self.line((end, FLOOR + 2), (end, row + 3))
            middle = (a + b) / 2
            self.label(value, (middle - 6, row - 9, middle + 6, row - 2), [[a, row], [b, row]], "h")
            at += value

    def height(
        self,
        column_in: float,
        low_in: float,
        high_in: float,
        *,
        value: float | None = None,
        to_in: float | None = None,
    ) -> str:
        """An upright dimension at `column_in` from `low_in` to `high_in` above the floor, its top
        end's extension line dashed back to `to_in` along the wall (an outlet), its bottom end on
        the floor line (drawn on under it)."""
        x = _x(column_in)
        top, bottom = _y(high_in), _y(low_in)
        self.line((x, top), (x, bottom))
        for end in (top, bottom):
            self.line((x - 1.2, end + 1.2), (x + 1.2, end - 1.2))
        if to_in is not None:
            self.dashed((x + 2, top), (_x(to_in) + 2.75 * PPI / 2, top))
        middle = (top + bottom) / 2
        printed = high_in - low_in if value is None else value
        return self.label(
            printed, (x - 9, middle - 6, x - 2, middle + 6), [[x, top], [x, bottom]], "v"
        )


def _elevation(side: str, outlet_in: float, *, value: float | None = None) -> tuple[Elevation, str]:
    """An elevation with the outlet's centre `outlet_in` above the floor at 40" along, and the
    floor-to-outlet height printed at 114" along; returns it and that height's label id."""
    drawing = Elevation(side, f"{side}.pdf")
    drawing.floor()
    drawing.counter()
    drawing.outlet(40, outlet_in)
    drawing.widths()
    key = drawing.height(114, 0, outlet_in, value=value, to_in=40)
    return drawing, key


def _page(drawing: Elevation) -> dim_ends.Page:
    return dim_ends.page_of(drawing.file, 1, drawing.labels, [], drawing.segments)


def _ends(page: dim_ends.Page, key: str) -> dim_ends.Ends:
    found = page.ends(key)
    assert found is not None
    return found


def test_an_outlet_height_runs_from_the_floor_to_the_outlets_centre() -> None:
    drawing, key = _elevation("vendor", 73.5)
    page = _page(drawing)
    ends = _ends(page, key)
    assert ends is not None
    assert "symbol_centre" in ends.low.kinds  # the top end (smaller y on the page)
    assert "floor" in ends.high.kinds
    assert (
        ends.words() == "from a symbol's centre (an outlet, a data or J-box, a light) to the floor"
    )


def test_the_same_kinds_prove_the_vendors_outlet_height_against_the_references() -> None:
    vendor, v_key = _elevation("vendor", 73.5)
    reference, a_key = _elevation("architect", 72)
    v_page, a_page = _page(vendor), _page(reference)
    state, why = dim_ends.same_kinds(v_page, _ends(v_page, v_key), a_page, _ends(a_page, a_key))
    assert state == "same_extent", why
    assert "a symbol's centre" in why and "the floor" in why


def test_another_outlets_height_is_a_rival_only_near_the_other_value() -> None:
    reference, a_key = _elevation("architect", 72)
    a_page = _page(reference)
    # a lower outlet at 45": far from 72", never this height's pair
    vendor, v_key = _elevation("vendor", 73.5)
    vendor.outlet(20, 45)
    vendor.height(110, 0, 45, to_in=20)
    v_page = _page(vendor)
    state, why = dim_ends.same_kinds(v_page, _ends(v_page, v_key), a_page, _ends(a_page, a_key))
    assert state == "same_extent", why
    # a second upper outlet at 70": it could be the reference's 72" as well
    vendor, v_key = _elevation("vendor", 73.5)
    vendor.outlet(20, 70)
    vendor.height(110, 0, 70, to_in=20)
    v_page = _page(vendor)
    state, why = dim_ends.same_kinds(v_page, _ends(v_page, v_key), a_page, _ends(a_page, a_key))
    assert state == "cannot_tell" and "another dimension" in why, why


def test_an_empty_box_or_an_end_off_its_centre_is_no_symbol() -> None:
    drawing = Elevation("vendor", "vendor.pdf")
    drawing.floor()
    drawing.widths()
    drawing.outlet(40, 60, plugs=False)  # nothing drawn inside: a box, not a symbol
    drawing.outlet(60, 50)
    empty = drawing.height(114, 0, 60, to_in=40)
    off = drawing.height(110, 0, 51.5, to_in=60)  # ends 1 1/2" above the outlet's centre
    page = _page(drawing)
    assert "symbol_centre" not in _ends(page, empty).low.kinds
    assert "symbol_centre" not in _ends(page, off).low.kinds
    assert "floor" in _ends(page, empty).high.kinds


def test_a_line_drawn_below_is_no_floor_but_the_sheets_frame_is_ignored() -> None:
    drawing, key = _elevation("vendor", 73.5)
    drawing.line((_x(-20), FLOOR + 30), (_x(130), FLOOR + 30))  # another drawing's line below
    assert "floor" not in _ends(_page(drawing), key).high.kinds
    drawing, key = _elevation("vendor", 73.5)
    drawing.line((_x(-200), FLOOR + 30), (_x(320), FLOOR + 30))  # the frame, far wider
    assert "floor" in _ends(_page(drawing), key).high.kinds
    # nothing tall stands on the line (a piece of top drawn alone): no floor
    drawing, key = _elevation("vendor", 73.5)
    drawing.segments.remove(((_x(-12), _y(96)), (_x(-12), FLOOR)))
    assert "floor" not in _ends(_page(drawing), key).high.kinds


def test_a_counter_height_runs_from_the_floor_to_the_counters_top() -> None:
    vendor = Elevation("vendor", "vendor.pdf")
    vendor.floor()
    vendor.counter(top_in=36)
    vendor.widths()
    v_key = vendor.height(70, 0, 36, value=34, to_in=61)  # 34" printed on a line drawn 36"
    reference = Elevation("architect", "architect.pdf")
    reference.floor()
    reference.counter(top_in=36)
    reference.widths()
    a_key = reference.height(70, 0, 36, to_in=61)
    v_page, a_page = _page(vendor), _page(reference)
    ends = _ends(a_page, a_key)
    assert ends is not None
    assert "counter_top" in ends.low.kinds and "floor" in ends.high.kinds
    state, why = dim_ends.same_kinds(v_page, _ends(v_page, v_key), a_page, ends)
    assert state == "cannot_tell" and "drawn 36" in why, why  # a typed-over value never proves
    vendor = Elevation("vendor", "vendor.pdf")
    vendor.floor()
    vendor.counter(top_in=35)
    vendor.widths()
    v_key = vendor.height(70, 0, 35, to_in=61)
    v_page = _page(vendor)
    state, why = dim_ends.same_kinds(v_page, _ends(v_page, v_key), a_page, ends)
    assert state == "same_extent", why
    assert "the top of a counter" in why and "the floor" in why


def test_the_same_size_printed_twice_is_no_rival() -> None:
    reference = Elevation("architect", "architect.pdf")
    reference.floor()
    reference.counter(top_in=36)
    reference.widths()
    a_key = reference.height(70, 0, 36, to_in=61)
    vendor = Elevation("vendor", "vendor.pdf")
    vendor.floor()
    vendor.counter(top_in=35)
    vendor.widths()
    v_key = vendor.height(70, 0, 35, to_in=61)
    vendor.height(-6, 0, 35, to_in=-1)  # the same 35" printed again at the other end
    v_page, a_page = _page(vendor), _page(reference)
    state, why = dim_ends.same_kinds(v_page, _ends(v_page, v_key), a_page, _ends(a_page, a_key))
    assert state == "same_extent", why


def test_the_heights_kinds_never_make_two_dimensions_unlike() -> None:
    span = dim_ends.Span("D1", "v", 0.0, 0.0, 100.0, Fraction(72))
    symbol = dim_ends.End(0.0, frozenset({"symbol_centre"}))
    floor = dim_ends.End(100.0, frozenset({"floor"}))
    sink = dim_ends.End(0.0, frozenset({"sink_centre"}))
    wall = dim_ends.End(100.0, frozenset({"wall"}))
    verdict, _why = dim_ends.alike(
        dim_ends.Ends(span, symbol, floor), dim_ends.Ends(span, sink, wall)
    )
    assert verdict == "unknown"


def test_a_line_from_a_neighbours_ticks_gives_way_to_the_line_drawn_to_its_value() -> None:
    page = dim_ends.Page("v.pdf", 1)
    page.spans["D1"] = dim_ends.Span("D1", "v", 50.0, 100.0, 133.0, Fraction(147, 2))  # 33 pt
    fit = scale_fit.PageFit()
    fit.spans["D1"] = scale_fit.Drawn("D1", "v", 50.0, 100.0, 247.0, Fraction(147, 2), "reader")
    fit.checks["D1"] = scale_fit.Check("D1", "v", Fraction(147, 2), 73.5, True)
    fit.sides["D1"], fit.views["D1"] = "vendor", 0
    fit.fits = [scale_fit.AxisFit("vendor", 0, "v", 0.5, ("D1",), 0.1, (0, 0, 500, 500))]
    page.fit = fit
    dim_ends._trust_fit(page)
    assert (page.spans["D1"].a, page.spans["D1"].b) == (100.0, 247.0)
    # a ticks' line drawn to its value stays as it is
    page.spans["D1"] = dim_ends.Span("D1", "v", 50.0, 90.0, 237.0, Fraction(147, 2))
    dim_ends._trust_fit(page)
    assert (page.spans["D1"].a, page.spans["D1"].b) == (90.0, 237.0)
