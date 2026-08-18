"""Which line a number belongs to — and, more importantly, when the answer is "nobody knows".

§9 of `docs/DESIGN_EXTRACTION.md` requires every resolver that can fail to have its *cannot-resolve*
path asserted, not only its success path. Association is the one where that matters most: a wrong
association produces a finding that is internally consistent, fully traced and completely wrong, and
no tolerance check downstream can catch it.

The fixtures are geometric rather than realistic. `data/drawings/` is empty, and a fixture built to
look like a real elevation would encode today's guess about real elevations as ground truth — §9
again. These exercise the logic; the characterisation tests come with the drawings.

The three placement modes tested here — text above the line, text inline in a broken line, text set
clear with a leader — are standard drafting conventions rather than guesses about this vendor. Which
of them GV actually uses, and at what offsets, is exactly what the two stated parameters are for.
"""

from __future__ import annotations

import inspect
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from evidence.coordinates import StoredPoint
from evidence.polygon import Polygon, PolygonSpaceMismatchError
from extraction.geometry.containment import DimensionExtent
from extraction.geometry.text_association import (
    AssociationResult,
    CannotAssociate,
    DimensionText,
    TextAssociation,
    associate,
)

DOCUMENT = uuid4()
PAGE = 3
PROXIMITY = Decimal("0.05")
MARGIN = Decimal("0.001")


def _d(value: str) -> Decimal:
    return Decimal(value)


def _box(
    x0: str,
    x1: str,
    y0: str,
    y1: str,
    *,
    document: UUID | None = None,
    page: int | None = None,
) -> Polygon:
    """A rectangle in stored space, top-left origin, normalised 0..1."""
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


def _text(
    x0: str,
    x1: str,
    y0: str,
    y1: str,
    *,
    rotation: int = 0,
    leader: tuple[str, str] | None = None,
    document: UUID | None = None,
    page: int | None = None,
) -> DimensionText:
    return DimensionText(
        observation_id=uuid4(),
        extent=_box(x0, x1, y0, y1, document=document, page=page),
        rotation_degrees=rotation,
        leader_endpoint=None if leader is None else StoredPoint(_d(leader[0]), _d(leader[1])),
    )


def _horizontal(y: str, x0: str = "0.10", x1: str = "0.40") -> DimensionExtent:
    return DimensionExtent(
        start=StoredPoint(_d(x0), _d(y)),
        end=StoredPoint(_d(x1), _d(y)),
        document_version_id=DOCUMENT,
        page=PAGE,
    )


def _vertical(x: str, y0: str = "0.10", y1: str = "0.50") -> DimensionExtent:
    return DimensionExtent(
        start=StoredPoint(_d(x), _d(y0)),
        end=StoredPoint(_d(x), _d(y1)),
        document_version_id=DOCUMENT,
        page=PAGE,
    )


def _associate(
    texts: tuple[DimensionText, ...],
    lines: tuple[DimensionExtent, ...],
    *,
    proximity: Decimal = PROXIMITY,
    margin: Decimal = MARGIN,
) -> AssociationResult:
    return associate(texts, lines, proximity_limit=proximity, ambiguity_margin=margin)


# ---------------------------------------------------------------------------
# The three placement modes a drafter actually uses
# ---------------------------------------------------------------------------


def test_text_placed_above_the_line_associates_to_it() -> None:
    """The commonest placement: the number set off the line, running along it."""
    line = _horizontal("0.30")
    text = _text("0.22", "0.28", "0.26", "0.29")

    result = _associate((text,), (line,))

    assert result.unassociated == ()
    assert len(result.associated) == 1
    assert result.associated[0].line is line
    assert any("clear of the line" in signal for signal in result.associated[0].signals)


def test_text_inline_in_a_broken_line_associates_to_it() -> None:
    """The other conventional placement: the dimension line is broken and the number sits in the
    gap. The line passes through the text box, so a rule that required the text to be *off* the
    line would miss every one of these."""
    line = _horizontal("0.30")
    text = _text("0.24", "0.26", "0.29", "0.31")

    result = _associate((text,), (line,))

    assert len(result.associated) == 1
    assert any("inline" in signal for signal in result.associated[0].signals)


