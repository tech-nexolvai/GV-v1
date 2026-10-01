"""Drawing a character the way the file draws it (#756 phase A).

Verification for: `extraction/glyph_shapes.py`, and the path data `extraction/annotations.py` now
keeps for it.

The first test is the acceptance criterion: an inch mark is two ticks, and a reader that kept only
points drew it as one joined stroke — so two different characters could rasterise alike, and no
matching done afterwards could tell them apart.

Every fixture is authored geometry. No client drawing is read here.
"""

from __future__ import annotations

import math
from decimal import Decimal
from fractions import Fraction

import numpy as np
import pytest

from extraction.annotations import PathSegment, SegmentKind, VectorPath, read_annotation_layers
from extraction.glyph_shapes import (
    GlyphShape,
    ShapeSettings,
    UndrawablePath,
    chamfer,
    cluster_shapes,
    describe,
    overlap,
    rasterise,
    size_ratio,
    subpaths,
)
from tests.extraction.test_annotations import DOCUMENT, DPI, _appearance, _pdf, _stamp

SIZE = 33
STEPS = 8
STROKE = 1


def _point(x: str | int, y: str | int) -> tuple[Decimal, Decimal]:
    return (Decimal(x), Decimal(y))


def _path(
    *steps: tuple[SegmentKind, tuple[Decimal, Decimal]], closes_last: bool = False
) -> VectorPath:
    segments = tuple(
        PathSegment(kind=kind, point=point, closes=closes_last and index == len(steps) - 1)
        for index, (kind, point) in enumerate(steps)
    )
    return VectorPath(segments=segments, stroked=True, filled=False)


MOVE, LINE, BEZIER = SegmentKind.MOVE, SegmentKind.LINE, SegmentKind.BEZIER

#: An inch mark: two ticks, each its own sub-path. Ten tall, four apart.
INCH_MARK = _path(
    (MOVE, _point(0, 0)), (LINE, _point(0, 10)), (MOVE, _point(4, 0)), (LINE, _point(4, 10))
)

#: The same four points with the pen never lifted — what keeping only points amounted to.
JOINED = _path(
    (MOVE, _point(0, 0)), (LINE, _point(0, 10)), (LINE, _point(4, 0)), (LINE, _point(4, 10))
)


def _draw(*paths: VectorPath) -> np.ndarray:
    return rasterise(paths, size_px=SIZE, bezier_steps=STEPS, stroke_px=STROKE)


def test_an_inch_mark_rasterises_as_two_ticks_not_one_joined_stroke() -> None:
    """**The acceptance criterion.** Outcome: nothing is drawn between the two ticks.

    Joined, the pen runs diagonally from the top of the first tick to the foot of the second, which
    crosses the middle of the raster. Lifted where the file lifts it, the middle stays empty.
    """
    two_ticks = _draw(INCH_MARK)
    joined = _draw(JOINED)
    middle = SIZE // 2

    assert not two_ticks[:, middle].any(), "nothing may cross between the two ticks"
    assert joined[:, middle].any(), "the control: without the lifted pen, a stroke crosses"
    assert len(subpaths(INCH_MARK, bezier_steps=STEPS)) == 2


def test_a_bezier_is_drawn_along_its_curve_not_through_its_control_points() -> None:
    """Outcome: the corner the control points sit on is empty.

    A curve from the bottom-left to the top-right with both control points on the top-left corner.
    Drawn through its control points it runs into that corner; along the curve it passes well
    inside it (at t = ½ the curve is at (1.25, 8.75)).
    """
    arc = _path(
        (MOVE, _point(0, 0)),
        (BEZIER, _point(0, 10)),
        (BEZIER, _point(0, 10)),
        (BEZIER, _point(10, 10)),
    )

    raster = _draw(arc)

    assert raster[0, 0] == 0, "the top-left corner is a control point, not on the curve"
    assert raster[SIZE - 1, 0] == 255, "the curve starts bottom-left"
    assert raster[0, SIZE - 1] == 255, "and ends top-right"


def test_a_closed_sub_path_is_closed() -> None:
    """Outcome: a triangle whose last segment closes it has its third side drawn."""
    triangle = _path(
        (MOVE, _point(0, 0)), (LINE, _point(10, 0)), (LINE, _point(0, 10)), closes_last=True
    )
    open_ = _path((MOVE, _point(0, 0)), (LINE, _point(10, 0)), (LINE, _point(0, 10)))

    # The left edge runs from (0, 0) to (0, 10): only the closing segment draws it.
    left_edge = SIZE // 2, 0
    assert _draw(triangle)[left_edge] == 255
    assert _draw(open_)[left_edge] == 0


