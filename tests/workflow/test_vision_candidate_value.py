"""A vision reading the shape check accepts must be one the value path can value (#733).

Before #733 they disagreed. The shape check canonicalised inch-mark spellings; the value path was a
bare `normalise_to_inches(raw_text)`. So a model's `8'-6''` passed validation and was stored with no
value, unable to take part in any agreement — and `25-1/2"`, `381 [15]` and `2" (VIF)` could not be
valued at all. Both now read `units.notation`. This file fails if they part again.

Synthetic values of the client's shapes; no client dimension appears here.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from app.evidence.record import NOT_A_SINGLE_VALUE_FLAG, UNPARSED_FLAG, _parse
from extraction.models.validation import (
    CandidateContext,
    CoordinateMode,
    CropSize,
    ObservationCandidate,
    validate_payload,
)
from workflow.stages import _vision_candidate_value

READINGS = [
    '10 1/4"',
    '10-1/4"',
    '10-11/16"',
    "300 [12]",
    '300mm [12"]',
    '2" (VIF)',
    '100 1/4" (4EQ)',
    "6'-0\"",
    "8'-6''",
    "2'-10-1/2\"",
    '10 1/4"+6"',
    "LA-016-CUST",
    "10 3/4",
]


class _Discard:
    def record_rejection(self, rejection: object) -> None:
        pass


def _shape_check_accepts(reading: str) -> bool:
    outcome = validate_payload(
        {"reading": reading, "unit_guess": "in", "x1": 1, "y1": 1, "x2": 50, "y2": 20},
        context=CandidateContext(candidate_id="c", extractor_version="m", page=0),
        crop_size=CropSize(100, 80),
        coordinate_mode=CoordinateMode.PIXELS,
        recorder=_Discard(),
    )
    return isinstance(outcome, ObservationCandidate)


def _states_its_unit(reading: str) -> bool:
    return any(mark in reading for mark in ('"', "'", "\u2033", "\u2032", "[", "mm"))


@pytest.mark.parametrize("reading", [r for r in READINGS if _states_its_unit(r)])
def test_whatever_the_shape_check_accepts_the_value_path_can_value(reading: str) -> None:
    """**Input: a reading that states its unit. Outcome: accepted by the check ⇒ valued by the store.**

    Readings that carry no unit are excluded on purpose — see the next test.
    """
    if not _shape_check_accepts(reading):
        pytest.skip("refused by the shape check, so it never reaches the value path")

    measurement, flag = _vision_candidate_value(reading)

    assert measurement is not None, f"{reading!r} passed the check and was stored with {flag}"


def test_a_reading_with_no_unit_passes_the_shape_check_but_is_never_valued() -> None:
    """**By design, and this pins it.** The shape check asks only whether a token is dimension-shaped
    (`unmarked_unit=Unit.INCH`, result discarded). The value path requires the unit in the text, and
    `evidence/normalize.py` uses `unit_guess` only to cross-check a unit already there — never to supply
    one. So `10 3/4` is dimension-shaped, and never becomes a value: we do not assume inches.

    A change that made this reading valued would be assuming a unit from a model's guess. That is a
    verdict-path decision, not a notation fix.
    """
    assert _shape_check_accepts("10 3/4")
    assert _vision_candidate_value("10 3/4") == (None, UNPARSED_FLAG)


@pytest.mark.parametrize(
    ("reading", "inches"),
    [('10-1/4"', "41/4"), ("300 [12]", "12"), ('2" (VIF)', "2"), ("8'-6''", "102")],
)
def test_the_vision_value_is_the_exact_inch(reading: str, inches: str) -> None:
    measurement, flag = _vision_candidate_value(reading)

    assert flag is None
    assert measurement is not None and measurement.exact == Fraction(inches)


def test_a_compound_is_flagged_as_not_one_value_not_as_unparsed() -> None:
    """Different facts: a compound is a dimension, just not one — and it is not retried (#733)."""
    assert _vision_candidate_value('10 1/4"+6"') == (None, NOT_A_SINGLE_VALUE_FLAG)
    assert _vision_candidate_value("LA-016-CUST") == (None, UNPARSED_FLAG)


@pytest.mark.parametrize("text", ['10-1/4"', '100 1/4" (4EQ)', "300 [12]"])
def test_the_evidence_lane_values_the_same_notation(text: str) -> None:
    """The text read from the PDF itself goes through `units.notation` too, so both lanes agree."""
    measurement, flags, _dual = _parse(text)

    assert measurement is not None, flags


def test_the_evidence_lane_flags_a_compound_as_not_one_value() -> None:
    measurement, flags, _dual = _parse('2"+3"(filler) ')

    assert measurement is None
    assert flags == (NOT_A_SINGLE_VALUE_FLAG,)
