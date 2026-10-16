"""One combined sheet read for the direct compare (`extraction/compare/page.py`).

Uses the hand-written synthetic sheet of `tests/extraction/architect/combined_sheet.py`: an
architect's drawing pasted above a vendor's, with headings. No client value appears here.
"""

from __future__ import annotations

from fractions import Fraction

from extraction.compare.dimensions import NOT_ON_A_LINE, Axis
from extraction.compare.page import MEASURED_COMPARE_SETTINGS, read_compare_page
from tests.extraction.architect.combined_sheet import combined_sheet


def _read(**kwargs: bool):  # type: ignore[no-untyped-def]
    return read_compare_page(
        combined_sheet(**kwargs), 0, settings=MEASURED_COMPARE_SETTINGS, dpi=150
    )


def test_every_architect_label_is_read_once_and_red_ink_never() -> None:
    """Input: the architect's drawing prints 3'-4", 2'-2", 1'-5" (to a centre line) and 2'-7" on a
    span drawn 24" long, and a red 9'-9" (a reviewer's mark). Outcome: four dimensions, each once;
    the red one is not among them; the one whose drawn length disagrees is held."""
    page = _read()

    (view,) = page.architect
    texts = [dim.text.replace("’", "'") for dim in view.dimensions]
    assert sorted(texts) == sorted(["3' - 4\"", "2' - 2\"", "1' - 5\"", "2' - 7\""])
    by_text = {dim.text.replace("’", "'"): dim for dim in view.dimensions}
    assert by_text["3' - 4\""].inches == Fraction(40)
    assert by_text["2' - 2\""].inches == Fraction(26)
    assert by_text["2' - 7\""].inches is None
    assert by_text["2' - 7\""].held_reason is not None
    assert all(dim.span is None or dim.span.axis is Axis.HORIZONTAL for dim in view.dimensions)
    assert [dim.number for dim in view.dimensions] == list(range(1, len(view.dimensions) + 1))


def test_a_centre_line_dimension_is_kept_for_the_compare() -> None:
    """Type 1 refused centre-line dimensions; the direct compare keeps them (it pairs them with
    the vendor's centre-line dimension of the same fixture)."""
    page = _read()

    (view,) = page.architect
    centre = next(dim for dim in view.dimensions if dim.text.replace("’", "'") == "1' - 5\"")
    assert centre.span is not None
    assert centre.inches == Fraction(17)


def test_the_vendor_view_gives_its_printed_scale() -> None:
    """The fixture's vendor drawing prints `1:10` and draws one line with no ticks: its scale is
    known (pasted at 1:1) and it has no dimension line (a tick-less line is not one)."""
    page = _read()

    (view,) = page.vendor
    assert view.points_per_inch == Fraction(72) / 10
    assert view.lines == ()


def test_without_headings_or_content_judgment_nothing_is_the_architects() -> None:
    page = _read(headings=False, vendor_text=False)

    assert page.architect == ()
    assert any("architect" in note for note in page.notes)


def test_a_label_without_a_line_is_listed_not_dropped() -> None:
    page = _read()

    (view,) = page.architect
    assert all(
        dim.span is not None or dim.placement_reason == NOT_ON_A_LINE for dim in view.dimensions
    )


def test_without_headings_the_drawing_decided_the_vendors_by_content_is_read() -> None:
    """A sheet with no heading: the architect reader decides both drawings from their content, so
    the vendor's drawing is read too (on the base it was not: no heading named it)."""
    page = _read(headings=False)

    assert len(page.architect) == 1
    assert len(page.vendor) == 1
