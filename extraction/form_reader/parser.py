"""Parse printed dimension text into exact inches, independently of model components and boxes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from extraction.form_reader.schema import FormDimension, PageFormAnswer
from units.measurement import Measurement
from units.normalise import UnitNormalisationError, normalise_to_inches
from units.notation import canonical_notation, is_compound


@dataclass(frozen=True, slots=True)
class ParsedDimension:
    value: Measurement | None
    reason: str | None


def validate_page_answer(payload: Mapping[str, Any], *, page_index: int) -> PageFormAnswer:
    """Validate a model JSON object and attach the request-owned page index locally."""
    if isinstance(page_index, bool) or page_index < 0:
        raise ValueError("page_index must be a non-negative integer")
    answer = dict(payload)
    if "page_index" in answer:
        raise ValueError("page_index is request metadata and must not come from model output")
    answer["page_index"] = page_index
    return PageFormAnswer.model_validate(answer)


def parse_dimension(dimension: FormDimension) -> ParsedDimension:
    """Use only the literal printed text; reject special cases for human review.

    The model's whole/numerator/denominator fields are cross-checks only. They never construct
    the value. A mismatch makes the proposal unreadable, rather than preferring either source.
    """
    if not dimension.readable or dimension.text is None or not dimension.text.strip():
        return ParsedDimension(None, "unreadable")
    if dimension.combined or is_compound(dimension.text):
        return ParsedDimension(None, "combined")
    if dimension.stacked:
        return ParsedDimension(None, "stacked")
    try:
        notation, _millimetre_half = canonical_notation(dimension.text)
        value = normalise_to_inches(notation)
    except (UnitNormalisationError, TypeError, ValueError, ArithmeticError):
        return ParsedDimension(None, "unreadable")

    exact = value.exact
    expected = _components_match(dimension, exact)
    if not expected:
        return ParsedDimension(None, "reader-components-disagree-with-printed-text")
    return ParsedDimension(Measurement(exact, value.unit, dimension.text), None)


def _components_match(dimension: FormDimension, value: Fraction) -> bool:
    """Check supplied component fields against the exact text parse without using them as input."""
    components = (dimension.whole, dimension.numerator, dimension.denominator)
    if all(part is None for part in components):
        return True
    # The prompt asks for an empty string for a missing part, so `2"` arrives as whole 2 with no
    # fraction parts and `3/4"` as a fraction with no whole. Each is checked on what it states.
    if dimension.numerator is None and dimension.denominator is None:
        return dimension.whole is not None and value.denominator == 1 and dimension.whole == value
    if dimension.denominator is None or dimension.denominator <= 0:
        return False
    whole = 0 if dimension.whole is None else dimension.whole
    numerator = 0 if dimension.numerator is None else dimension.numerator
    rendered = Fraction(whole * dimension.denominator + numerator, dimension.denominator)
    return rendered == value