def test_a_leader_attaches_text_that_is_nowhere_near_its_line() -> None:
    """When the number will not fit by its line, the drafter sets it clear and draws a leader to
    what it labels. The leader endpoint is the drawing itself saying what the text is about, so it
    is measured from instead of the text box — and the text is not rotated to match, because the
    whole reason for the leader is that it could not be placed along the line."""
    line = _vertical("0.60")
    text = _text("0.70", "0.80", "0.70", "0.75", leader=("0.601", "0.30"))

    result = _associate((text,), (line,))

    assert len(result.associated) == 1
    assert result.associated[0].line is line
    assert any("leader" in signal for signal in result.associated[0].signals)


def test_without_its_leader_the_same_text_is_nowhere_near_anything() -> None:
    """The companion to the test above: it is the leader doing the work, not the proximity."""
    line = _vertical("0.60")
    text = _text("0.70", "0.80", "0.70", "0.75")

    result = _associate((text,), (line,))

    assert result.associated == ()
    assert len(result.unassociated) == 1


# ---------------------------------------------------------------------------
# Orientation — rotated and vertical dimension text
# ---------------------------------------------------------------------------


def test_a_vertical_dimension_with_rotated_text_associates() -> None:
    """Heights are dimensioned too, with the number turned on its side to run along the line. An
    implementation that only handled upright text would leave every height unattached."""
    line = _vertical("0.20")
    text = _text("0.16", "0.19", "0.28", "0.32", rotation=90)

    result = _associate((text,), (line,))

    assert len(result.associated) == 1
    assert result.associated[0].line is line


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [(0, "horizontal"), (180, "horizontal"), (90, "vertical"), (270, "vertical")],
)
def test_upside_down_text_reads_along_the_same_axis_as_upright_text(
    rotation: int, expected: str
) -> None:
    """A number rotated 180° is still a horizontal number. Treating 0 and 180 as different
    orientations would refuse half the dimensions on a mirrored elevation."""
    assert _text("0.22", "0.28", "0.26", "0.29", rotation=rotation).reads_along == expected


@pytest.mark.parametrize("rotation", [0, 180])
def test_horizontal_text_never_takes_a_vertical_line(rotation: int) -> None:
    """**A refusal, not a near miss.** The vertical line is well within the proximity limit and is
    the only line on the page — the nearest guess would take it. Dimension text runs along what it
    measures, so a line running the other way is a different dimension whose own text has not been
    read, and saying so is the only safe answer."""
    line = _vertical("0.20")
    text = _text("0.22", "0.26", "0.29", "0.31", rotation=rotation)

    result = _associate((text,), (line,))

    assert result.associated == ()
    assert len(result.unassociated) == 1
    assert "runs" in result.unassociated[0].reason
    assert result.unassociated[0].candidates == (line,)


# ---------------------------------------------------------------------------
# Refusals — the safety-critical half
# ---------------------------------------------------------------------------


def test_two_equally_plausible_lines_produce_no_association() -> None:
    """**The finding this story exists to prevent.** The number sits exactly midway between two
    parallel dimension lines. Which side of a line a vendor prints its text on is empirical, so
    there is no ground for preferring either, and the nearest guess is what must not be returned."""
    above = _horizontal("0.28")
    below = _horizontal("0.32")
    text = _text("0.22", "0.28", "0.295", "0.305")

    result = _associate((text,), (above, below))

    assert result.associated == ()
    assert len(result.unassociated) == 1
    refusal = result.unassociated[0]
    assert set(refusal.candidates) == {above, below}, "a reviewer sees what the choice was between"
    assert "ambiguity margin" in refusal.reason


def test_a_near_tie_inside_the_margin_still_refuses() -> None:
    """Not only exact ties. One line is slightly nearer, but not by enough to call it, and 'slightly
    nearer' is not a reason to attach a number to a dimension."""
    nearer = _horizontal("0.315")
    further = _horizontal("0.28")
    text = _text("0.22", "0.28", "0.295", "0.305")

    result = _associate((text,), (nearer, further), margin=Decimal("0.01"))

    assert result.associated == ()
    assert len(result.unassociated) == 1


def test_the_same_near_tie_resolves_when_the_caller_states_a_tighter_margin() -> None:
    """Otherwise the margin would be a parameter in name only. The same drawing refuses or resolves
    depending on what the caller states, which is the whole point of it being theirs to state."""
    nearer = _horizontal("0.315")
    further = _horizontal("0.28")
    text = _text("0.22", "0.28", "0.295", "0.305")

    result = _associate((text,), (nearer, further), margin=Decimal("0.0001"))

    assert len(result.associated) == 1
    assert result.associated[0].line is nearer


