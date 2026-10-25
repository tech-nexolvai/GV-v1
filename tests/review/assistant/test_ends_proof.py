"""What a dimension's ends touch (`dim_ends`, final plan #4) and code's proof that a vendor and a
reference dimension run between the same two points (`ends_proof`): made-up line work, no client
data.

Both drawings are plans at 4 points to the inch: a top 54" wide and 24" deep against a wall drawn
as a band 4" thick behind it, a sink cut-out 20" x 15" with "SINK" inside, three widths with the
label reader's own lines (the fit), and an upright dimension from the wall's face to the cut-out's
back edge, printed level beside its line. The vendor's cut-out sits 1" further from the wall.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.review.assistant import dim_ends, ends_proof, proof

type Point = tuple[float, float]
type Segment = tuple[Point, Point]

PPI = 4.0
X0, TOP = 100.0, 100.0


@dataclass
class Plan:
    side: str
    file: str
    segments: list[Segment] = field(default_factory=list)
    labels: list[dict[str, Any]] = field(default_factory=list)
    phrases: list[dict[str, Any]] = field(default_factory=list)

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
                "file": self.file,
                "page": 1,
                **extra,
            }
        )
        return key

    def width(self, a_in: float, b_in: float, row: float) -> str:
        x0, x1 = X0 + a_in * PPI, X0 + b_in * PPI
        self.line((x0, row), (x1, row))
        for at in (x0, x1):
            self.line((at - 1.2, row + 1.2), (at + 1.2, row - 1.2))
            self.line((at, row - 3), (at, TOP - 2))
        middle = (x0 + x1) / 2
        value = b_in - a_in
        return self.label(
            f'{value:g}"',
            value,
            (middle - 8, row - 10, middle + 8, row - 2),
            dim_line=[[x0, row], [x1, row]],
            span_pt=x1 - x0,
            scale_ok=True,
        )

    def depth(self, y0: float, y1: float, column: float, inches: float) -> str:
        """An upright dimension with ticks and extension lines to the right, its text level."""
        self.line((column, y0), (column, y1))
        for at in (y0, y1):
            self.line((column - 1.2, at + 1.2), (column + 1.2, at - 1.2))
            self.line((column - 3, at), (column + 26, at))
        middle = (y0 + y1) / 2
        return self.label(f'{inches:g}"', inches, (column - 13, middle - 4, column - 2, middle + 4))


def _plan(side: str, file: str, back_in: float) -> tuple[Plan, str]:
    """A plan with its cut-out `back_in` inches from the wall; returns it and the back offset's
    label id."""
    plan = Plan(side, file)
    right = X0 + 54 * PPI
    front = TOP + 24 * PPI
    plan.rect(X0, TOP, right, front)  # the top
    plan.line((X0 - 20, TOP - 4 * PPI), (right + 20, TOP - 4 * PPI))  # the wall's far face
    plan.line((X0 - 20, TOP), (X0, TOP))  # the wall's near face runs on past the top
    plan.line((right, TOP), (right + 20, TOP))
    cut_top = TOP + back_in * PPI
    plan.rect(X0 + 17 * PPI, cut_top, X0 + 37 * PPI, cut_top + 15 * PPI)  # the cut-out
    plan.phrases.append(
        {
            "text": "SINK",
            "box": [X0 + 25 * PPI, cut_top + 6 * PPI, X0 + 29 * PPI, cut_top + 8 * PPI],
        }
    )
    plan.width(0, 17, TOP - 50)
    plan.width(17, 37, TOP - 50)
    plan.width(37, 54, TOP - 50)
    plan.width(0, 54, TOP - 70)
    key = plan.depth(TOP, cut_top, X0 + 12 * PPI, back_in)
    return plan, key


def _page(plan: Plan) -> dim_ends.Page:
    return dim_ends.page_of(plan.file, 1, plan.labels, plan.phrases, plan.segments)


def test_the_back_offset_ends_on_the_walls_face_and_the_cut_outs_edge() -> None:
    plan, key = _plan("vendor", "v.pdf", 5)
    page = _page(plan)
    ends = page.ends(key)
    assert ends is not None
    assert "wall" in ends.low.kinds and "cutout_edge" in ends.high.kinds
    assert page.fit is not None and page.fit.check(key) is not None and page.fit.check(key).agrees


def test_the_same_kinds_on_both_drawings_prove_the_same_two_points() -> None:
    vendor, v_key = _plan("vendor", "v.pdf", 5)
    reference, a_key = _plan("architect", "a.pdf", 4)
    v_page, a_page = _page(vendor), _page(reference)
    state, why = dim_ends.same_kinds(v_page, v_page.ends(v_key), a_page, a_page.ends(a_key))
    assert state == "same_extent", why
    assert "the wall's face" in why and "the sink cut-out's edge" in why


def test_a_rival_a_typed_over_value_or_a_big_difference_proves_nothing() -> None:
    reference, a_key = _plan("architect", "a.pdf", 4)
    a_page = _page(reference)
    # a second dimension from the wall to a cut-out's edge (its far one) on the vendor's drawing
    vendor, v_key = _plan("vendor", "v.pdf", 5)
    vendor.depth(TOP, TOP + 20 * PPI, X0 + 30 * PPI, 20)
    v_page = _page(vendor)
    state, why = dim_ends.same_kinds(v_page, v_page.ends(v_key), a_page, a_page.ends(a_key))
    assert state == "cannot_tell" and "another dimension" in why, why
    # typed over: the vendor prints 6" on a line drawn 5" long
    vendor, v_key = _plan("vendor", "v.pdf", 5)
    for label in vendor.labels:
        if label["id"] == v_key:
            label.update(text='6"', inches="6")
    v_page = _page(vendor)
    said = dim_ends.drawn_words(v_page, v_key, "the vendor's")
    assert said == "the vendor's 6\" is drawn 5\" long at its view's own scale"
    assert v_page.ends(v_key) is None  # a line the fit cannot vouch for names no end
    # more than 3" apart: more likely two different dimensions
    vendor, v_key = _plan("vendor", "v.pdf", 8)
    v_page = _page(vendor)
    state, why = dim_ends.same_kinds(v_page, v_page.ends(v_key), a_page, a_page.ends(a_key))
    assert state == "cannot_tell" and "more than 3" in why, why


def test_a_top_edge_against_a_panel_is_the_countertop_against_the_cabinets() -> None:
    top = dim_ends.End(0.0, frozenset({"top_edge"}))
    panel = dim_ends.End(0.0, frozenset({"end_panel"}))
    span = dim_ends.Span("D1", "h", 0.0, 0.0, 100.0, dim_ends.Fraction(60))
    other = dim_ends.Span("D2", "h", 0.0, 0.0, 200.0, dim_ends.Fraction(87))
    vendor = dim_ends.Ends(span, top, top)
    architect = dim_ends.Ends(other, panel, panel)
    page = dim_ends.Page("x.pdf", 1)
    state, why = dim_ends.same_kinds(page, vendor, page, architect)
    assert state == "different_extent"
    assert why.startswith("one measures the countertop, the other the cabinets")


def test_code_extent_a_drawn_disagreement_overrides_the_anchor_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vendor, v_key = _plan("vendor", "v.pdf", 5)
    reference, a_key = _plan("architect", "a.pdf", 4)
    pages = {"v.pdf": _page(vendor), "a.pdf": _page(reference)}
    by_file = {"v.pdf": vendor, "a.pdf": reference}

    def labels_of(file: str, page: int) -> list[dict[str, Any]]:
        return by_file[file].labels

    def label(plan: Plan, key: str) -> dict[str, Any]:
        return next(item for item in plan.labels if item["id"] == key)

    kwargs: dict[str, Any] = {
        "labels_of": labels_of,
        "segments_of": lambda file, page: by_file[file].segments,
        "page_map": None,
        "pages": lambda file, page: pages[file],
    }
    state, why = ends_proof.code_extent(label(vendor, v_key), label(reference, a_key), **kwargs)
    assert state == "same_extent" and why.startswith("the ends: "), why
    # a typed-over vendor value never feeds a red, even where the anchor fit lines the ends up
    monkeypatch.setattr(
        ends_proof, "same_extent", lambda *a: ("same_extent", "the ends land together")
    )
    for item in vendor.labels:
        if item["id"] == v_key:
            item.update(text='6"', inches="6")
    pages["v.pdf"] = _page(vendor)
    state, why = ends_proof.code_extent(label(vendor, v_key), label(reference, a_key), **kwargs)
    assert state == "cannot_tell" and "does not agree with its drawing" in why, why


def _right(v_key: str, a_key: str) -> dict[str, Any]:
    return {
        "id": "F1",
        "status": "same",
        "dims": {
            "vendor": {"id": v_key, "file": "v.pdf", "page": 1},
            "architect": {"id": a_key, "file": "a.pdf", "page": 1},
        },
    }


def test_a_right_records_whether_code_proved_its_ends_alike() -> None:
    vendor, v_key = _plan("vendor", "v.pdf", 5)
    reference, a_key = _plan("architect", "a.pdf", 5)
    pages = {"v.pdf": _page(vendor), "a.pdf": _page(reference)}
    finding = _right(v_key, a_key)
    record = dim_ends.right_ends(finding, lambda file, page: pages[file])
    assert record is not None and record["proved"] is True, record
    finding[dim_ends.LIKE_FOR_LIKE] = record
    assert not proof.matches_unproved(finding)
    # the vendor's overall width (its ends named by nothing code can see) against the offset
    width = next(label["id"] for label in vendor.labels if label["text"] == '54"')
    finding = _right(width, a_key)
    record = dim_ends.right_ends(finding, lambda file, page: pages[file])
    assert record is not None and record["proved"] is False
    finding[dim_ends.LIKE_FOR_LIKE] = record
    assert proof.matches_unproved(finding)
    assert proof.MATCHES_UNPROVED == "Matches (ends not proved)"
    # a record for other labels says nothing about the item's pair
    finding["dims"]["architect"]["id"] = "D99"
    assert not proof.matches_unproved(finding)