def test_the_aspect_is_kept_so_a_one_stays_a_thin_stroke() -> None:
    """Outcome: a vertical stroke fills the height and one column, not the whole square."""
    one = _path((MOVE, _point(0, 0)), (LINE, _point(0, 10)))

    raster = _draw(one)

    columns = np.flatnonzero(raster.any(axis=0))
    assert len(columns) == STROKE
    assert raster[:, columns[0]].all()


def test_a_filled_outline_is_filled() -> None:
    """Outcome: the inside of a filled square is ink; the inside of a stroked one is not."""
    corners = (
        (MOVE, _point(0, 0)),
        (LINE, _point(10, 0)),
        (LINE, _point(10, 10)),
        (LINE, _point(0, 10)),
    )
    stroked = _path(*corners, closes_last=True)
    filled = VectorPath(segments=stroked.segments, stroked=False, filled=True)

    centre = SIZE // 2, SIZE // 2
    assert _draw(filled)[centre] == 255
    assert _draw(stroked)[centre] == 0


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        (
            _path((MOVE, _point(0, 0)), (SegmentKind.UNKNOWN, _point(1, 1))),
            "does not name",
        ),
        (
            _path((MOVE, _point(0, 0)), (BEZIER, _point(1, 1)), (BEZIER, _point(2, 2))),
            "part-way",
        ),
        (
            VectorPath(segments=INCH_MARK.segments, stroked=None, filled=False),
            "line or a filled shape",
        ),
        (
            VectorPath(segments=INCH_MARK.segments, stroked=False, filled=False),
            "nothing is drawn",
        ),
        (_path((MOVE, _point(3, 3))), "no extent"),
    ],
)
def test_a_path_that_cannot_be_drawn_faithfully_is_refused(path: VectorPath, reason: str) -> None:
    """Outcome: `UndrawablePath` with the reason, never an approximate drawing read as a character."""
    with pytest.raises(UndrawablePath, match=reason):
        _draw(path)


def test_no_size_or_resolution_is_assumed() -> None:
    """Outcome: every drawing parameter is the caller's, and nonsense is refused."""
    with pytest.raises(TypeError):
        rasterise((INCH_MARK,))  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="size_px"):
        rasterise((INCH_MARK,), size_px=0, bezier_steps=STEPS, stroke_px=STROKE)
    with pytest.raises(ValueError, match="bezier_steps"):
        rasterise((INCH_MARK,), size_px=SIZE, bezier_steps=0, stroke_px=STROKE)


# ---------------------------------------------------------------------------
# What the reader keeps from a real file
# ---------------------------------------------------------------------------

#: A label as the client's drawing draws it: a `2` as one stroked path, and an inch mark as ONE path
#: with two sub-paths. Plus one filled glyph and one curved one. Appearance space, not page space.
SHAPES_APPEARANCE = (
    b"0.2 w 110 520 m 113.6 525.5 l 110 525.5 l S\n"  # 2
    b"114.5 523.9 m 115 525.5 l 116.1 523.9 m 116.6 525.5 l S\n"  # inch mark, two ticks
    b"117.5 520 m 120.5 520 l 120.5 525 l 117.5 525 l h f\n"  # a filled block
    b"121.5 520 m 121.5 523 123 525.5 124.5 525.5 c S\n"  # a curve
)


def _shape_layers():
    return read_annotation_layers(
        _pdf(
            annotations=[_stamp(appearance_object=6)],
            extra_objects=[_appearance(SHAPES_APPEARANCE)],
        ),
        0,
        document_version_id=DOCUMENT,
        dpi=DPI,
        line_minimum_pt=Decimal(6),
        glyph_maximum_pt=Decimal(6),
        glyph_gap_pt=Decimal(4),
    )


def test_a_region_carries_its_member_paths_with_their_steps_and_draw_mode() -> None:
    """**The second acceptance criterion.** Outcome: the region holds its four paths as drawn."""
    (region,) = _shape_layers().outlined_regions
    two, inch, block, curve = region.glyph_paths

    assert [segment.kind for segment in two.segments] == [MOVE, LINE, LINE]
    assert [segment.kind for segment in inch.segments] == [MOVE, LINE, MOVE, LINE]
    assert (two.stroked, two.filled) == (True, False)
    assert (block.stroked, block.filled) == (False, True)
    assert block.segments[-1].closes, "`h` closes the block's sub-path"
    assert [segment.kind for segment in curve.segments] == [MOVE, BEZIER, BEZIER, BEZIER]


