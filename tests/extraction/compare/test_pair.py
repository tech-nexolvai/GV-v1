"""Pairing every architect dimension with the vendor's line for the same span, by geometry only.

Verification for: `extraction/compare/pair.py`. Views are built by hand from plain values; every
number here is made up.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

from extraction.compare.dimensions import ArchitectDimension, Axis, DrawnDimension
from extraction.compare.page import ArchitectViewDimensions, ComparePage, VendorViewDimensions
from extraction.compare.pair import MEASURED_PAIR_SETTINGS, PairStatus, pair_page
from extraction.geometry.rows import Box

ARCH_PPI = Decimal(3)
VENDOR_PPI = Fraction(36, 5)  # a printed 1:10
BOX = Box(Decimal(0), Decimal(0), Decimal(1), Decimal(1))


def _span(
    axis: Axis, start: float, end: float, *, row: int = 0, index: int = 0, offset: float = 50
):
    return DrawnDimension(
        axis=axis,
        offset_pt=Decimal(str(offset)),
        start_pt=Decimal(str(start)),
        end_pt=Decimal(str(end)),
        row=row,
        index=index,
        label_boxes=(BOX,),
        label_text=None,
    )


def _arch_dim(
    number: int, inches: int | None, span: DrawnDimension | None, *, held: str | None = None
):
    drawn = None if span is None else Fraction(span.length_pt) / Fraction(ARCH_PPI)
    return ArchitectDimension(
        number=number,
        text=f'{inches}"',
        label_box=BOX,
        printed_inches=None if inches is None else Fraction(inches),
        inches=None if held or inches is None else Fraction(inches),
        held_reason=held,
        span=span,
        placement_reason="on a line" if span else "no dimension line was found under this label",
        drawn_inches=drawn,
    )


def _arch_view(*dims: ArchitectDimension) -> ArchitectViewDimensions:
    return ArchitectViewDimensions(
        annotation_index=0,
        box=BOX,
        title=None,
        scale_note=None,
        points_per_inch=ARCH_PPI,
        scale_reason="witnessed",
        dimensions=dims,
        lines=tuple(dim.span for dim in dims if dim.span is not None),
    )


def _vendor(*lines: DrawnDimension, ppi: Fraction | None = VENDOR_PPI, number: int = 1):
    return VendorViewDimensions(
        number=number,
        panel_box=BOX,
        heading=None,
        extent=BOX,
        points_per_inch=ppi,
        scale_reason="printed" if ppi else "the view prints no scale code can read",
        lines=lines,
    )


def _page(arch: ArchitectViewDimensions, *vendor: VendorViewDimensions) -> ComparePage:
    return ComparePage(0, (arch,), vendor, ())


def _vendor_line(axis: Axis, inches: float, *, row: int = 0, start: float = 10):
    return _span(axis, start, start + inches * float(VENDOR_PPI), row=row)


def test_the_one_vendor_line_drawn_as_long_on_the_same_axis_is_the_pair() -> None:
    """Input: the architect draws a 36" width; the vendor draws a 36" width, a 20" width and a 36"
    height. Outcome: paired with the 36" width only (axis and length decide)."""
    arch = _arch_view(_arch_dim(1, 36, _span(Axis.HORIZONTAL, 0, 108)))
    width = _vendor_line(Axis.HORIZONTAL, 36)
    page = _page(
        arch,
        _vendor(width, _vendor_line(Axis.HORIZONTAL, 20, row=1), _vendor_line(Axis.VERTICAL, 36)),
    )

    (result,) = pair_page(page, MEASURED_PAIR_SETTINGS)

    assert result.status is PairStatus.PAIRED
    assert result.vendor == width
    assert result.how == "printed scale"


def test_a_little_drawing_error_still_pairs() -> None:
    """A vendor drawing drawn 4% short (the paste factor's error) still pairs."""
    arch = _arch_view(_arch_dim(1, 50, _span(Axis.HORIZONTAL, 0, 150)))
    page = _page(arch, _vendor(_vendor_line(Axis.HORIZONTAL, 48)))

    (result,) = pair_page(page, MEASURED_PAIR_SETTINGS)

    assert result.status is PairStatus.PAIRED


