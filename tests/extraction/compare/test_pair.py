"""Pairing every architect dimension with the vendor's line for the same span, by geometry only.

Verification for: `extraction/compare/pair.py`. Views are built by hand from plain values; every
number here is made up. The architect's view is drawn at 3 pt per inch, the vendor's at 7.2 (a
printed 1:10); both are elevations whose floor is at y = 300 (architect) and y = 700 (vendor).
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from fractions import Fraction

from extraction.compare.dimensions import ArchitectDimension, Axis, DrawnDimension
from extraction.compare.page import ArchitectViewDimensions, ComparePage, VendorViewDimensions
from extraction.compare.pair import MEASURED_PAIR_SETTINGS, PairStatus, compare_pdf, pair_page
from extraction.geometry.rows import Box, TickSource

ARCH_PPI = Decimal(3)
VENDOR_PPI = Fraction(36, 5)
ARCH_FLOOR = 300.0
VENDOR_FLOOR = 700.0
UNIT = Box(Decimal(0), Decimal(0), Decimal(1), Decimal(1))


def _d(value: float) -> Decimal:
    return Decimal(str(round(value, 4)))


def _height(
    floor: float, ppi: float, low_in: float, high_in: float, *, x: float, label: bool = True
) -> DrawnDimension:
    """A height from `low_in` to `high_in` above the floor, its line at `x`, its label beside it."""
    start, end = floor - high_in * ppi, floor - low_in * ppi
    middle = (start + end) / 2
    return DrawnDimension(
        axis=Axis.VERTICAL,
        offset_pt=_d(x),
        start_pt=_d(start),
        end_pt=_d(end),
        row=0,
        index=0,
        label_boxes=(Box(_d(x - 6), _d(middle - 5), _d(x - 1), _d(middle + 5)),) if label else (),
        label_text=None,
        tick_source=TickSource.SLASH,
    )


def _arch_height(low: float, high: float, *, x: float) -> DrawnDimension:
    return _height(ARCH_FLOOR, float(ARCH_PPI), low, high, x=x)


def _vendor_height(low: float, high: float, *, x: float, label: bool = True) -> DrawnDimension:
    return _height(VENDOR_FLOOR, float(VENDOR_PPI), low, high, x=x, label=label)


def _arch_dim(
    number: int, span: DrawnDimension | None, *, held: str | None = None
) -> ArchitectDimension:
    drawn = None if span is None else Fraction(span.length_pt) / Fraction(ARCH_PPI)
    inches = None if drawn is None else Fraction(round(drawn))
    return ArchitectDimension(
        number=number,
        text=f'{inches}"',
        label_box=UNIT,
        printed_inches=inches,
        inches=None if held else inches,
        held_reason=held,
        span=span,
        placement_reason="on a line" if span else "no dimension line was found under this label",
        drawn_inches=drawn,
    )


def _arch_view(
    *dims: ArchitectDimension, kind: str | None = "elevation"
) -> ArchitectViewDimensions:
    return ArchitectViewDimensions(
        annotation_index=0,
        box=UNIT,
        title=None,
        scale_note=None,
        points_per_inch=ARCH_PPI,
        scale_reason="witnessed",
        dimensions=dims,
        lines=tuple(dim.span for dim in dims if dim.span is not None),
        kind=kind,
    )


def _vendor(
    *lines: DrawnDimension, ppi: Fraction | None = VENDOR_PPI, kind: str | None = "elevation"
) -> VendorViewDimensions:
    return VendorViewDimensions(
        number=1,
        panel_box=UNIT,
        heading=None,
        extent=UNIT,
        points_per_inch=ppi,
        scale_reason="printed" if ppi else "the view prints no scale code can read",
        lines=lines,
        kind=kind,
    )


def _page(arch: ArchitectViewDimensions, *vendor: VendorViewDimensions) -> ComparePage:
    return ComparePage(0, (arch,), vendor, ())


def _two_heights() -> ArchitectViewDimensions:
    """The architect's floor-to-counter 36" and floor-to-top 84" (two heights on the floor)."""
    return _arch_view(
        _arch_dim(1, _arch_height(0, 36, x=10)), _arch_dim(2, _arch_height(0, 84, x=20))
    )


def test_the_vendor_height_at_the_same_place_from_the_floor_is_the_pair() -> None:
    counter = _vendor_height(0, 36, x=100)
    page = _page(_two_heights(), _vendor(counter, _vendor_height(0, 84, x=120)))

    first, second = pair_page(page, MEASURED_PAIR_SETTINGS)

    assert first.status is PairStatus.PAIRED
    assert first.vendor == counter
    assert first.vendor_label == counter.label_boxes[0]
    assert second.status is PairStatus.PAIRED


def test_the_same_length_somewhere_else_is_not_the_same_span() -> None:
    """Input: the vendor draws a 36" height, but from 40" to 76" above the floor (an upper
    cabinet), not from the floor. Outcome: no pair (length alone never pairs)."""
    page = _page(
        _two_heights(),
        _vendor(
            _vendor_height(40, 76, x=100),
            _vendor_height(0, 84, x=120),
            _vendor_height(0, 20, x=140),
        ),
    )

    first, _second = pair_page(page, MEASURED_PAIR_SETTINGS)

    assert first.status is PairStatus.UNPAIRED
    assert "no labelled vertical dimension line at this place" in first.reason


