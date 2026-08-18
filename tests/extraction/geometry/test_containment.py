"""What a dimension line measures — and, more importantly, when it refuses to say.

§9 of `docs/DESIGN_EXTRACTION.md` requires that every resolver which can fail has its *cannot-resolve*
path asserted, not only its success path. Those are the safety-critical cases here: a wrong
association produces a finding that is internally consistent, fully traced and completely wrong, and
nothing downstream can catch it.

The fixtures are deliberately geometric rather than realistic. `data/drawings/` is empty, and a
fixture built to look like a real elevation would be encoding today's guess about real elevations as
ground truth — §9 again. These check the logic, and the characterisation tests come with the
drawings.
"""

from __future__ import annotations

import inspect
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from evidence.coordinates import StoredPoint
from evidence.polygon import Polygon, PolygonSpaceMismatchError
from extraction.geometry.containment import (
    CannotResolve,
    DimensionExtent,
    Span,
    resolve_span,
)
from extraction.model.items import DrawingItem, ViewIdentity
from vocabulary.semantic_types import SemanticType

DOCUMENT = uuid4()
PAGE = 3
TOLERANCE = Decimal("0.001")


def _d(value: str) -> Decimal:
    return Decimal(value)


def _box(
    x0: str,
    x1: str,
    y0: str = "0.20",
    y1: str = "0.30",
    *,
    document: UUID | None = None,
    page: int | None = None,
) -> Polygon:
    """A rectangle in stored space. Only the axis under test carries meaning."""
    return Polygon(
        points=(
            StoredPoint(_d(x0), _d(y0)),
            StoredPoint(_d(x1), _d(y0)),
            StoredPoint(_d(x1), _d(y1)),
            StoredPoint(_d(x0), _d(y1)),
        ),
        space="stored",
        document_version_id=document or DOCUMENT,
        page=PAGE if page is None else page,
    )


def _item(
    x0: str,
    x1: str,
    kind: SemanticType = SemanticType.CT001,
    y0: str = "0.20",
    y1: str = "0.30",
    *,
    document: UUID | None = None,
    page: int | None = None,
) -> DrawingItem:
    resolved_document = document or DOCUMENT
    resolved_page = PAGE if page is None else page
    return DrawingItem(
        view=ViewIdentity(document_version_id=resolved_document, page=resolved_page, tag="D"),
        item_type=kind,
        extent=_box(x0, x1, y0, y1, document=resolved_document, page=resolved_page),
    )


def _line(x0: str, x1: str, y: str = "0.10") -> DimensionExtent:
    return DimensionExtent(
        start=StoredPoint(_d(x0), _d(y)),
        end=StoredPoint(_d(x1), _d(y)),
        document_version_id=DOCUMENT,
        page=PAGE,
    )


# ---------------------------------------------------------------------------
# Single versus run — the difference is a whole check
# ---------------------------------------------------------------------------


def test_a_dimension_over_one_item_resolves_to_that_item() -> None:
    item = _item("0.10", "0.40")
    result = resolve_span(_line("0.10", "0.40"), (item,), endpoint_tolerance=TOLERANCE)

    assert isinstance(result, Span)
    assert result.kind == "single"
    assert result.items == (item,)


def test_a_dimension_over_three_adjacent_items_resolves_to_a_run() -> None:
    """ "The countertop is as wide as the cabinets beneath it" needs to know it is summing three
    cabinets, not measuring one. A caller cannot infer that from a list length without guessing."""
    left = _item("0.10", "0.20")
    middle = _item("0.20", "0.30")
    right = _item("0.30", "0.40")

    result = resolve_span(
        _line("0.10", "0.40"), (right, left, middle), endpoint_tolerance=TOLERANCE
    )

    assert isinstance(result, Span)
    assert result.kind == "run"
    assert result.items == (
        left,
        middle,
        right,
    ), "a run is ordered along the axis, not as passed in"


def test_a_run_is_never_reported_as_a_single_item() -> None:
    """The failure that matters: a rule summing a run silently sums one thing and passes."""
    items = (_item("0.10", "0.25"), _item("0.25", "0.40"))
    result = resolve_span(_line("0.10", "0.40"), items, endpoint_tolerance=TOLERANCE)

    assert isinstance(result, Span)
    assert result.kind != "single"
    assert len(result.items) == 2


def test_a_single_item_wins_over_the_run_that_shares_its_extent() -> None:
    """A countertop drawn over three cabinets is one item the dimension can be about. When something
    aligns exactly, that is the answer — assembling a run instead would sum the parts of the thing
    the line was already pointing at."""
    countertop = _item("0.10", "0.40", SemanticType.CT002, y0="0.30", y1="0.34")
    cabinets = (_item("0.10", "0.20"), _item("0.20", "0.30"), _item("0.30", "0.40"))

    result = resolve_span(
        _line("0.10", "0.40"), (*cabinets, countertop), endpoint_tolerance=TOLERANCE
    )

    assert isinstance(result, Span)
    assert result.kind == "single"
    assert result.items == (countertop,)


