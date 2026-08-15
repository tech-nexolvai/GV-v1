"""Readable examples for issue #122's exact dimension-chain validator."""

from __future__ import annotations

from fractions import Fraction

import pytest

from extraction.chains import ClosureStatus, DimensionChain, validate_closure
from units.measurement import Measurement, MixedUnitError, Unit


def mm(value: int) -> Measurement:
    """Create an authored whole-millimetre fixture."""

    return Measurement(Fraction(value), Unit.MM, str(value))


def inch(value: Fraction, text: str) -> Measurement:
    """Create an authored imperial fixture without converting it to millimetres."""

    return Measurement(value, Unit.INCH, text)


def test_real_metric_overall_chain_closes_exactly() -> None:
    """Input 2025+1968+2019 equals expected 6012, so the chain is CLOSED."""

    result = validate_closure(DimensionChain((mm(2025), mm(1968), mm(2019)), mm(6012)))

    assert result.status is ClosureStatus.CLOSED
    assert result.expected == mm(6012)
    assert result.actual == Measurement(Fraction(6012), Unit.MM, None)
    assert result.difference == Measurement(Fraction(0), Unit.MM, None)


def test_real_imperial_overall_chain_closes_in_its_own_unit() -> None:
    """Input 79 3/4+77 1/2+79 1/2 equals 236 3/4 inches exactly."""

    result = validate_closure(
        DimensionChain(
            (
                inch(Fraction(319, 4), "79 3/4"),
                inch(Fraction(155, 2), "77 1/2"),
                inch(Fraction(159, 2), "79 1/2"),
            ),
            inch(Fraction(947, 4), "236 3/4"),
        )
    )

    assert result.status is ClosureStatus.CLOSED
    assert result.actual == Measurement(Fraction(947, 4), Unit.INCH, None)


def test_one_unit_misread_reports_expected_actual_and_signed_difference() -> None:
    """Input sums to 2024 against expected 2025, so it fails with difference -1 mm."""

    result = validate_closure(DimensionChain((mm(51), mm(533), mm(456), mm(984)), mm(2025)))

    assert result.status is ClosureStatus.NOT_CLOSED
    assert result.expected == mm(2025)
    assert result.actual == Measurement(Fraction(2024), Unit.MM, None)
    assert result.difference == Measurement(Fraction(-1), Unit.MM, None)


@pytest.mark.parametrize(
    ("chain", "expected_actual"),
    [
        pytest.param(DimensionChain((), mm(2025)), None, id="empty-has-nothing-to-check"),
        pytest.param(
            DimensionChain((mm(2025),), mm(2025)),
            Measurement(Fraction(2025), Unit.MM, "2025"),
            id="one-segment-would-be-a-vacuous-pass",
        ),
        pytest.param(
            DimensionChain((mm(51), mm(533)), None),
            Measurement(Fraction(584), Unit.MM, None),
            id="missing-overall-has-no-expected-total",
        ),
    ],
)
def test_insufficient_chains_are_not_verifiable(
    chain: DimensionChain, expected_actual: Measurement | None
) -> None:
    """Empty, single-segment and missing-overall inputs never produce a false CLOSED."""

    result = validate_closure(chain)

    assert result.status is ClosureStatus.NOT_VERIFIABLE
    assert result.actual == expected_actual
    assert result.difference is None


def test_mixed_authored_units_raise_before_any_sum() -> None:
    """Input 25.4 mm plus 1 inch is rejected even though their converted values match."""

    chain = DimensionChain((mm(25), inch(Fraction(1), "1")), mm(50))

    with pytest.raises(MixedUnitError, match="cannot combine"):
        validate_closure(chain)


def test_nested_real_drawing_chain_validates_each_level() -> None:
    """The 2025 bay closes internally and contributes its authored overall to 6012."""

    bay = DimensionChain((mm(51), mm(533), mm(457), mm(984)), mm(2025))
    drawing = DimensionChain((bay, mm(1968), mm(2019)), mm(6012))

    result = validate_closure(drawing)

    assert result.status is ClosureStatus.CLOSED
    assert result.actual == Measurement(Fraction(6012), Unit.MM, None)
    assert len(result.children) == 1
    assert result.children[0].status is ClosureStatus.CLOSED
    assert result.children[0].actual == Measurement(Fraction(2025), Unit.MM, None)


def test_nested_failure_remains_visible_even_when_parent_authored_totals_close() -> None:
    """A bad child sums to 2024, while its authored 2025 still closes the parent total."""

    bad_bay = DimensionChain((mm(51), mm(533), mm(456), mm(984)), mm(2025))
    drawing = DimensionChain((bad_bay, mm(1968), mm(2019)), mm(6012))

    result = validate_closure(drawing)

    assert result.status is ClosureStatus.CLOSED
    assert result.children[0].status is ClosureStatus.NOT_CLOSED
    assert result.children[0].difference == Measurement(Fraction(-1), Unit.MM, None)


def test_nested_chain_without_overall_makes_parent_not_verifiable() -> None:
    """A child without an authored overall cannot silently become a parent addend."""

    partial_bay = DimensionChain((mm(51), mm(533)), None)
    result = validate_closure(DimensionChain((partial_bay, mm(1968)), mm(2552)))

    assert result.status is ClosureStatus.NOT_VERIFIABLE
    assert result.children[0].status is ClosureStatus.NOT_VERIFIABLE


def test_chain_and_result_are_frozen() -> None:
    """Validated extraction evidence cannot be mutated after construction."""

    chain = DimensionChain((mm(1), mm(2)), mm(3))
    result = validate_closure(chain)

    with pytest.raises(AttributeError):
        chain.overall = mm(4)  # type: ignore[misc]
    with pytest.raises(AttributeError):
        result.status = ClosureStatus.NOT_CLOSED  # type: ignore[misc]
