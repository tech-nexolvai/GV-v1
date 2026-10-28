"""Whether a crop shows a stacked fraction, and which ones (#735, #834).

The rule every reading over a stacked fraction is held to (`stacked_reading_check`, #846). It was
first the vision readers' pre-call check; the vision readers were retired with the label-first
route (2026-10-28), and the tests of that route went with them. The sheets here are shared with
the OCR routes' tests.

Verification for: `workflow/stages.py` (`crop_shows_a_stacked_fraction`, `stacked_layouts_shown`).
"""

from __future__ import annotations

from decimal import Decimal

from evidence.coordinates import ImagePoint
from evidence.polygon import Polygon
from extraction.annotations import StackedFraction
from extraction.glyph_bands import FractionLayout
from tests.extraction.models.test_validation import THIRTY_SEVEN_AND_A_HALF_LAYOUT, THREE_QUARTERS
from tests.extraction.test_annotations import STACKED_APPEARANCE, _appearance, _pdf, _stamp
from workflow.stages import crop_shows_a_stacked_fraction, stacked_layouts_shown

pytest_plugins = ("tests.app.postgres_fixture",)

#: A line-work run for the label to sit beside: `plan_reads` sets aside a region with no line near it.
DIMENSION_LINE = b"1 w 105 515 m 205 515 l S\n"

STACKED_SHEET = _pdf(
    annotations=[_stamp(appearance_object=6)],
    extra_objects=[_appearance(DIMENSION_LINE + STACKED_APPEARANCE)],
)

#: The same label with its fraction taken away — the two whole-number digits and the inch mark.
PLAIN_SHEET = _pdf(
    annotations=[_stamp(appearance_object=6)],
    extra_objects=[
        _appearance(
            DIMENSION_LINE
            + b"0.2 w 110 520 m 113.6 525.5 l 110 525.5 l S\n"
            + b"114.5 520 m 118.1 525.5 l 114.5 525.5 l S\n"
            + b"125.6 527.7 m 126.1 529.3 l S\n"
            + b"127.2 527.7 m 127.7 529.3 l S\n"
        )
    ],
)


# ---------------------------------------------------------------------------
# Whether a crop shows a fraction: its pixels against the fraction's
# ---------------------------------------------------------------------------


def _fraction(
    left: int, top: int, right: int, bottom: int, layout: FractionLayout | None = None
) -> StackedFraction:
    from uuid import uuid4

    from evidence.coordinates import StoredPoint

    return StackedFraction(
        extent=Polygon(
            points=(
                StoredPoint(x=Decimal("0.1"), y=Decimal("0.1")),
                StoredPoint(x=Decimal("0.2"), y=Decimal("0.1")),
                StoredPoint(x=Decimal("0.2"), y=Decimal("0.2")),
                StoredPoint(x=Decimal("0.1"), y=Decimal("0.2")),
            ),
            space="stored",
            document_version_id=uuid4(),
            page=0,
        ),
        image_extent=(
            ImagePoint(left, top),
            ImagePoint(right, top),
            ImagePoint(right, bottom),
            ImagePoint(left, bottom),
        ),
        layout=layout,
    )


def test_a_fraction_inside_the_crop_is_shown() -> None:
    assert crop_shows_a_stacked_fraction((100, 100, 200, 200), [_fraction(120, 120, 140, 160)])


def test_a_fraction_half_inside_the_crop_is_shown() -> None:
    """A model reads whatever it is shown, and a numerator at the edge can still be promoted into a
    whole number. Any part counts."""
    assert crop_shows_a_stacked_fraction((100, 100, 200, 200), [_fraction(190, 150, 230, 190)])


def test_a_fraction_touching_the_crop_edge_is_shown() -> None:
    assert crop_shows_a_stacked_fraction((100, 100, 200, 200), [_fraction(200, 150, 230, 190)])


def test_a_fraction_elsewhere_on_the_page_is_not() -> None:
    assert not crop_shows_a_stacked_fraction((100, 100, 200, 200), [_fraction(300, 300, 340, 360)])


def test_no_fractions_nothing_shown() -> None:
    assert not crop_shows_a_stacked_fraction((100, 100, 200, 200), [])


def test_a_crop_has_the_layouts_of_the_fractions_it_shows_and_no_others() -> None:
    """**By the same rule as whether it shows one** (#834): what a reading of the crop is checked
    against is every stacked label in it, and nothing elsewhere on the page."""
    (quarters,), (half,) = THREE_QUARTERS, THIRTY_SEVEN_AND_A_HALF_LAYOUT
    fractions = [_fraction(190, 150, 230, 190, quarters), _fraction(300, 300, 340, 360, half)]

    assert stacked_layouts_shown((100, 100, 200, 200), fractions) == (quarters,)
    assert stacked_layouts_shown((100, 100, 400, 400), fractions) == (quarters, half)
    assert stacked_layouts_shown((500, 500, 600, 600), fractions) == ()


def test_a_fraction_set_in_text_shows_but_has_no_layout() -> None:
    """Its characters are text, not paths, so nothing counts them: the crop shows a stacked
    fraction and is refused as one, with no layout to check a reading by."""
    text = [_fraction(120, 120, 140, 160)]

    assert crop_shows_a_stacked_fraction((100, 100, 200, 200), text)
    assert stacked_layouts_shown((100, 100, 200, 200), text) == ()