def test_no_line_this_long_and_two_lines_this_long_are_both_question_marks() -> None:
    arch = _arch_view(
        _arch_dim(1, 36, _span(Axis.HORIZONTAL, 0, 108)),
        _arch_dim(2, 24, _span(Axis.VERTICAL, 0, 72)),
    )
    page = _page(
        arch,
        _vendor(
            _vendor_line(Axis.HORIZONTAL, 30),
            _vendor_line(Axis.VERTICAL, 24, row=0),
            _vendor_line(Axis.VERTICAL, 24, row=1, start=200),
        ),
    )

    width, height = pair_page(page, MEASURED_PAIR_SETTINGS)

    assert width.status is PairStatus.UNPAIRED
    assert "no horizontal dimension this long" in width.reason
    assert height.status is PairStatus.UNPAIRED
    assert "2 vendor vertical dimensions" in height.reason


def test_never_paired_by_the_printed_value() -> None:
    """The vendor line carries the architect's own text but is drawn another length: no pair."""
    arch = _arch_view(_arch_dim(1, 36, _span(Axis.HORIZONTAL, 0, 108)))
    line = _vendor_line(Axis.HORIZONTAL, 60)
    page = _page(arch, _vendor(DrawnDimension(**{**_as_dict(line), "label_text": '36"'})))

    (result,) = pair_page(page, MEASURED_PAIR_SETTINGS)

    assert result.status is PairStatus.UNPAIRED


def _as_dict(span: DrawnDimension) -> dict[str, object]:
    return {name: getattr(span, name) for name in DrawnDimension.__slots__}


def test_one_vendor_line_matching_two_architect_dimensions_pairs_neither() -> None:
    arch = _arch_view(
        _arch_dim(1, 24, _span(Axis.HORIZONTAL, 0, 72)),
        _arch_dim(2, 24, _span(Axis.HORIZONTAL, 72, 144, index=1)),
    )
    page = _page(arch, _vendor(_vendor_line(Axis.HORIZONTAL, 24)))

    results = pair_page(page, MEASURED_PAIR_SETTINGS)

    assert [r.status for r in results] == [PairStatus.UNPAIRED, PairStatus.UNPAIRED]
    assert all("only match for 2" in r.reason for r in results)


def test_every_architect_dimension_appears_exactly_once_held_ones_too() -> None:
    arch = _arch_view(
        _arch_dim(1, 36, _span(Axis.HORIZONTAL, 0, 108)),
        _arch_dim(2, 12, None),
        _arch_dim(3, 18, _span(Axis.VERTICAL, 0, 54), held="two labels on one span"),
    )
    page = _page(arch, _vendor(_vendor_line(Axis.HORIZONTAL, 36)))

    results = pair_page(page, MEASURED_PAIR_SETTINGS)

    assert [r.architect.number for r in results] == [1, 2, 3]
    assert results[1].reason == "no dimension line was found under this label"
    assert results[2].reason == "two labels on one span"


def test_no_vendor_drawing_on_the_sheet_says_so() -> None:
    arch = _arch_view(_arch_dim(1, 36, _span(Axis.HORIZONTAL, 0, 108)))

    (result,) = pair_page(_page(arch), MEASURED_PAIR_SETTINGS)

    assert result.reason == "no drawing on this sheet is named the vendor's"


def test_a_vendor_view_with_no_printed_scale_is_registered_to_the_architects() -> None:
    """Input: the architect's chain of four 30" spans (90 pt each) and the vendor's same chain
    drawn at twice the size and shifted, printing no scale. Outcome: each architect span pairs
    with the vendor span on top of it (lengths are all alike, so only the lining-up can tell)."""
    arch_spans = [_span(Axis.HORIZONTAL, 90 * i, 90 * (i + 1), index=i) for i in range(4)]
    vendor_spans = [
        _span(Axis.HORIZONTAL, 40 + 180 * i, 40 + 180 * (i + 1), index=i) for i in range(4)
    ]
    arch = _arch_view(*(_arch_dim(i + 1, 30, span) for i, span in enumerate(arch_spans)))
    page = _page(arch, _vendor(*vendor_spans, ppi=None))

    results = pair_page(page, MEASURED_PAIR_SETTINGS)

    assert [r.status for r in results] == [PairStatus.PAIRED] * 4
    assert [r.vendor for r in results] == vendor_spans
    assert all(r.how == "registered" for r in results)


def test_a_whole_pdf_gives_every_architect_dimension_once_without_a_database() -> None:
    from extraction.compare.pair import compare_pdf
    from tests.extraction.architect.combined_sheet import combined_sheet

    pages = compare_pdf(combined_sheet())

    ((page, pairings),) = pages
    assert len(pairings) == sum(len(view.dimensions) for view in page.architect) == 4
