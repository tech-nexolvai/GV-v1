"""The normalised, attributable observation boundary.

This module records an evidence status; it does not decide one. Candidate identifiers and
typed corroboration lanes stay separate so a marker can never masquerade as a candidate.

Source: ``docs/DESIGN.md`` section 3.14, ADR-0006, ADR-0016 and issue #118.
Verification: ``tests/evidence/test_canonical.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from evidence.polygon import Polygon
from units.measurement import Measurement
from verdict.operands import QUALIFIED_STATUSES, EvidenceStatus
from vocabulary.semantic_types import DocumentRole, SemanticType

__all__ = [
    "QUALIFIED_STATUSES",
    "Authority",
    "CanonicalObservation",
    "CorroborationLane",
    "EvidenceStatus",
]


class Authority(StrEnum):
    """Whether an observation is eligible to become a verdict operand."""

    AUTHORITATIVE = "AUTHORITATIVE"
    ADVISORY = "ADVISORY"


class CorroborationLane(StrEnum):
    """Typed non-candidate routes that independently corroborated a reading."""

    SECOND_READER = "SECOND_READER"
    DUAL_UNIT = "DUAL_UNIT"
    HUMAN = "HUMAN"


def _validate_candidate_ids(candidate_ids: tuple[str, ...], *, field: str) -> None:
    """Require an explicit tuple of unique, non-empty candidate identifiers."""

    if not isinstance(candidate_ids, tuple):
        raise TypeError(f"{field} must be a tuple of candidate ids")
    if any(
        not isinstance(candidate_id, str) or not candidate_id.strip()
        for candidate_id in candidate_ids
    ):
        raise ValueError(f"{field} must contain only non-empty candidate ids")
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError(f"{field} must not repeat a candidate id")


@dataclass(frozen=True, slots=True)
class CanonicalObservation:
    """One exact fact with enough provenance to explain its evidence status."""

    document_version_id: UUID
    document_role: DocumentRole
    page: int
    polygon: Polygon
    semantic_type: SemanticType
    value: Measurement
    status: EvidenceStatus
    authority: Authority
    supported_by: tuple[str, ...]
    corroborated_by: tuple[CorroborationLane, ...]
    conflicts_with: tuple[str, ...]
    evidence_crop_uri: str | None

    def __post_init__(self) -> None:
        """Reject observations whose identity or provenance contradicts their status."""

        if not isinstance(self.document_version_id, UUID):
            raise TypeError("document_version_id must be a UUID")
        if not isinstance(self.document_role, DocumentRole):
            raise TypeError("document_role must be a DocumentRole")
        if isinstance(self.page, bool) or not isinstance(self.page, int):
            raise TypeError("page must be an integer")
        if self.page < 0:
            raise ValueError("page must be zero or greater")
        if not isinstance(self.polygon, Polygon):
            raise TypeError("polygon must be a Polygon")
        if self.polygon.document_version_id != self.document_version_id:
            raise ValueError("polygon and observation must share a document version")
        if self.polygon.page != self.page:
            raise ValueError("polygon and observation must share a page")
        if not isinstance(self.semantic_type, SemanticType):
            raise TypeError("semantic_type must be a SemanticType")
        if not isinstance(self.value, Measurement):
            raise TypeError("value must be an exact Measurement; float is not allowed")
        if not isinstance(self.status, EvidenceStatus):
            raise TypeError("status must be the verdict EvidenceStatus")
        if not isinstance(self.authority, Authority):
            raise TypeError("authority must be an Authority")

        _validate_candidate_ids(self.supported_by, field="supported_by")
        _validate_candidate_ids(self.conflicts_with, field="conflicts_with")
        if not isinstance(self.corroborated_by, tuple):
            raise TypeError("corroborated_by must be a tuple of CorroborationLane values")
        if any(not isinstance(lane, CorroborationLane) for lane in self.corroborated_by):
            raise TypeError("corroborated_by must contain only CorroborationLane values")
        if len(set(self.corroborated_by)) != len(self.corroborated_by):
            raise ValueError("corroborated_by must not repeat a lane")
        if self.evidence_crop_uri is not None and not isinstance(self.evidence_crop_uri, str):
            raise TypeError("evidence_crop_uri must be a string or None")

        self._validate_status_provenance()

    def _validate_status_provenance(self) -> None:
        support_count = len(self.supported_by)
        if self.status is EvidenceStatus.RAW_CANDIDATE:
            if support_count < 1 or self.conflicts_with:
                raise ValueError("RAW_CANDIDATE requires support and cannot record a conflict")
            return
        if self.status is EvidenceStatus.CORROBORATED:
            two_candidates = support_count >= 2
            dual_unit_lane = (
                support_count >= 1 and CorroborationLane.DUAL_UNIT in self.corroborated_by
            )
            if not (two_candidates or dual_unit_lane):
                raise ValueError(
                    "CORROBORATED requires two candidates or one candidate plus "
                    "dual-unit corroboration"
                )
            if self.conflicts_with:
                raise ValueError("CORROBORATED cannot record a conflict")
            return
        if self.status is EvidenceStatus.CONFLICTING and (
            support_count < 1 or not self.conflicts_with
        ):
            raise ValueError("CONFLICTING requires both supporting and conflicting candidates")
