"""Raw, uncertain observations produced by extraction routes.

An observation candidate records what a reader reported without claiming that the
reading is correct. Qualification and evidence status belong to later pipeline stages.

Source: ``docs/DESIGN.md`` section 3.14 and backend proposal section 7.1.
Verification: ``tests/evidence/test_candidate.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from evidence.coordinates import ImagePoint
from rules.semantic_types import SemanticType
from units.measurement import Measurement, Unit


@dataclass(frozen=True, slots=True)
class ObservationCandidate:
    """What one extractor said, preserving its uncertainty and original frame.

    ``confidence`` is diagnostic metadata, never authority. Even a high-confidence
    reading remains a candidate until an independent route or a human corroborates it.
    The image-space polygon is retained exactly as reported so coordinate normalisation
    remains independently auditable.
    """

    candidate_id: str
    extractor: str
    extractor_version: str
    raw_text: str
    parsed_value: Measurement | None
    unit_guess: Unit | None
    semantic_guess: SemanticType | None
    page: int
    polygon: tuple[ImagePoint, ...]
    confidence: Decimal | None
    ambiguity_flags: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject values that would blur the raw candidate boundary."""

        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip():
            raise ValueError("candidate_id must be a non-empty string")
        if not isinstance(self.extractor, str):
            raise TypeError("extractor must be a string")
        if not isinstance(self.extractor_version, str):
            raise TypeError("extractor_version must be a string")
        if not isinstance(self.raw_text, str):
            raise TypeError("raw_text must be a string")
        if self.parsed_value is not None and not isinstance(self.parsed_value, Measurement):
            raise TypeError("parsed_value must be a Measurement or None; float is not allowed")
        if self.unit_guess is not None and not isinstance(self.unit_guess, Unit):
            raise TypeError("unit_guess must be a Unit or None")
        if self.semantic_guess is not None and not isinstance(self.semantic_guess, SemanticType):
            raise TypeError("semantic_guess must be a SemanticType or None")
        if isinstance(self.page, bool) or not isinstance(self.page, int):
            raise TypeError("page must be an integer")
        if self.page < 0:
            raise ValueError("page must be zero or greater")

        if not isinstance(self.polygon, tuple):
            raise TypeError("polygon must be a tuple of ImagePoint values")
        for point in self.polygon:
            if not isinstance(point, ImagePoint):
                raise TypeError("polygon must contain only ImagePoint values")
            if (
                isinstance(point.x, bool)
                or not isinstance(point.x, int)
                or isinstance(point.y, bool)
                or not isinstance(point.y, int)
            ):
                raise TypeError("ImagePoint coordinates must be integer values")

        if self.confidence is not None and not isinstance(self.confidence, Decimal):
            raise TypeError("confidence must be a Decimal or None; float is not allowed")
        if not isinstance(self.ambiguity_flags, tuple):
            raise TypeError("ambiguity_flags must be a tuple of strings")
        if any(not isinstance(flag, str) for flag in self.ambiguity_flags):
            raise TypeError("ambiguity_flags must contain only strings")