def test_a_little_drawing_error_still_pairs() -> None:
    """A vendor drawing drawn 4% short (the paste factor's error) still pairs."""
    page = _page(
        _two_heights(),
        _vendor(_vendor_height(0, 36 * 0.96, x=100), _vendor_height(0, 84 * 0.96, x=120)),
    )

    assert [p.status for p in pair_page(page, MEASURED_PAIR_SETTINGS)] == [PairStatus.PAIRED] * 2


def test_a_line_with_no_label_of_its_own_or_witness_ticks_is_not_a_dimension() -> None:
    """A cabinet edge: drawn at the right place but with no label on it, or with witness ticks."""
    edge = _vendor_height(0, 36, x=100, label=False)
    witnessed = replace(_vendor_height(0, 36, x=110), tick_source=TickSource.WITNESS)
    page = _page(_two_heights(), _vendor(edge, witnessed, _vendor_height(0, 84, x=120)))

    first, _second = pair_page(page, MEASURED_PAIR_SETTINGS)

    assert first.status is PairStatus.UNPAIRED


def test_a_label_nearer_another_line_is_not_this_lines_label() -> None:
    """The overall's slot also holds the next line's label: it is not the overall's own."""
    piece = _vendor_height(0, 36, x=130)
    overall = replace(_vendor_height(0, 84, x=120), label_boxes=piece.label_boxes)
    page = _page(_two_heights(), _vendor(overall, piece))

    _first, second = pair_page(page, MEASURED_PAIR_SETTINGS)

    assert second.status is PairStatus.UNPAIRED


def test_another_kind_of_view_or_no_printed_scale_pairs_nothing() -> None:
    lines = (_vendor_height(0, 36, x=100), _vendor_height(0, 84, x=120))
    as_plan = pair_page(_page(_two_heights(), _vendor(*lines, kind="plan")), MEASURED_PAIR_SETTINGS)
    unscaled = pair_page(_page(_two_heights(), _vendor(*lines, ppi=None)), MEASURED_PAIR_SETTINGS)
    unnamed = pair_page(_page(_two_heights(), _vendor(*lines, kind=None)), MEASURED_PAIR_SETTINGS)

    assert all(p.status is PairStatus.UNPAIRED for p in (*as_plan, *unscaled, *unnamed))
    assert "is a plan" in as_plan[0].reason
    assert "prints no scale" in unscaled[0].reason


def test_an_architect_view_of_no_printed_kind_pairs_nothing() -> None:
    arch = replace(_two_heights(), kind=None)
    page = _page(arch, _vendor(_vendor_height(0, 36, x=100), _vendor_height(0, 84, x=120)))

    results = pair_page(page, MEASURED_PAIR_SETTINGS)

    assert all("what kind of view" in r.reason for r in results)


def test_never_paired_by_the_printed_value() -> None:
    """The vendor line carries the architect's own text but is drawn another length: no pair."""
    other = replace(_vendor_height(0, 50, x=100), label_text='36"')
    page = _page(_two_heights(), _vendor(other, _vendor_height(0, 84, x=120)))

    first, _second = pair_page(page, MEASURED_PAIR_SETTINGS)

    assert first.status is PairStatus.UNPAIRED


def test_two_lines_at_the_same_place_are_a_question_mark() -> None:
    page = _page(
        _two_heights(),
        _vendor(
            _vendor_height(0, 36, x=100),
            _vendor_height(0, 36, x=200),
            _vendor_height(0, 84, x=120),
        ),
    )

    first, _second = pair_page(page, MEASURED_PAIR_SETTINGS)

    assert first.status is PairStatus.UNPAIRED
    assert "2 vendor vertical dimension lines" in first.reason


def test_one_vendor_line_matching_two_architect_dimensions_pairs_neither() -> None:
    arch = _arch_view(
        _arch_dim(1, _arch_height(0, 36, x=10)),
        _arch_dim(2, _arch_height(0, 36, x=15)),
        _arch_dim(3, _arch_height(0, 84, x=20)),
    )
    page = _page(arch, _vendor(_vendor_height(0, 36, x=100), _vendor_height(0, 84, x=120)))

    results = pair_page(page, MEASURED_PAIR_SETTINGS)

    assert [r.status for r in results[:2]] == [PairStatus.UNPAIRED, PairStatus.UNPAIRED]
    assert all("only match for 2" in r.reason for r in results[:2])


def test_every_architect_dimension_appears_exactly_once_held_ones_too() -> None:
    arch = _arch_view(
        _arch_dim(1, _arch_height(0, 36, x=10)),
        _arch_dim(2, None),
        _arch_dim(3, _arch_height(0, 84, x=20), held="two labels on one span"),
    )
    page = _page(arch, _vendor(_vendor_height(0, 36, x=100), _vendor_height(0, 84, x=120)))

    results = pair_page(page, MEASURED_PAIR_SETTINGS)

    assert [r.architect.number for r in results] == [1, 2, 3]
    assert results[1].reason == "no dimension line was found under this label"
    assert results[2].reason == "two labels on one span"


def test_no_vendor_drawing_on_the_sheet_says_so() -> None:
    result, _other = pair_page(_page(_two_heights()), MEASURED_PAIR_SETTINGS)

    assert result.reason == "no drawing on this sheet is named the vendor's"


def test_a_whole_pdf_gives_every_architect_dimension_once_without_a_database() -> None:
    from tests.extraction.architect.combined_sheet import combined_sheet

    ((page, pairings),) = compare_pdf(combined_sheet())

    assert len(pairings) == sum(len(view.dimensions) for view in page.architect) == 4
