"""Pure conversion from an extractor candidate to canonical evidence.

Normalisation preserves authored measurements and refuses incomplete or contradictory
candidate data. It never corroborates a reading and never guesses a missing field.

Source: ``docs/DESIGN.md`` section 3.14, ADR-0001 and issue #119.
Verification: ``tests/evidence/test_normalize.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from evidence.candidate import ObservationCandidate
from evidence.canonical import Authority, CanonicalObservation, EvidenceStatus
from evidence.coordinates import PageTransform
from evidence.polygon import Polygon
from vocabulary.semantic_types import DocumentRole


class NormalizationReason(StrEnum):
    """Why a candidate could not become a canonical observation."""

    MISSING_VALUE = "MISSING_VALUE"
    UNKNOWN_UNIT = "UNKNOWN_UNIT"
    AMBIGUOUS_UNIT = "AMBIGUOUS_UNIT"
    MISSING_SEMANTIC_TYPE = "MISSING_SEMANTIC_TYPE"
    INCONSISTENT_TOKEN = "INCONSISTENT_TOKEN"
    INVALID_POLYGON = "INVALID_POLYGON"


@dataclass(frozen=True, slots=True)
class NormalizationRefusal:
    """A deterministic refusal that tells a reviewer what must be corrected."""

    reason: NormalizationReason
    detail: str


def normalize(
    candidate: ObservationCandidate,
    *,
    transform: PageTransform,
    document_version_id: UUID,
    document_role: DocumentRole,
) -> CanonicalObservation | NormalizationRefusal:
    """Normalise one candidate without converting, promoting, guessing, or performing I/O."""

    if candidate.parsed_value is None:
        return NormalizationRefusal(
            NormalizationReason.MISSING_VALUE,
            "candidate has no parsed measurement; there is no value to normalise",
        )
    if candidate.unit_guess is None:
        return NormalizationRefusal(
            NormalizationReason.UNKNOWN_UNIT,
            "candidate has no unit; normalisation will not assume millimetres",
        )
    if candidate.unit_guess is not candidate.parsed_value.unit:
        return NormalizationRefusal(
            NormalizationReason.AMBIGUOUS_UNIT,
            "candidate unit disagrees with the parsed measurement unit",
        )
    if candidate.semantic_guess is None:
        return NormalizationRefusal(
            NormalizationReason.MISSING_SEMANTIC_TYPE,
            "candidate has no semantic type; normalisation will not infer one from position",
        )
    if candidate.parsed_value.raw_text != candidate.raw_text:
        return NormalizationRefusal(
            NormalizationReason.INCONSISTENT_TOKEN,
            "parsed measurement token differs from the extractor's authored text",
        )

    try:
        stored_points = tuple(transform.to_stored(point) for point in candidate.polygon)
        polygon = Polygon(
            points=stored_points,
            space="stored",
            document_version_id=document_version_id,
            page=candidate.page,
        )
    except (TypeError, ValueError) as error:
        return NormalizationRefusal(
            NormalizationReason.INVALID_POLYGON,
            f"candidate polygon cannot become valid stored geometry: {error}",
        )

    return CanonicalObservation(
        document_version_id=document_version_id,
        document_role=document_role,
        page=candidate.page,
        polygon=polygon,
        semantic_type=candidate.semantic_guess,
        value=candidate.parsed_value,
        status=EvidenceStatus.RAW_CANDIDATE,
        authority=Authority.AUTHORITATIVE,
        supported_by=(candidate.candidate_id,),
        corroborated_by=(),
        conflicts_with=(),
        evidence_crop_uri=None,
    )