def test_a_vertical_dimension_is_measured_down_the_page() -> None:
    """Heights are dimensioned too, and an implementation that only projected onto x would resolve
    every one of them to whatever happened to share its x extent."""
    item = _item("0.10", "0.40", y0="0.20", y1="0.60")
    line = DimensionExtent(
        start=StoredPoint(_d("0.05"), _d("0.20")),
        end=StoredPoint(_d("0.05"), _d("0.60")),
        document_version_id=DOCUMENT,
        page=PAGE,
    )

    result = resolve_span(line, (item,), endpoint_tolerance=TOLERANCE)
    assert isinstance(result, Span)
    assert result.items == (item,)


# ---------------------------------------------------------------------------
# Nesting resolves to the most specific, and says why
# ---------------------------------------------------------------------------


def test_a_nested_item_wins_and_the_rationale_names_what_it_beat() -> None:
    """A drawer front inside a cabinet, both flush with the dimension. The inner one is what the
    line can most specifically be about, and §2.4 requires the reason to be legible."""
    cabinet = _item("0.10", "0.40", SemanticType.CT001, y0="0.10", y1="0.50")
    drawer = _item("0.10", "0.40", SemanticType.CT003, y0="0.20", y1="0.30")

    result = resolve_span(_line("0.10", "0.40"), (cabinet, drawer), endpoint_tolerance=TOLERANCE)

    assert isinstance(result, Span)
    assert result.items == (drawer,)
    assert SemanticType.CT001.value in result.rationale, "the rationale must say what it beat"
    assert "inside" in result.rationale


def test_every_span_records_why_it_resolved_that_way() -> None:
    """A rationale is not optional decoration. A reviewer shown a finding has to be able to see the
    reasoning, and the type refuses to be built without one."""
    result = resolve_span(
        _line("0.10", "0.40"), (_item("0.10", "0.40"),), endpoint_tolerance=TOLERANCE
    )
    assert isinstance(result, Span)
    assert len(result.rationale.split()) > 3


# ---------------------------------------------------------------------------
# Refusals — the safety-critical half
# ---------------------------------------------------------------------------


def test_two_equally_good_candidates_refuse_rather_than_pick_one() -> None:
    """**The finding this story exists to prevent.** Neither item is inside the other, so there is no
    ground for preferring either, and the nearest guess is what must not be returned."""
    left = _item("0.10", "0.40", SemanticType.CT001, y0="0.20", y1="0.30")
    right = _item("0.10", "0.40", SemanticType.CT002, y0="0.60", y1="0.70")

    result = resolve_span(_line("0.10", "0.40"), (left, right), endpoint_tolerance=TOLERANCE)

    assert isinstance(result, CannotResolve)
    assert set(result.candidates) == {left, right}, "a reviewer sees what the choice was between"


def test_a_gap_in_the_run_refuses_rather_than_summing_across_it() -> None:
    """Something between them is missing or unread. Treating the group as one run would sum two
    cabinets and report the width of three."""
    items = (_item("0.10", "0.20"), _item("0.30", "0.40"))
    result = resolve_span(_line("0.10", "0.40"), items, endpoint_tolerance=TOLERANCE)

    assert isinstance(result, CannotResolve)
    assert "gap" in result.reason


def test_overlapping_items_refuse_rather_than_counting_the_overlap_twice() -> None:
    items = (_item("0.10", "0.30"), _item("0.20", "0.40"))
    result = resolve_span(_line("0.10", "0.40"), items, endpoint_tolerance=TOLERANCE)

    assert isinstance(result, CannotResolve)
    assert "twice" in result.reason


def test_items_that_do_not_reach_the_ends_of_the_line_refuse() -> None:
    """The line measures something wider than everything found under it — so what it measures has
    not been read yet, and the honest answer is that nobody knows."""
    items = (_item("0.15", "0.25"), _item("0.25", "0.35"))
    result = resolve_span(_line("0.10", "0.40"), items, endpoint_tolerance=TOLERANCE)

    assert isinstance(result, CannotResolve)


def test_no_items_at_all_refuses() -> None:
    result = resolve_span(_line("0.10", "0.40"), (), endpoint_tolerance=TOLERANCE)
    assert isinstance(result, CannotResolve)
    assert result.candidates == ()


def test_a_diagonal_line_has_no_axis_and_refuses() -> None:
    """At exactly 45° there is no reason to project onto x rather than y. Picking either would make
    the answer depend on which comparison happened to be written first."""
    line = DimensionExtent(
        start=StoredPoint(_d("0.10"), _d("0.10")),
        end=StoredPoint(_d("0.40"), _d("0.40")),
        document_version_id=DOCUMENT,
        page=PAGE,
    )

    result = resolve_span(line, (_item("0.10", "0.40"),), endpoint_tolerance=TOLERANCE)
    assert isinstance(result, CannotResolve)
    assert "axis" in result.reason