def test_a_line_with_no_dominant_direction_blocks_rather_than_being_skipped() -> None:
    """A line running equally in both directions cannot be judged compatible or incompatible with
    the text. Skipping it would be worse than refusing: the number would fall through to the next
    line along, be attached with full confidence, and the line that was actually nearest would
    appear nowhere in the record."""
    diagonal = DimensionExtent(
        start=StoredPoint(_d("0.20"), _d("0.20")),
        end=StoredPoint(_d("0.40"), _d("0.40")),
        document_version_id=DOCUMENT,
        page=PAGE,
    )
    further = _horizontal("0.34", x0="0.10", x1="0.50")
    text = _text("0.28", "0.32", "0.30", "0.32")

    result = _associate((text,), (diagonal, further))

    assert result.associated == ()
    assert set(result.unassociated[0].candidates) == {diagonal, further}

    without_the_diagonal = _associate((text,), (further,))
    assert (
        len(without_the_diagonal.associated) == 1
    ), "the diagonal is what stopped it — otherwise this test proves nothing"


def test_a_number_beyond_the_end_of_a_line_is_not_that_line_s_text() -> None:
    """Distance is measured to the line segment, not to the infinite line it lies on. A dimension
    stops where the dimension stops, and a number level with a short line but far past its end
    belongs to something else."""
    line = _horizontal("0.30", x0="0.10", x1="0.40")
    text = _text("0.58", "0.62", "0.29", "0.31")

    result = _associate((text,), (line,))

    assert result.associated == ()


def test_a_number_with_no_line_near_it_is_refused_rather_than_attached_to_the_only_one() -> None:
    """A number in a title block and one dimension line on the sheet. Without a proximity limit,
    'nearest' would attach them."""
    line = _horizontal("0.90")
    text = _text("0.22", "0.28", "0.26", "0.29")

    result = _associate((text,), (line,))

    assert result.associated == ()
    assert "proximity limit" in result.unassociated[0].reason


def test_a_leader_that_points_at_nothing_refuses_and_says_so() -> None:
    """The leader was drawn, so the drafter said this number labels something — and that something
    has not been read. Refusing names the right cause, which is what sends a reviewer to the right
    place."""
    line = _horizontal("0.30")
    text = _text("0.70", "0.80", "0.70", "0.75", leader=("0.90", "0.90"))

    result = _associate((text,), (line,))

    assert result.associated == ()
    assert "leader" in result.unassociated[0].reason


def test_a_refusal_always_says_what_could_not_be_decided() -> None:
    with pytest.raises(ValueError, match="what could not be decided"):
        CannotAssociate(_text("0.22", "0.28", "0.26", "0.29"), "   ")


# ---------------------------------------------------------------------------
# Retention — an unassociated number is still evidence of something
# ---------------------------------------------------------------------------


def test_a_number_with_no_lines_on_the_page_at_all_is_retained() -> None:
    """It is evidence of something — quite possibly of a dimension line nobody read. Dropping it
    would turn a missed line into silence."""
    text = _text("0.22", "0.28", "0.26", "0.29")

    result = _associate((text,), ())

    assert result.unassociated[0].text is text
    assert result.unassociated_observation_ids == (text.observation_id,)


def test_every_number_handed_in_comes_back_out_exactly_once() -> None:
    """The invariant the whole result type exists to hold: one attached, one out of range, one
    ambiguous, and nothing quietly lost between them."""
    attached = _text("0.22", "0.28", "0.26", "0.29")
    out_of_range = _text("0.70", "0.80", "0.70", "0.75")
    ambiguous = _text("0.22", "0.28", "0.595", "0.605")
    lines = (_horizontal("0.30"), _horizontal("0.58"), _horizontal("0.62"))

    result = _associate((attached, out_of_range, ambiguous), lines)

    returned = {entry.text.observation_id for entry in result.associated}
    returned |= {entry.text.observation_id for entry in result.unassociated}
    assert returned == {
        attached.observation_id,
        out_of_range.observation_id,
        ambiguous.observation_id,
    }
    assert len(result.associated) == 1
    assert len(result.unassociated) == 2


