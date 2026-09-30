"""Telling a dimension line from the box it measures (#179).

Verification for: `extraction/geometry/dimension_lines.py`.

The one to read first is `test_a_box_corner_is_not_a_dimension`. Every stroke on a drawing has ends,
and at most ends some other stroke passes close by — so "met at both ends by a perpendicular" finds
the dimensions *and* all four edges of every cabinet. On the real sheet this was developed against
that is 81 candidates where 21 are dimensions. What separates them is whether the perpendicular
*crosses*: a witness line runs from the geometry, past the dimension line, and a little beyond,
while a box corner simply stops. T against L.

Every fixture here is authored geometry — a few points typed out. No client drawing is read, and no
dimension off one appears in this file.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from evidence.coordinates import StoredPoint
from extraction.geometry.containment import DimensionExtent
from extraction.geometry.dimension_lines import Axis, DetectedDimensions, detect

DOCUMENT = UUID("11111111-1111-4111-8111-111111111111")

#: Deliberately generous across the axis and tight along it — the two jobs the detector keeps apart.
TOLERANCE = Decimal("0.01")
MARGIN = Decimal("0.001")
STRAIGHT = Decimal("0.0005")
MINIMUM = Decimal("0.02")


def _segment(x0: str, y0: str, x1: str, y1: str, *, page: int = 0) -> DimensionExtent:
    return DimensionExtent(
        start=StoredPoint(Decimal(x0), Decimal(y0)),
        end=StoredPoint(Decimal(x1), Decimal(y1)),
        document_version_id=DOCUMENT,
        page=page,
    )


def _detect(*segments: DimensionExtent) -> DetectedDimensions:
    return detect(
        segments,
        witness_tolerance=TOLERANCE,
        minimum_span=MINIMUM,
        straightness=STRAIGHT,
        crossing_margin=MARGIN,
    )


def _dimension(y: str, x0: str, x1: str) -> tuple[DimensionExtent, ...]:
    """A horizontal dimension at `y` from `x0` to `x1`, with witness lines crossing both ends.

    The witnesses run from above the line to below it, which is what makes each junction a T.
    """
    return (
        _segment(x0, y, x1, y),
        _segment(x0, "0.30", x0, "0.50"),
        _segment(x1, "0.30", x1, "0.50"),
    )


# ---------------------------------------------------------------------------
# The discriminator
# ---------------------------------------------------------------------------


def test_a_dimension_line_bounded_by_crossing_witnesses_is_found() -> None:
    """Input: a run whose ends are crossed by perpendiculars. Outcome: one dimension line."""
    found = _detect(*_dimension("0.40", "0.20", "0.60"))

    assert len(found.lines) == 1
    line = found.lines[0]
    assert line.axis is Axis.HORIZONTAL
    assert line.span == Decimal("0.40")
    assert len(line.witness_lines) == 2
    assert line.source == "vector"
    assert found.unclassified == ()


def test_a_box_corner_is_not_a_dimension() -> None:
    """**The discriminator.** Input: a closed rectangle. Outcome: nothing classified.

    Every edge of a rectangle is met at both ends by a perpendicular — that is what a corner is. If
    meeting were the test, a drawing of four cabinets would report sixteen dimensions, each of them
    the outline of a thing rather than a measurement of it, and every one would be handed to the
    association step as somewhere a reading could attach.

    The corners here are Ls: the strokes stop at one another rather than crossing. Nothing is
    classified, and all four edges come back unclassified rather than being forced.
    """
    rectangle = (
        _segment("0.20", "0.30", "0.60", "0.30"),
        _segment("0.20", "0.50", "0.60", "0.50"),
        _segment("0.20", "0.30", "0.20", "0.50"),
        _segment("0.60", "0.30", "0.60", "0.50"),
    )

    found = _detect(*rectangle)

    assert found.lines == ()
    assert len(found.unclassified) == 4


def test_a_run_met_at_only_one_end_is_not_a_dimension() -> None:
    """Input: one witness line. Outcome: unclassified.

    A stroke crossed at one end is a stroke that happens to touch something. A dimension states a
    distance *between two places*, and one bound is not a distance.
    """
    found = _detect(
        _segment("0.20", "0.40", "0.60", "0.40"),
        _segment("0.20", "0.30", "0.20", "0.50"),
    )

    assert found.lines == ()
    assert len(found.unclassified) == 2


def test_a_witness_that_stops_short_does_not_bound_anything() -> None:
    """Input: a perpendicular ending exactly on the line. Outcome: not a dimension.

    The boundary case for the T/L rule, stated on its own so a change to `crossing_margin` cannot
    quietly turn every corner back into a dimension.
    """
    found = _detect(
        _segment("0.20", "0.40", "0.60", "0.40"),
        _segment("0.20", "0.40", "0.20", "0.50"),
        _segment("0.60", "0.40", "0.60", "0.50"),
    )

    assert found.lines == ()


def test_a_run_shorter_than_the_minimum_is_not_a_candidate() -> None:
    """Input: a crossed run below `minimum_span`. Outcome: unclassified.

    Arrowheads, ticks and hatching are strokes too, and each has ends other strokes pass near.
    """
    found = _detect(*_dimension("0.40", "0.20", "0.21"))

    assert found.lines == ()


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------


def test_every_stroke_comes_back_in_exactly_one_list() -> None:
    """**The contract.** Outcome: nothing is dropped and nothing is counted twice.

    A dimension quietly lost here is a check that silently stops running, and there is nothing
    downstream to notice: a sheet with three dimensions and a sheet with thirty where twenty-seven
    were missed produce the same findings, both complete-looking.
    """
    strokes = (
        *_dimension("0.40", "0.20", "0.60"),
        _segment("0.70", "0.70", "0.90", "0.90"),
        _segment("0.10", "0.10", "0.15", "0.10"),
    )

    found = _detect(*strokes)

    classified = {id(found.lines[0].extent)} | {id(w) for w in found.lines[0].witness_lines}
    assert len(classified) + len(found.unclassified) == len(strokes)


def test_a_segment_cannot_be_both_classified_and_unclassified() -> None:
    """Outcome: the result type refuses to exist.

    Asserted on the type rather than trusting `detect` to be careful, because the invariant is what
    lets a caller add the two lists and know it has the whole sheet.
    """
    line = _segment("0.20", "0.40", "0.60", "0.40")
    witness = _segment("0.20", "0.30", "0.20", "0.50")
    from extraction.geometry.dimension_lines import DimensionLine

    with pytest.raises(ValueError, match="both classified and unclassified"):
        DetectedDimensions(
            lines=(DimensionLine(extent=line, axis=Axis.HORIZONTAL, witness_lines=(witness,)),),
            chains=(),
            closures=(),
            unclassified=(line,),
        )


def test_the_same_stroke_drawn_twice_is_one_stroke() -> None:
    """**Input: coincident duplicates. Outcome: one dimension, not two.**

    The real sheet emits many of its lines in duplicate, which is ordinary in CAD output and doubled
    the dimension count until it was handled. Two strokes with identical endpoints cannot be two
    different dimensions.
    """
    first = _dimension("0.40", "0.20", "0.60")

    found = _detect(*first, *first)

    assert len(found.lines) == 1


def test_a_stroke_drawn_backwards_is_the_same_stroke() -> None:
    """Outcome: reversing the endpoints does not make a second dimension.

    A path's direction is how it was drawn, not what it is. Without this the duplicate check would
    miss exactly the case a plotter produces when it retraces a line the other way.
    """
    found = _detect(
        _segment("0.20", "0.40", "0.60", "0.40"),
        _segment("0.60", "0.40", "0.20", "0.40"),
        _segment("0.20", "0.30", "0.20", "0.50"),
        _segment("0.60", "0.30", "0.60", "0.50"),
    )

    assert len(found.lines) == 1


# ---------------------------------------------------------------------------
# Chains — what closure validation needs
# ---------------------------------------------------------------------------


def test_dimensions_drawn_end_to_end_are_a_chain() -> None:
    """**Input: three dimensions sharing their ends. Outcome: one chain of three.**

    A cabinet run dimensioned segment by segment is a statement the drawing makes about itself, and
    the parts summing to the whole is checkable without knowing what any of it means. `B3` is where
    that check lives; this only has to find the chain for it to have something to check.
    """
    strokes = (
        _segment("0.20", "0.40", "0.40", "0.40"),
        _segment("0.40", "0.40", "0.55", "0.40"),
        _segment("0.55", "0.40", "0.80", "0.40"),
        _segment("0.20", "0.30", "0.20", "0.50"),
        _segment("0.40", "0.30", "0.40", "0.50"),
        _segment("0.55", "0.30", "0.55", "0.50"),
        _segment("0.80", "0.30", "0.80", "0.50"),
    )

    found = _detect(*strokes)

    assert len(found.chains) == 1
    chain = found.chains[0]
    assert chain.axis is Axis.HORIZONTAL
    assert len(chain.lines) == 3
    # The closure the chain exists to make checkable: the links span exactly what the chain spans.
    assert sum((line.span for line in chain.lines), Decimal(0)) == chain.span


def test_one_dimension_is_not_a_chain() -> None:
    """Outcome: no chain.

    Returning a chain of one would make every dimension on a sheet look like a closure waiting to be
    validated, and `B3` would spend its time confirming that a number equals itself.
    """
    found = _detect(*_dimension("0.40", "0.20", "0.60"))

    assert found.lines != ()
    assert found.chains == ()


def test_two_dimensions_on_the_same_line_but_apart_are_not_a_chain() -> None:
    """Input: collinear, not touching. Outcome: two dimensions, no chain.

    Collinearity alone would chain two measurements at opposite ends of a sheet that happen to share
    a `y`, and a closure check on those would compare distances that were never meant to add up.
    """
    strokes = (
        _segment("0.05", "0.40", "0.25", "0.40"),
        _segment("0.60", "0.40", "0.90", "0.40"),
        _segment("0.05", "0.30", "0.05", "0.50"),
        _segment("0.25", "0.30", "0.25", "0.50"),
        _segment("0.60", "0.30", "0.60", "0.50"),
        _segment("0.90", "0.30", "0.90", "0.50"),
    )

    found = _detect(*strokes)

    assert len(found.lines) == 2
    assert found.chains == ()


# ---------------------------------------------------------------------------
# What it will not do
# ---------------------------------------------------------------------------


def test_an_oblique_stroke_is_left_unclassified() -> None:
    """Input: a diagonal. Outcome: unclassified, not forced onto the nearer axis.

    An oblique dimension is real and this does not pretend to find one: there is no single
    perpendicular direction to look along without the rotated frame `deskew.py` owns.
    """
    found = _detect(_segment("0.20", "0.20", "0.60", "0.60"))

    assert found.lines == ()
    assert len(found.unclassified) == 1


def test_geometry_from_two_sheets_is_refused() -> None:
    """**Input: segments from two pages. Outcome: `ValueError`.**

    A dimension line bounded by a witness line from another sheet is not a wrong answer but a
    meaningless one — which is why `DimensionExtent` carries its page and document at all.
    """
    with pytest.raises(ValueError, match="more than one page"):
        _detect(
            _segment("0.20", "0.40", "0.60", "0.40", page=0),
            _segment("0.20", "0.30", "0.20", "0.50", page=1),
        )


def test_no_threshold_has_a_default() -> None:
    """**Outcome: every threshold must be passed.**

    They are properties of a vendor's CAD output, not of drawings in general. A default would ship
    one sheet's measurements as though they were universal, and the call site that inherited it
    would have no way of knowing it had.
    """
    with pytest.raises(TypeError):
        detect([_segment("0.20", "0.40", "0.60", "0.40")])  # type: ignore[call-arg]


def test_no_segments_is_not_an_error() -> None:
    """Input: nothing. Outcome: an empty result.

    A page with no line-work is an ordinary page, not a failure — and `PageLayers.geometry_read` is
    what distinguishes *nothing there* from *nobody looked*.
    """
    found = detect(
        (),
        witness_tolerance=TOLERANCE,
        minimum_span=MINIMUM,
        straightness=STRAIGHT,
        crossing_margin=MARGIN,
    )

    assert found.lines == ()
    assert found.chains == ()
    assert found.unclassified == ()


def test_nothing_here_says_what_a_dimension_measures() -> None:
    """**The boundary.** Outcome: no semantic vocabulary anywhere in the module.

    Finding the line is geometry. Deciding that the geometry it bounds is a cabinet, a filler or a
    countertop is meaning, and `docs/DESIGN_EXTRACTION.md` §6 names assigning it from position as
    the failure this layer exists to prevent: the number reads correctly, the arithmetic is exact,
    and the finding is about the wrong thing.
    """
    import ast
    from pathlib import Path

    import extraction.geometry.dimension_lines as module

    source = module.__file__
    assert source is not None
    tree = ast.parse(Path(source).read_text(encoding="utf-8"))
    names = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
    }

    forbidden = sorted(
        name
        for name in names
        if any(word in name.lower() for word in ("semantic", "cabinet", "filler", "countertop"))
    )
    assert not forbidden, f"the detector reaches for meaning: {forbidden}"


# ---------------------------------------------------------------------------
# Closures — the drawing checking itself
# ---------------------------------------------------------------------------


def _at(y: str, x0: str, x1: str) -> tuple[DimensionExtent, ...]:
    """A horizontal dimension at `y` from `x0` to `x1`, crossed at both ends."""
    return (
        _segment(x0, y, x1, y),
        _segment(x0, str(Decimal(y) - Decimal("0.10")), x0, str(Decimal(y) + Decimal("0.10"))),
        _segment(x1, str(Decimal(y) - Decimal("0.10")), x1, str(Decimal(y) + Decimal("0.10"))),
    )


def test_an_overall_dimension_tiled_by_smaller_ones_is_a_closure() -> None:
    """**Input: an overall, and three parts that tile it on another row. Outcome: one closure.**

    This is the drawing checking itself. A run dimensioned segment by segment *and* again overall
    asserts that the parts make up the whole, and `CT-WIDTH-001` is written against exactly that
    arrangement.

    Across rows on purpose: a drawing puts the overall on one line and the breakdown above it.
    `_chains` requires one offset and would find none of these.
    """
    strokes = (
        *_at("0.60", "0.10", "0.90"),
        *_at("0.40", "0.10", "0.30"),
        *_at("0.40", "0.30", "0.55"),
        *_at("0.40", "0.55", "0.90"),
    )

    found = _detect(*strokes)

    assert len(found.closures) == 1
    closure = found.closures[0]
    assert closure.axis is Axis.HORIZONTAL
    assert closure.overall.span == Decimal("0.80")
    assert len(closure.parts) == 3
    # Spatial only: the parts tile the overall's extent. Whether their *stated numbers* sum to its
    # stated number is arithmetic, and B3's to check — which is what keeps this from being circular.
    assert sum((part.span for part in closure.parts), Decimal(0)) == closure.overall.span


def test_dimensions_merely_inside_a_longer_one_are_not_a_closure() -> None:
    """**Input: two short dimensions floating inside a long one. Outcome: no closure.**

    Containment alone says nothing — they might measure two unrelated features that happen to fall
    within it. A breakdown starts where the overall starts, ends where it ends, and meets in
    between. Without that, running a closure check would compare numbers the drawing never claimed
    were related.
    """
    strokes = (
        *_at("0.60", "0.10", "0.90"),
        *_at("0.40", "0.20", "0.35"),
        *_at("0.40", "0.55", "0.70"),
    )

    found = _detect(*strokes)

    assert found.closures == ()


def test_a_gap_in_the_breakdown_is_not_a_closure() -> None:
    """Input: parts that start and end right but leave a gap. Outcome: none.

    The boundary case for "with nothing left over". A gap means something inside the overall is
    undimensioned, and the parts cannot be said to account for it.
    """
    strokes = (
        *_at("0.60", "0.10", "0.90"),
        *_at("0.40", "0.10", "0.30"),
        *_at("0.40", "0.45", "0.90"),
    )

    found = _detect(*strokes)

    assert found.closures == ()


def test_the_same_dimension_drawn_on_two_rows_is_not_its_own_breakdown() -> None:
    """**Input: one dimension repeated on another row. Outcome: no closure.**

    A drawing repeats an overall above and below a run often enough that this matters: without the
    strictly-shorter rule, each copy would be reported as the other's single-part breakdown, and a
    closure check would then confirm that a number equals itself.
    """
    strokes = (
        *_at("0.60", "0.10", "0.90"),
        *_at("0.30", "0.10", "0.90"),
    )

    found = _detect(*strokes)

    assert found.closures == ()


def test_nothing_in_a_closure_reads_a_value() -> None:
    """**The boundary that stops this being circular.** Outcome: closures come from geometry alone.

    If which dimensions formed a breakdown were decided by trying combinations until the numbers
    added up, the closure check would then verify that they add up — and pass on every drawing,
    including one with a real error in it. So the tiling is spatial, and the arithmetic stays
    `B3`'s: a closure found here can still fail the check that uses it.
    """
    import ast
    import inspect
    import textwrap

    from extraction.geometry import dimension_lines

    # Read off the identifiers these two functions actually reference, not their text. A text scan
    # trips on the docstring that explains the rule — "nothing here reads a value" contains
    # "value" — and a guard a comment can fail is a guard somebody deletes.
    referenced: set[str] = set()
    for function in (dimension_lines._closures, dimension_lines._tiling):
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        referenced |= {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(tree)
            if isinstance(node, (ast.Name, ast.Attribute))
        }

    reaching = sorted(
        name
        for name in referenced
        if any(word in name.lower() for word in ("value", "measure", "raw_text", "total", "sum"))
    )
    assert not reaching, f"closure detection reaches for {reaching}"