def test_a_refusal_always_says_what_could_not_be_decided() -> None:
    with pytest.raises(ValueError, match="what could not be decided"):
        CannotResolve("  ")


# ---------------------------------------------------------------------------
# The tolerance is a stated parameter
# ---------------------------------------------------------------------------


def test_the_endpoint_tolerance_is_a_required_keyword_argument() -> None:
    """The acceptance criterion, asserted on the signature. The number separating "spans this
    cabinet" from "spans the run" is empirical and `data/drawings/` is empty — a default would be
    today's guess shipped as ground truth, and a positional one could be passed by accident."""
    parameter = inspect.signature(resolve_span).parameters["endpoint_tolerance"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


def test_the_tolerance_actually_changes_the_answer() -> None:
    """Otherwise it is a parameter in name only. The same drawing resolves or refuses depending on
    what the caller states, which is the whole point of it being theirs to state."""
    items = (_item("0.10", "0.20"), _item("0.2004", "0.40"))
    line = _line("0.10", "0.40")

    assert isinstance(
        resolve_span(line, items, endpoint_tolerance=Decimal("0.0001")), CannotResolve
    )
    assert isinstance(resolve_span(line, items, endpoint_tolerance=Decimal("0.001")), Span)


def test_a_float_tolerance_is_refused() -> None:
    """Binary rounding deciding whether a dimension spans one cabinet or three is exactly the class
    of failure ADR-0001 exists to prevent."""
    with pytest.raises(TypeError, match="Decimal"):
        resolve_span(
            _line("0.10", "0.40"),
            (_item("0.10", "0.40"),),
            endpoint_tolerance=0.001,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_a_tolerance_that_is_not_a_finite_number_is_refused(value: str) -> None:
    """`Decimal("NaN")` is a `Decimal` and `Decimal("NaN") < 0` is `False`, so it passed both earlier
    checks — and then every `abs(...) <= NaN` below was `False` too. A dimension line sitting exactly
    on an item refused, with the reason "no item starts and ends where the dimension line does".

    That is the failure this module exists to prevent, wearing the costume of the safe answer: a
    reviewer reads the refusal and goes looking for a cabinet that was never missing. Infinity is the
    mirror image — everything aligns and whichever item comes first wins.
    """
    item = _item("0.10", "0.40")
    line = _line("0.10", "0.40")

    assert isinstance(resolve_span(line, (item,), endpoint_tolerance=TOLERANCE), Span)
    with pytest.raises(ValueError, match="finite"):
        resolve_span(line, (item,), endpoint_tolerance=Decimal(value))


def test_a_negative_tolerance_is_refused() -> None:
    with pytest.raises(ValueError, match="negative"):
        resolve_span(
            _line("0.10", "0.40"),
            (_item("0.10", "0.40"),),
            endpoint_tolerance=Decimal("-0.001"),
        )


# ---------------------------------------------------------------------------
# Coordinate planes
# ---------------------------------------------------------------------------


def test_an_item_from_another_page_raises_rather_than_resolving() -> None:
    """Not ambiguity to mark for review — a caller mistake. Answering it would mean comparing
    geometry from two different sheets, which is not a wrong answer but a meaningless one."""
    with pytest.raises(PolygonSpaceMismatchError):
        resolve_span(
            _line("0.10", "0.40"),
            (_item("0.10", "0.40", page=PAGE + 1),),
            endpoint_tolerance=TOLERANCE,
        )


def test_an_item_from_another_document_version_raises() -> None:
    with pytest.raises(PolygonSpaceMismatchError):
        resolve_span(
            _line("0.10", "0.40"),
            (_item("0.10", "0.40", document=uuid4()),),
            endpoint_tolerance=TOLERANCE,
        )


# ---------------------------------------------------------------------------
# The extent type refuses nonsense
# ---------------------------------------------------------------------------


def test_a_line_with_identical_endpoints_is_refused() -> None:
    """It spans nothing and has no axis, so every question asked of it would be answered by
    whichever branch happened to run first."""
    with pytest.raises(ValueError, match="spans nothing"):
        DimensionExtent(
            start=StoredPoint(_d("0.10"), _d("0.10")),
            end=StoredPoint(_d("0.10"), _d("0.10")),
            document_version_id=DOCUMENT,
            page=PAGE,
        )


def test_a_point_outside_the_page_is_refused() -> None:
    with pytest.raises(ValueError, match="0..1"):
        DimensionExtent(
            start=StoredPoint(_d("0.10"), _d("0.10")),
            end=StoredPoint(_d("1.40"), _d("0.10")),
            document_version_id=DOCUMENT,
            page=PAGE,
        )


def test_a_span_of_one_item_cannot_claim_to_be_a_run() -> None:
    """The type refuses it, so a future caller assembling a Span by hand cannot make a rule that
    sums a run silently sum one thing."""
    item = _item("0.10", "0.40")
    with pytest.raises(ValueError, match="two or more"):
        Span(extent=_line("0.10", "0.40"), items=(item,), kind="run", rationale="because")