def test_the_paths_are_in_page_space_and_match_the_region() -> None:
    """Outcome: every member point lies inside the region's page-space box.

    The stamp's appearance is placed on the page by an affine, and the region is in page space; a
    member path left in appearance space would sit somewhere else entirely.
    """
    (region,) = _shape_layers().outlined_regions
    points = [point for path in region.glyph_paths for point in path.points]
    xs = [x for x, _ in points]
    ys = [y for _, y in points]

    # The fixture's stamp places appearance (100, 500) at page (50, 50).
    assert min(xs) == Decimal(60) and min(ys) == Decimal(70)
    assert region.path_count == len(region.glyph_paths)
    assert region.point_count == len(points)


def test_the_inch_marks_ticks_survive_from_the_file_to_the_raster() -> None:
    """Outcome: the inch mark read from a real appearance stream still draws as two ticks."""
    (region,) = _shape_layers().outlined_regions
    inch = region.glyph_paths[1]

    raster = _draw(inch)

    assert not raster[:, SIZE // 2].any()
    assert len(subpaths(inch, bezier_steps=STEPS)) == 2


def test_a_regions_paths_do_not_change_what_the_region_is() -> None:
    """Outcome: two regions that differ only in their member paths are equal.

    A region is its place and counts. The paths are what a shape reader reads, not part of which
    region it is — so nothing that compares regions changed when they started carrying them.
    """
    from dataclasses import replace

    (region,) = _shape_layers().outlined_regions

    assert replace(region, glyph_paths=()) == region


# ---------------------------------------------------------------------------
# Phase B: describing, comparing and grouping shapes
# ---------------------------------------------------------------------------


SETTINGS = ShapeSettings(size_px=24, bezier_steps=8, stroke_px=1, dilate_px=1)
RUN = Decimal(10)


def _polygon(sides: int, radius: str = "5") -> VectorPath:
    """A closed regular polygon, its points rounded to thousandths so the geometry is exact."""
    r = Decimal(radius)
    points = [
        (
            Decimal(str(round(math.cos(2 * math.pi * k / sides), 3))) * r,
            Decimal(str(round(math.sin(2 * math.pi * k / sides), 3))) * r,
        )
        for k in range(sides)
    ]
    steps = [(MOVE, points[0])] + [(LINE, point) for point in points[1:]]
    return _path(*steps, closes_last=True)


VERTICAL = _path((MOVE, _point(0, 0)), (LINE, _point(0, 10)))
TICK = _path((MOVE, _point(0, 7)), (LINE, _point(0, 10)))
HORIZONTAL = _path((MOVE, _point(0, 5)), (LINE, _point(6, 5)))
DOT = _path((MOVE, _point(2, 2)))


def test_a_shape_is_sized_exactly_against_its_label() -> None:
    """Outcome: a 10-high stroke in a 10-high run is height 1; a 3-high tick is 3/10, exactly."""
    assert describe(VERTICAL, run_height=RUN, settings=SETTINGS).relative_height == 1
    tick = describe(TICK, run_height=RUN, settings=SETTINGS)
    assert tick.relative_height == Fraction(3, 10)
    assert tick.relative_width == 0


def test_a_single_point_is_a_dot_not_a_refusal() -> None:
    """Outcome: 395 of the client's member paths are single points; each becomes a dot (#756)."""
    dot = describe(DOT, run_height=RUN, settings=SETTINGS)

    assert dot.dot
    assert not dot.raster.any()


def test_a_character_cannot_be_sized_against_nothing() -> None:
    with pytest.raises(ValueError, match="run_height"):
        describe(VERTICAL, run_height=Decimal(0), settings=SETTINGS)


def test_overlap_is_an_exact_ratio() -> None:
    """Outcome: the same shape is 1, disjoint shapes are 0, and nothing in between is a float."""
    a = describe(VERTICAL, run_height=RUN, settings=SETTINGS).raster
    b = describe(HORIZONTAL, run_height=RUN, settings=SETTINGS).raster

    assert overlap(a, a) == 1
    score = overlap(a, b)
    assert isinstance(score, Fraction)
    assert 0 < score < 1


def test_chamfer_distance_is_exact_symmetric_and_zero_for_the_same_shape() -> None:
    a = describe(VERTICAL, run_height=RUN, settings=SETTINGS).raster
    b = describe(HORIZONTAL, run_height=RUN, settings=SETTINGS).raster

    assert chamfer(a, a) == 0
    assert chamfer(a, b) == chamfer(b, a)
    assert isinstance(chamfer(a, b), Fraction) and chamfer(a, b) > 0
    with pytest.raises(ValueError, match="ink"):
        chamfer(a, np.zeros_like(a))


def test_a_flat_stroke_is_never_the_same_size_as_a_character_with_width() -> None:
    """Outcome: a perfectly vertical line has width 0, and no ratio makes that equal to a `0`."""
    line = describe(VERTICAL, run_height=RUN, settings=SETTINGS)
    ring = describe(_polygon(8), run_height=RUN, settings=SETTINGS)

    assert size_ratio(line, line) == 1
    assert size_ratio(line, ring) > 1000


def test_the_same_character_drawn_with_different_point_counts_clusters_together() -> None:
    """**The measured obstacle** (#756). Outcome: an octagon `0` and a 24-point `0` are one cluster.

    Exact point fingerprints gave 951–1,124 "distinct shapes" on the client's drawing, because the
    same character is stored with different point counts at different sizes. Compared as dilated
    rasters, the two are one shape.
    """
    shapes = [
        describe(_polygon(8), run_height=RUN, settings=SETTINGS),
        describe(_polygon(24), run_height=RUN, settings=SETTINGS),
    ]

    clusters = cluster_shapes(
        shapes, minimum_overlap=Decimal("0.5"), maximum_size_ratio=Decimal("1.3")
    )

    assert clusters == [[0, 1]]


def test_size_separates_what_shape_alone_cannot() -> None:
    """Outcome: a full-height stroke and a tick are one line normalised, and two clusters by size."""
    shapes = [
        describe(VERTICAL, run_height=RUN, settings=SETTINGS),
        describe(TICK, run_height=RUN, settings=SETTINGS),
        describe(VERTICAL, run_height=RUN, settings=SETTINGS),
    ]

    clusters = cluster_shapes(
        shapes, minimum_overlap=Decimal("0.5"), maximum_size_ratio=Decimal("1.3")
    )

    assert overlap(shapes[0].raster, shapes[1].raster) == 1, "the same line once normalised"
    assert clusters == [[0, 2], [1]]


def test_every_dot_is_one_cluster_and_clustering_is_deterministic() -> None:
    shapes = [
        describe(DOT, run_height=RUN, settings=SETTINGS),
        describe(VERTICAL, run_height=RUN, settings=SETTINGS),
        describe(DOT, run_height=RUN, settings=SETTINGS),
        describe(HORIZONTAL, run_height=RUN, settings=SETTINGS),
    ]
    arguments = {"minimum_overlap": Decimal("0.5"), "maximum_size_ratio": Decimal("1.3")}

    first = cluster_shapes(shapes, **arguments)

    assert first == [[0, 2], [1], [3]]
    assert cluster_shapes(shapes, **arguments) == first


def test_grouping_thresholds_are_the_callers_and_are_checked() -> None:
    with pytest.raises(ValueError, match="minimum_overlap"):
        cluster_shapes([], minimum_overlap=Decimal(0), maximum_size_ratio=Decimal(1))
    with pytest.raises(ValueError, match="maximum_size_ratio"):
        cluster_shapes([], minimum_overlap=Decimal("0.5"), maximum_size_ratio=Decimal("0.9"))
    with pytest.raises(ValueError, match="dilate_px"):
        ShapeSettings(size_px=24, bezier_steps=8, stroke_px=1, dilate_px=-1)
    assert isinstance(GlyphShape, type)


def test_a_single_straight_segment_is_marked_straight_and_nothing_else_is() -> None:
    """Outcome: vertical, horizontal and slanted strokes are straight; a ring and an inch mark are not."""
    slant = _path((MOVE, _point(0, 0)), (LINE, _point(4, 10)))

    for path in (VERTICAL, HORIZONTAL, slant):
        assert describe(path, run_height=RUN, settings=SETTINGS).straight
    for path in (_polygon(8), INCH_MARK):
        assert not describe(path, run_height=RUN, settings=SETTINGS).straight
