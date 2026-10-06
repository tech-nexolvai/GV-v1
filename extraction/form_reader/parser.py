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
    if dimension.whole is None or dimension.denominator is None:
        return False
    numerator = 0 if dimension.numerator is None else dimension.numerator
    if dimension.denominator <= 0:
        return False
    rendered = Fraction(dimension.whole * dimension.denominator + numerator, dimension.denominator)
    return rendered == value