def test_a_result_refuses_to_report_the_same_number_twice() -> None:
    """Attached *and* retained would be counted twice by anything reading both halves."""
    text = _text("0.22", "0.28", "0.26", "0.29")
    line = _horizontal("0.30")

    with pytest.raises(ValueError, match="twice"):
        AssociationResult(
            associated=(TextAssociation(text=text, line=line, signals=("because",)),),
            unassociated=(CannotAssociate(text, "and also not"),),
        )


def test_nothing_in_and_nothing_out() -> None:
    assert _associate((), ()) == AssociationResult((), ())


# ---------------------------------------------------------------------------
# One line, two numbers — dual-unit drawings are normal
# ---------------------------------------------------------------------------


def test_two_numbers_may_share_one_line() -> None:
    """`984` and `38 3/4"` are one dimension printed twice, and the second reading is what
    corroborates the first. A line consumed by the first text to claim it would throw that away."""
    line = _horizontal("0.30")
    millimetres = _text("0.22", "0.28", "0.26", "0.29")
    inches = _text("0.22", "0.28", "0.31", "0.34")

    result = _associate((millimetres, inches), (line,))

    assert len(result.associated) == 2
    assert {entry.line for entry in result.associated} == {line}


# ---------------------------------------------------------------------------
# Signals — §2.4 traceability
# ---------------------------------------------------------------------------


def test_every_association_records_the_signals_that_produced_it() -> None:
    """Signals are not decoration. They are what a reviewer reads when an association turns out to
    be wrong, and the type refuses to be built without them."""
    result = _associate((_text("0.22", "0.28", "0.26", "0.29"),), (_horizontal("0.30"),))

    signals = result.associated[0].signals
    assert len(signals) >= 3
    assert all(len(signal.split()) > 3 for signal in signals), "plain English, not a code"


def test_the_signals_say_when_there_was_more_than_one_candidate() -> None:
    """ "It was the only line in range" and "it beat two others" are different facts about how safe
    an association is, and a reviewer triaging findings needs to tell them apart."""
    text = _text("0.22", "0.28", "0.295", "0.305")
    result = _associate((text,), (_horizontal("0.315"), _horizontal("0.28")), margin=Decimal(0))

    assert any("2 lines were candidates" in signal for signal in result.associated[0].signals)


def test_an_association_cannot_be_built_without_signals() -> None:
    with pytest.raises(ValueError, match="signals"):
        TextAssociation(
            text=_text("0.22", "0.28", "0.26", "0.29"),
            line=_horizontal("0.30"),
            signals=(),
        )


# ---------------------------------------------------------------------------
# The two numbers are the caller's, and neither has a default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["proximity_limit", "ambiguity_margin"])
def test_both_thresholds_are_required_keyword_arguments(name: str) -> None:
    """The condition this story was ruled ready under. Both numbers are empirical and
    `data/drawings/` is empty — a default would ship today's guess as ground truth, and a positional
    argument could be passed by accident."""
    parameter = inspect.signature(associate).parameters[name]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


def test_the_proximity_limit_actually_changes_the_answer() -> None:
    text = _text("0.22", "0.28", "0.26", "0.29")
    lines = (_horizontal("0.30"),)

    assert _associate((text,), lines, proximity=Decimal("0.01")).associated == ()
    assert len(_associate((text,), lines, proximity=Decimal("0.05")).associated) == 1


