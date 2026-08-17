"""Immutable persistence models for candidates, canonical facts and evidence artifacts.

Candidate and canonical observations are deliberately separate tables. Exact measurements
are stored as normalized rational pairs, while provenance is represented by two relational
tables so candidate support cannot be confused with a non-candidate corroboration lane.

Source: backend proposal section 10.1, ``AGENTS.md`` sections 2.3 and 2.7,
``DESIGN.md`` section 3.14, and issue #195.
Verification: ``tests/db/test_evidence_models.py``.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from enum import Enum, StrEnum
from math import gcd
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Immutable, TimestampedUUID
from evidence.canonical import Authority, CorroborationLane
from rules.semantic_types import DocumentRole, SemanticType
from units.measurement import Unit
from verdict.operands import EvidenceStatus


class EvidenceCandidateRole(StrEnum):
    """How one extraction candidate relates to a canonical observation."""

    PRIMARY = "primary"
    CORROBORATING = "corroborating"
    CONFLICTING = "conflicting"


class EvidenceArtifactKind(StrEnum):
    """Immutable reviewer-facing artifacts retained by the evidence plane."""

    CROP = "crop"
    RENDER = "render"


def _sql_values(enum_type: type[Enum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum_type)


UNIT_VALUES = _sql_values(Unit)
SEMANTIC_TYPE_VALUES = _sql_values(SemanticType)
DOCUMENT_ROLE_VALUES = _sql_values(DocumentRole)
EVIDENCE_STATUS_VALUES = _sql_values(EvidenceStatus)
AUTHORITY_VALUES = _sql_values(Authority)
CORROBORATION_LANE_VALUES = _sql_values(CorroborationLane)
CANDIDATE_ROLE_VALUES = _sql_values(EvidenceCandidateRole)
ARTIFACT_KIND_VALUES = _sql_values(EvidenceArtifactKind)
SHA256_PATTERN = "^[0-9a-f]{64}$"

EXACT_OPTIONAL_VALUE = """(
    value_numerator IS NULL AND value_denominator IS NULL AND unit IS NULL
) OR (
    value_numerator IS NOT NULL AND value_denominator IS NOT NULL AND unit IS NOT NULL
)"""


class ObservationCandidate(Base, TimestampedUUID, Immutable):
    """Exactly what one extraction run reported, without evidence authority."""

    __tablename__ = "observation_candidates"

    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="RESTRICT"), index=True
    )
    page_id: Mapped[UUID] = mapped_column(ForeignKey("pages.id", ondelete="RESTRICT"))
    extraction_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="RESTRICT"), index=True
    )
    raw_text: Mapped[str]
    value_numerator: Mapped[int | None] = mapped_column(BigInteger, default=None)
    value_denominator: Mapped[int | None] = mapped_column(BigInteger, default=None)
    unit: Mapped[str | None] = mapped_column(String(32), default=None)
    unit_guess: Mapped[str | None] = mapped_column(String(32), default=None)
    semantic_guess: Mapped[str | None] = mapped_column(String(100), default=None)
    polygon: Mapped[list[list[int]]] = mapped_column(JSONB)
    coordinate_space: Mapped[str] = mapped_column(String(32), default="image")
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(), default=None)
    ambiguity_flags: Mapped[list[str]] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint(EXACT_OPTIONAL_VALUE, name="observation_candidate_exact_value"),
        CheckConstraint(
            "value_denominator IS NULL OR value_denominator > 0",
            name="observation_candidate_denominator",
        ),
        CheckConstraint(
            f"unit IS NULL OR unit IN ({UNIT_VALUES})", name="observation_candidate_unit"
        ),
        CheckConstraint(
            f"unit_guess IS NULL OR unit_guess IN ({UNIT_VALUES})",
            name="observation_candidate_unit_guess",
        ),
        CheckConstraint(
            f"semantic_guess IS NULL OR semantic_guess IN ({SEMANTIC_TYPE_VALUES})",
            name="observation_candidate_semantic_guess",
        ),
        CheckConstraint("coordinate_space = 'image'", name="observation_candidate_space"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="observation_candidate_confidence",
        ),
    )


class CanonicalObservation(Base, TimestampedUUID, Immutable):
    """One normalized attributable fact, separate from every extractor candidate."""

    __tablename__ = "canonical_observations"

    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="RESTRICT"), index=True
    )
    page_id: Mapped[UUID] = mapped_column(ForeignKey("pages.id", ondelete="RESTRICT"))
    document_role: Mapped[str] = mapped_column(String(32))
    polygon: Mapped[list[list[str]]] = mapped_column(JSONB)
    coordinate_space: Mapped[str] = mapped_column(String(32), default="stored")
    semantic_type: Mapped[str] = mapped_column(String(100))
    value_numerator: Mapped[int] = mapped_column(BigInteger)
    value_denominator: Mapped[int] = mapped_column(BigInteger)
    unit: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    authority: Mapped[str] = mapped_column(String(32))
    evidence_crop_uri: Mapped[str | None] = mapped_column(String(1000), default=None)

    __table_args__ = (
        CheckConstraint("value_denominator > 0", name="canonical_observation_denominator"),
        CheckConstraint(f"unit IN ({UNIT_VALUES})", name="canonical_observation_unit"),
        CheckConstraint(
            f"document_role IN ({DOCUMENT_ROLE_VALUES})", name="canonical_observation_role"
        ),
        CheckConstraint(
            f"semantic_type IN ({SEMANTIC_TYPE_VALUES})", name="canonical_observation_semantic_type"
        ),
        CheckConstraint(
            f"status IN ({EVIDENCE_STATUS_VALUES})", name="canonical_observation_status"
        ),
        CheckConstraint(
            f"authority IN ({AUTHORITY_VALUES})", name="canonical_observation_authority"
        ),
        CheckConstraint("coordinate_space = 'stored'", name="canonical_observation_space"),
    )


class EvidenceSupportingCandidate(Base, TimestampedUUID, Immutable):
    """One candidate supporting or conflicting with a canonical observation."""

    __tablename__ = "evidence_supporting_candidates"

    canonical_observation_id: Mapped[UUID] = mapped_column(
        ForeignKey("canonical_observations.id", ondelete="RESTRICT"), index=True
    )
    candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("observation_candidates.id", ondelete="RESTRICT"), index=True
    )
    role: Mapped[str] = mapped_column(String(32))

    __table_args__ = (
        CheckConstraint(f"role IN ({CANDIDATE_ROLE_VALUES})", name="evidence_candidate_role"),
        UniqueConstraint("canonical_observation_id", "candidate_id"),
    )


class EvidenceCorroborationLane(Base, TimestampedUUID, Immutable):
    """One typed corroboration route that is not itself an extraction candidate."""

    __tablename__ = "evidence_corroboration_lanes"

    canonical_observation_id: Mapped[UUID] = mapped_column(
        ForeignKey("canonical_observations.id", ondelete="RESTRICT"), index=True
    )
    lane: Mapped[str] = mapped_column(String(32))

    __table_args__ = (
        CheckConstraint(
            f"lane IN ({CORROBORATION_LANE_VALUES})", name="evidence_corroboration_lane"
        ),
        UniqueConstraint("canonical_observation_id", "lane"),
    )


class EvidenceArtifact(Base, TimestampedUUID, Immutable):
    """A content-addressed crop or render reference; artifact bytes live in storage."""

    __tablename__ = "evidence_artifacts"

    candidate_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("observation_candidates.id", ondelete="RESTRICT"), default=None
    )
    canonical_observation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("canonical_observations.id", ondelete="RESTRICT"), default=None
    )
    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="RESTRICT"), index=True
    )
    page_id: Mapped[UUID] = mapped_column(ForeignKey("pages.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(32))
    storage_key: Mapped[str] = mapped_column(String(1000))
    sha256: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(200))
    coordinate_space: Mapped[str] = mapped_column(String(32))

    __table_args__ = (
        CheckConstraint(
            "(candidate_id IS NOT NULL AND canonical_observation_id IS NULL) OR "
            "(candidate_id IS NULL AND canonical_observation_id IS NOT NULL)",
            name="evidence_artifact_owner",
        ),
        CheckConstraint(f"kind IN ({ARTIFACT_KIND_VALUES})", name="evidence_artifact_kind"),
        CheckConstraint("storage_key <> ''", name="evidence_artifact_storage_key"),
        CheckConstraint(f"sha256 ~ '{SHA256_PATTERN}'", name="evidence_artifact_sha256"),
        CheckConstraint("media_type <> ''", name="evidence_artifact_media_type"),
        CheckConstraint("coordinate_space IN ('image', 'stored')", name="evidence_artifact_space"),
        UniqueConstraint("storage_key", "sha256"),
    )

    def content_matches(self, content: bytes) -> bool:
        """Return whether retrieved bytes match the immutable persisted digest."""

        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        return hashlib.sha256(content).hexdigest() == self.sha256


def _require_normalized_rational(numerator: int | None, denominator: int | None) -> None:
    if numerator is None and denominator is None:
        return
    if numerator is None or denominator is None or denominator <= 0:
        return  # Database constraints provide the authoritative completeness check.
    if gcd(numerator, denominator) != 1:
        raise ValueError("exact values must be stored in normalized Fraction form")


@event.listens_for(ObservationCandidate, "before_insert")
def _candidate_fraction_is_normalized(
    mapper: object, connection: Connection, target: ObservationCandidate
) -> None:
    """Reject a second spelling of the same exact candidate value."""

    del mapper, connection
    _require_normalized_rational(target.value_numerator, target.value_denominator)


@event.listens_for(CanonicalObservation, "before_insert")
def _canonical_fraction_is_normalized(
    mapper: object, connection: Connection, target: CanonicalObservation
) -> None:
    """Reject a second spelling of the same exact canonical value."""

    del mapper, connection
    _require_normalized_rational(target.value_numerator, target.value_denominator)
