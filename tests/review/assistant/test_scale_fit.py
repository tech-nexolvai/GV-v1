"""Each view's scale fitted across and up, each value against its drawn length (`scale_fit`): made-up
line work, no client data.

The plan is drawn at 4 points to the inch: three widths the label reader already gave a line, a
depth printed level beside an upright line (the reader found none), a typed-over depth (printed
23", drawn 24"), a dual label whose millimetres split its line, and a detail at another scale
beside the plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.review.assistant import scale_fit

type Point = tuple[float, float]
type Segment = tuple[Point, Point]

PPI = 4.0


@dataclass
class Sheet:
    """One page of made-up line work and `dims.json` labels."""

    side: str = "vendor"
    segments: list[Segment] = field(default_factory=list)
    labels: list[dict[str, Any]] = field(default_factory=list)

    def line(self, a: Point, b: Point) -> None:
        self.segments.append((a, b))

    def ticks(self, x: float, y: float) -> None:
        self.line((x - 1.2, y + 1.2), (x + 1.2, y - 1.2))

    def label(
        self, text: str, inches: float, box: tuple[float, float, float, float], **extra: Any
    ) -> str:
        key = f"D{len(self.labels) + 1}"
        self.labels.append(
            {
                "id": key,
                "text": text,
                "inches": str(inches),
                "box": list(box),
                "side": self.side,
                "orientation": "h",
                **extra,
            }
        )
        return key

    def width(self, x0: float, x1: float, row: float, inches: float, *, reader: bool = True) -> str:
        """A ticked width, its text above the line; `reader`: the label reader gave it its line."""
        self.line((x0, row), (x1, row))
        for at in (x0, x1):
            self.ticks(at, row)
            self.line((at, row - 4), (at, row + 30))  # extension line toward the drawing below
        middle = (x0 + x1) / 2
        extra: dict[str, Any] = {}
        if reader:
            extra = {"dim_line": [[x0, row], [x1, row]], "span_pt": x1 - x0, "scale_ok": True}
        return self.label(
            f'{inches:g}"', inches, (middle - 8, row - 10, middle + 8, row - 2), **extra
        )

    def depth(
        self, y0: float, y1: float, column: float, inches: float, text: str | None = None
    ) -> str:
        """An upright dimension line with ticks, its text printed level to its left (no line given
        by the reader)."""
        self.line((column, y0), (column, y1))
        for at in (y0, y1):
            self.ticks(column, at)
            self.line((column - 3, at), (column + 30, at))
        middle = (y0 + y1) / 2
        return self.label(
            text or f'{inches:g}"', inches, (column - 16, middle - 4, column - 4, middle + 4)
        )


def _plan() -> tuple[Sheet, dict[str, str]]:
    sheet = Sheet()
    ids = {
        "a": sheet.width(100, 100 + 30 * PPI, 60, 30),
        "b": sheet.width(100 + 30 * PPI, 100 + 54 * PPI, 60, 24),
        "c": sheet.width(100, 100 + 54 * PPI, 40, 54),
    }
    # the drawing itself: a 54" x 24" top (it joins the dimensions' extension lines: one view)
    top, bottom = 90.0, 90.0 + 24 * PPI
    for a, b in (
        ((100, top), (316, top)),
        ((316, top), (316, bottom)),
        ((316, bottom), (100, bottom)),
        ((100, bottom), (100, top)),
    ):
        sheet.line(a, b)
    ids["depth"] = sheet.depth(top, bottom, 70, 24)
    return sheet, ids


def test_a_level_label_beside_an_upright_line_gets_that_line_and_agrees() -> None:
    sheet, ids = _plan()
    fit = scale_fit.page_fit(sheet.labels, sheet.segments)
    span = fit.spans[ids["depth"]]
    assert span.axis == "v" and span.how in ("marks", "scale")
    assert abs(span.length - 24 * PPI) < 1
    check = fit.check(ids["depth"])
    assert check is not None and check.agrees and check.other_axis  # the plan's fit across
    assert [f.axis for f in fit.fits] == ["h"] and abs(fit.fits[0].in_per_pt - 1 / PPI) < 1e-6


def test_a_typed_over_value_does_not_agree_with_its_drawn_length() -> None:
    sheet, ids = _plan()
    for label in sheet.labels:
        if label["id"] == ids["depth"]:
            label.update(text='23"', inches="23")
    fit = scale_fit.page_fit(sheet.labels, sheet.segments)
    check = fit.disagrees(ids["depth"])
    assert check is not None
    assert (
        check.words("the vendor's")
        == "the vendor's 23\" is drawn 24\" long at its view's own scale"
    )
    assert fit.disagrees(ids["a"]) is None


def test_a_dual_label_whose_millimetres_split_its_line_is_found() -> None:
    sheet, _ids = _plan()
    # a 20" width whose text and millimetres leave a 30 pt gap in its line
    x0, x1, row = 100.0, 180.0, 230.0
    sheet.line((x0, row), (125, row))
    sheet.line((155, row), (x1, row))
    for at in (x0, x1):
        sheet.line((at, row + 3), (at, 188))  # extension lines up to the plan's front edge
    key = sheet.label('20"', 20, (133, row - 5, 147, row + 3))
    fit = scale_fit.page_fit(sheet.labels, sheet.segments)
    assert key in fit.spans and fit.check(key) is not None and fit.check(key).agrees


def test_a_detail_at_another_scale_borrows_no_fit_and_a_wrong_line_is_dropped() -> None:
    sheet, _ids = _plan()
    # a detail far below at 10 points to the inch: two widths, apart from the plan
    detail = Sheet(side="vendor")
    first = detail.width(500, 500 + 6 * 10, 500, 6, reader=False)
    second = detail.width(500 + 6 * 10, 500 + 10 * 10, 500, 4, reader=False)
    detail.line((500, 520), (600, 520))
    sheet.segments += detail.segments
    sheet.labels += [{**label, "id": f"X{label['id']}"} for label in detail.labels]
    fit = scale_fit.page_fit(sheet.labels, sheet.segments)
    assert fit.check(f"X{first}") is None and fit.check(f"X{second}") is None
    # a label whose nearest line is drawn three times its value at the plan's fit: not its line
    sheet2, _ = _plan()
    sheet2.line((100, 210), (100 + 30 * PPI, 210))
    for at in (100, 100 + 30 * PPI):
        sheet2.ticks(at, 210)
        sheet2.line((at, 213), (at, 188))
    wrong = sheet2.label('10"', 10, (152, 200, 168, 208))
    fit2 = scale_fit.page_fit(sheet2.labels, sheet2.segments)
    assert wrong not in fit2.spans and fit2.check(wrong) is None


def test_no_line_work_keeps_only_the_readers_lines() -> None:
    sheet, ids = _plan()
    fit = scale_fit.page_fit(sheet.labels, None)
    assert set(fit.spans) == {ids["a"], ids["b"], ids["c"]}
    assert fit.check(ids["depth"]) is None