@pytest.mark.parametrize("name", ["proximity_limit", "ambiguity_margin"])
def test_a_float_threshold_is_refused(name: str) -> None:
    """Binary rounding deciding which dimension a number belongs to is exactly the class of failure
    ADR-0001 exists to prevent."""
    with pytest.raises(TypeError, match="Decimal"):
        associate(
            (_text("0.22", "0.28", "0.26", "0.29"),),
            (_horizontal("0.30"),),
            **{"proximity_limit": PROXIMITY, "ambiguity_margin": MARGIN, name: 0.05},  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("name", ["proximity_limit", "ambiguity_margin"])
@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_a_threshold_that_is_not_a_finite_number_is_refused(name: str, value: str) -> None:
    """`Decimal("NaN")` is a `Decimal` and `Decimal("NaN") < 0` is `False`, so it survives every
    other check — and then every comparison against it is `False` too.

    Both directions are silent and both are wrong. A NaN proximity limit puts nothing in range, so
    every number is retained with the reason "no line is near enough" and a reviewer goes looking
    for dimension lines that are there. A NaN ambiguity margin never finds two candidates close
    enough to refuse, so a coin toss comes back as an association — the exact failure this module
    exists to prevent, wearing the costume of a decision.
    """
    with pytest.raises(ValueError, match="finite"):
        associate(
            (_text("0.22", "0.28", "0.26", "0.29"),),
            (_horizontal("0.30"),),
            **{
                "proximity_limit": PROXIMITY,
                "ambiguity_margin": MARGIN,
                name: Decimal(value),
            },
        )


@pytest.mark.parametrize("name", ["proximity_limit", "ambiguity_margin"])
def test_a_negative_threshold_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="negative"):
        associate(
            (_text("0.22", "0.28", "0.26", "0.29"),),
            (_horizontal("0.30"),),
            **{
                "proximity_limit": PROXIMITY,
                "ambiguity_margin": MARGIN,
                name: Decimal("-0.001"),
            },
        )


# ---------------------------------------------------------------------------
# Coordinate planes and malformed input
# ---------------------------------------------------------------------------


def test_text_from_another_page_raises_rather_than_resolving() -> None:
    """Not ambiguity to mark for review — a caller mistake. Stored coordinates are normalised per
    page, so a distance measured between two sheets is arithmetic on unrelated numbers."""
    with pytest.raises(PolygonSpaceMismatchError):
        _associate((_text("0.22", "0.28", "0.26", "0.29", page=PAGE + 1),), (_horizontal("0.30"),))


def test_text_from_another_document_version_raises() -> None:
    with pytest.raises(PolygonSpaceMismatchError):
        _associate(
            (_text("0.22", "0.28", "0.26", "0.29", document=uuid4()),), (_horizontal("0.30"),)
        )


def test_two_texts_sharing_an_observation_id_are_refused() -> None:
    """The result could then not say which of them was attached and which retained, and the
    retained one would be invisible."""
    first = _text("0.22", "0.28", "0.26", "0.29")
    second = DimensionText(
        observation_id=first.observation_id,
        extent=_box("0.22", "0.28", "0.31", "0.34"),
        rotation_degrees=0,
    )

    with pytest.raises(ValueError, match="observation id"):
        _associate((first, second), (_horizontal("0.30"),))


def test_a_list_of_lines_is_refused_rather_than_accepted_loosely() -> None:
    with pytest.raises(TypeError, match="tuple of DimensionExtent"):
        associate(
            (_text("0.22", "0.28", "0.26", "0.29"),),
            [_horizontal("0.30")],  # type: ignore[arg-type]
            proximity_limit=PROXIMITY,
            ambiguity_margin=MARGIN,
        )


# ---------------------------------------------------------------------------
# The text type refuses what it cannot represent
# ---------------------------------------------------------------------------


def test_text_at_an_arbitrary_angle_is_refused_rather_than_rounded_to_an_axis() -> None:
    """Rounding 30° to horizontal would decide which line the number belongs to, quietly. The margin
    separating "near enough to horizontal" from "genuinely diagonal" is empirical and
    `data/drawings/` is empty, so the honest answer today is that this text has no representation
    here — said loudly, at construction."""
    with pytest.raises(ValueError, match="0, 90, 180 or 270"):
        DimensionText(
            observation_id=uuid4(),
            extent=_box("0.22", "0.28", "0.26", "0.29"),
            rotation_degrees=30,
        )


def test_a_leader_endpoint_outside_the_page_is_refused() -> None:
    with pytest.raises(ValueError, match="0..1"):
        DimensionText(
            observation_id=uuid4(),
            extent=_box("0.22", "0.28", "0.26", "0.29"),
            rotation_degrees=0,
            leader_endpoint=StoredPoint(_d("1.20"), _d("0.30")),
        )


def test_a_float_leader_endpoint_is_refused() -> None:
    with pytest.raises(TypeError, match="Decimal"):
        DimensionText(
            observation_id=uuid4(),
            extent=_box("0.22", "0.28", "0.26", "0.29"),
            rotation_degrees=0,
            leader_endpoint=StoredPoint(0.6, 0.3),  # type: ignore[arg-type]
        )
