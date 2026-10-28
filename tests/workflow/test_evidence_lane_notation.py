"""The evidence lane values the same notation the model readers' shape check accepts (#733, #1199).

Before #733 the shape check (`extraction/models/validation.py`) canonicalised inch-mark spellings and
the value paths did not. The vision readers' own value path (`workflow.stages._vision_candidate_value`)
was retired with the label-first route (2026-10-28), with its tests; what is left here is the
evidence lane, which values the text read from the PDF itself through `units.notation` too, and the
shape check's refusal of a fraction no drawing prints.

Synthetic values of the client's shapes; no client dimension appears here.
"""

from __future__ import annotations

import pytest

from app.evidence.record import NOT_A_SINGLE_VALUE_FLAG, _parse
from extraction.models.validation import (
    CandidateContext,
    CoordinateMode,
    CropSize,
    ObservationCandidate,
    validate_payload,
)


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
        stacked_label=False,
        stacked_layouts=(),
    )
    return isinstance(outcome, ObservationCandidate)


@pytest.mark.parametrize("text", ['10-1/4"', '100 1/4" (4EQ)', "300 [12]"])
def test_the_evidence_lane_values_the_same_notation(text: str) -> None:
    """The text read from the PDF itself goes through `units.notation` too, so both lanes agree."""
    measurement, flags, _dual = _parse(text)

    assert measurement is not None, flags


def test_the_evidence_lane_flags_a_compound_as_not_one_value() -> None:
    measurement, flags, _dual = _parse('2"+3"(filler) ')

    assert measurement is None
    assert flags == (NOT_A_SINGLE_VALUE_FLAG,)


@pytest.mark.parametrize("reading", ['16 35/8"', '16 15/8"', '16 3/5"', '163/8"'])
def test_a_fraction_no_drawing_prints_is_refused_and_never_valued(reading: str) -> None:
    """#1199: a whole number with an impossible fraction is refused by the shape check, with the
    reason."""
    from extraction.models.validation import _reading_refusal

    refusal = _reading_refusal(reading)
    assert refusal is not None and "fraction" in refusal
    assert not _shape_check_accepts(reading)
