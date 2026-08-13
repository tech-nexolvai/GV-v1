"""Verification for issue #39: exact, auditable measurement values."""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from units.measurement import INCH_TO_MM, Measurement, MixedUnitError, Unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_inch_conversion_uses_the_exact_decimal_factor() -> None:
    measurement = Measurement(Fraction(1), Unit.INCH, '1"')

    assert INCH_TO_MM == Decimal("25.4")
    assert measurement.mm == Decimal("25.4")


def test_third_of_an_inch_round_trips_without_loss() -> None:
    original = Measurement(Fraction(1, 3), Unit.INCH, "1/3")

    in_mm = original.to(Unit.MM)
    round_tripped = in_mm.to(Unit.INCH)

    assert in_mm.exact == Fraction(127, 15)
    assert round_tripped == original
    assert round_tripped.raw_text == "1/3"


def test_arithmetic_is_exact_and_does_not_retain_a_synthetic_source_token() -> None:
    left = Measurement(Fraction(38, 4), Unit.INCH, "9 1/2")
    right = Measurement(Fraction(1, 4), Unit.INCH, "1/4")

    total = left + right
    difference = left - right

    assert total.exact == Fraction(39, 4)
    assert difference.exact == Fraction(37, 4)
    assert total.raw_text is None
    assert difference.raw_text is None


def test_original_token_is_preserved_when_converted() -> None:
    source = Measurement(Fraction(984), Unit.MM, "984")

    converted = source.to(Unit.INCH)

    assert converted.raw_text == "984"
    assert converted.exact == Fraction(4920, 127)


def test_measurement_is_immutable_hashable_and_ordered() -> None:
    one = Measurement(Fraction(1), Unit.MM, "1")
    two = Measurement(Fraction(2), Unit.MM, "2")

    assert hash(one)
    assert sorted([two, one]) == [one, two]
    with pytest.raises(FrozenInstanceError):
        one.exact = Fraction(3)  # type: ignore[misc]


def test_mixed_unit_arithmetic_fails_instead_of_silently_converting() -> None:
    millimetres = Measurement(Fraction(25), Unit.MM, "25")
    inches = Measurement(Fraction(1), Unit.INCH, '1"')

    with pytest.raises(MixedUnitError):
        _ = millimetres + inches


@pytest.mark.parametrize(
    ("exact", "unit", "raw_text"),
    [
        (1, Unit.MM, "1"),
        (Fraction(1), "mm", "1"),
        (Fraction(1), Unit.MM, 1),
    ],
)
def test_invalid_boundary_values_are_rejected(
    exact: object, unit: object, raw_text: object
) -> None:
    with pytest.raises(TypeError):
        Measurement(exact, unit, raw_text)  # type: ignore[arg-type]


def test_verdict_and_rules_contain_no_float_values_or_references() -> None:
    """Keep binary floating point out of the decision path before it reaches a verdict."""

    offenders: list[str] = []
    for package in ("verdict", "rules"):
        for path in (REPO_ROOT / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, float):
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno} uses a float literal"
                    )
                elif (
                    isinstance(node, ast.Name)
                    and node.id == "float"
                    or isinstance(node, ast.Attribute)
                    and node.attr == "float"
                ):
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno} references float"
                    )

    assert not offenders, "Float use in the decision path:\n  " + "\n  ".join(offenders)
