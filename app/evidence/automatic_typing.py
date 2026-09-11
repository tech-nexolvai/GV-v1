"""Persist the narrow automatic semantic-typing lane.

The lane is deliberately smaller than a classifier: it promotes only an exact *vector* tag that
the existing association stage has already attached to the same dimension line as a numeric reading.
Every other candidate remains untyped and is reviewer work.  In particular, no value is written to
``ObservationCandidate.semantic_guess``.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.events import SYSTEM_ACTOR, AuditCategory, emit
from app.evidence.confirm import _document_role, _transform
from app.models.document import PackageRevisionDocument, Page
from app.models.evidence import (
    CanonicalObservation,
    EvidenceArtifact,
    EvidenceArtifactKind,
    EvidenceCorroborationLane,
    EvidenceSupportingCandidate,
    ObservationAssociation,
    ObservationCandidate,
)
from app.models.runs import ExtractionRun
from evidence.candidate import ObservationCandidate as DomainCandidate
from evidence.canonical import Authority, CorroborationLane, EvidenceStatus
from evidence.coordinates import ImagePoint
from evidence.normalize import NormalizationRefusal, normalize
from evidence.semantic_typing import (
    SemanticTypingDecision,
    TypingDisposition,
    from_exact_tag,
)
from units.measurement import Measurement, Unit
from vocabulary.semantic_types import SemanticType

__all__ = [
    "AutomaticTypingSettings",
    "TypingBatch",
    "qualify_exact_tag_pair",
    "qualify_exact_tags_for_revision",
]


@dataclass(frozen=True, slots=True)
class AutomaticTypingSettings:
    """The vocabulary explicitly approved for one deployment/layout.

    There is no default set.  Q20 makes the three-sided tags final but leaves other layouts
    provisional, so a worker must be configured with exactly the tags it is allowed to honour.
    """

    permitted_types: frozenset[SemanticType]
    vector_extractors: frozenset[str] = frozenset({"pdfplumber"})

    def __post_init__(self) -> None:
        if not isinstance(self.permitted_types, frozenset) or not self.permitted_types:
            raise ValueError("automatic typing needs a non-empty explicit set of semantic types")
        if any(not isinstance(item, SemanticType) for item in self.permitted_types):
            raise TypeError("permitted_types must contain only SemanticType values")
        if not isinstance(self.vector_extractors, frozenset) or not self.vector_extractors:
            raise ValueError("automatic typing needs an explicit vector extractor allowlist")
        if any(not isinstance(item, str) or not item.strip() for item in self.vector_extractors):
            raise TypeError("vector_extractors must contain only non-empty strings")


@dataclass(frozen=True, slots=True)
class TypingBatch:
    """Automatic qualifications and explicit reviewer-needed decisions for one revision."""

    qualified: tuple[CanonicalObservation, ...]
    review_required: tuple[SemanticTypingDecision, ...]


def _line_keys(session: Session, candidate_id: UUID) -> frozenset[tuple[str, str, str, str]]:
    """Every attached dimension line for a candidate, across immutable association runs."""

    return _line_keys_for_candidates(session, (candidate_id,)).get(candidate_id, frozenset())


def _line_keys_for_candidates(
    session: Session, candidate_ids: tuple[UUID, ...]
) -> dict[UUID, frozenset[tuple[str, str, str, str]]]:
    """Load association evidence once for a revision rather than once per possible tag pair."""

    if not candidate_ids:
        return {}
    rows = session.scalars(
        select(ObservationAssociation).where(ObservationAssociation.candidate_id.in_(candidate_ids))
    ).all()
    attached_by_candidate: dict[UUID, set[tuple[str, str, str, str]]] = {
        candidate_id: set() for candidate_id in candidate_ids
    }
    for row in rows:
        if None in (row.start_x, row.start_y, row.end_x, row.end_y):
            continue
        assert row.start_x is not None
        assert row.start_y is not None
        assert row.end_x is not None
        assert row.end_y is not None
        attached_by_candidate.setdefault(row.candidate_id, set()).add(
            (row.start_x, row.start_y, row.end_x, row.end_y)
        )
    return {candidate_id: frozenset(lines) for candidate_id, lines in attached_by_candidate.items()}


def _already_qualified(session: Session, candidate_id: UUID) -> bool:
    return (
        session.execute(
            select(CanonicalObservation.id)
            .join(
                EvidenceSupportingCandidate,
                EvidenceSupportingCandidate.canonical_observation_id == CanonicalObservation.id,
            )
            .where(EvidenceSupportingCandidate.candidate_id == candidate_id)
        ).first()
        is not None
    )


def _has_crop(session: Session, candidate_id: UUID) -> bool:
    """Only promote inspectable reading/tag evidence into a verdict operand."""
    return (
        session.execute(
            select(EvidenceArtifact.id).where(
                EvidenceArtifact.candidate_id == candidate_id,
                EvidenceArtifact.kind == EvidenceArtifactKind.CROP.value,
            )
        ).first()
        is not None
    )


def _review(
    *, candidate_id: UUID, reason: str, semantic_type: SemanticType | None = None
) -> SemanticTypingDecision:
    """Make the typed abstention visible without minting canonical evidence."""

    from evidence.semantic_typing import TypingMethod

    return SemanticTypingDecision(
        candidate_id=candidate_id,
        semantic_type=semantic_type,
        method=TypingMethod.MECHANICAL_TAG,
        disposition=TypingDisposition.REVIEW_REQUIRED,
        reason=reason,
    )


def qualify_exact_tag_pair(
    session: Session,
    *,
    candidate_id: UUID,
    tag_candidate_id: UUID,
    settings: AutomaticTypingSettings,
) -> CanonicalObservation | SemanticTypingDecision:
    """Promote one reading only when an exact vector tag proves its meaning.

    The tag candidate is persisted alongside the numeric primary candidate and the exact association
    line is checked before promotion.  A tag text alone, a near tag, and an agent label all fail this
    boundary deliberately.
    """

    reading = session.get(ObservationCandidate, candidate_id)
    tag = session.get(ObservationCandidate, tag_candidate_id)
    if reading is None or tag is None:
        return _review(
            candidate_id=candidate_id, reason="the reading or its tag candidate is absent"
        )
    if reading.document_version_id != tag.document_version_id or reading.page_id != tag.page_id:
        return _review(
            candidate_id=candidate_id,
            reason="the reading and tag are not on the same document page",
        )
    if _already_qualified(session, candidate_id):
        return _review(
            candidate_id=candidate_id,
            reason="the reading already has canonical evidence; it is not promoted twice",
        )
    if not _has_crop(session, candidate_id) or not _has_crop(session, tag_candidate_id):
        return _review(
            candidate_id=candidate_id,
            reason="the reading or its exact tag has no stored evidence crop",
        )
    tag_run = session.get(ExtractionRun, tag.extraction_run_id)
    decision = from_exact_tag(
        candidate_id=candidate_id,
        tag_candidate_id=tag_candidate_id,
        tag_text=tag.raw_text,
        permitted_types=settings.permitted_types,
        shared_dimension_line=bool(
            _line_keys(session, candidate_id) & _line_keys(session, tag_candidate_id)
        ),
        tag_is_vector_text=tag_run is not None and tag_run.extractor in settings.vector_extractors,
    )
    if decision.disposition is not TypingDisposition.QUALIFIED:
        return decision

    page = session.get(Page, reading.page_id)
    run = session.get(ExtractionRun, reading.extraction_run_id)
    if page is None or run is None:
        return _review(
            candidate_id=candidate_id, reason="the reading page or extraction run is absent"
        )
    transform = _transform(page, run)
    role = _document_role(session, reading.document_version_id)
    if transform is None or role is None:
        return _review(
            candidate_id=candidate_id,
            reason="the reading cannot be normalised into a document-backed evidence location",
        )
    if (
        reading.value_numerator is None
        or reading.value_denominator is None
        or reading.unit is None
        or decision.semantic_type is None
    ):
        return _review(candidate_id=candidate_id, reason="the reading has no exact usable value")

    measurement = Measurement(
        exact=Fraction(reading.value_numerator, reading.value_denominator),
        unit=Unit(reading.unit),
        raw_text=reading.raw_text,
    )
    normalised = normalize(
        DomainCandidate(
            candidate_id=str(reading.id),
            extractor=run.extractor,
            extractor_version=run.extractor_version,
            raw_text=reading.raw_text,
            parsed_value=measurement,
            unit_guess=Unit(reading.unit),
            semantic_guess=decision.semantic_type,
            page=page.index,
            polygon=tuple(ImagePoint(x=int(x), y=int(y)) for x, y in reading.polygon),
            confidence=reading.confidence,
            ambiguity_flags=tuple(reading.ambiguity_flags),
        ),
        transform=transform,
        document_version_id=reading.document_version_id,
        document_role=role,
    )
    if isinstance(normalised, NormalizationRefusal):
        return _review(candidate_id=candidate_id, reason=normalised.detail)

    observation = CanonicalObservation(
        document_version_id=reading.document_version_id,
        page_id=page.id,
        document_role=role.value,
        polygon=[[str(point.x), str(point.y)] for point in normalised.polygon.points],
        coordinate_space="stored",
        semantic_type=decision.semantic_type.value,
        value_numerator=measurement.exact.numerator,
        value_denominator=measurement.exact.denominator,
        unit=measurement.unit.value,
        status=EvidenceStatus.CORROBORATED.value,
        authority=Authority.AUTHORITATIVE.value,
        evidence_crop_uri=None,
    )
    session.add(observation)
    session.flush()
    session.add_all(
        (
            EvidenceSupportingCandidate(
                canonical_observation_id=observation.id,
                candidate_id=reading.id,
                role="primary",
            ),
            EvidenceSupportingCandidate(
                canonical_observation_id=observation.id,
                candidate_id=tag.id,
                role="corroborating",
            ),
            EvidenceCorroborationLane(
                canonical_observation_id=observation.id,
                lane=CorroborationLane.MECHANICAL_TAG.value,
            ),
        )
    )
    emit(
        session,
        category=AuditCategory.EVIDENCE_QUALIFICATION,
        actor=SYSTEM_ACTOR,
        target_id=observation.id,
        target_type="canonical_observation",
    )
    session.flush()
    return observation


def _candidate_rows(
    session: Session, package_revision_id: UUID
) -> tuple[ObservationCandidate, ...]:
    rows = session.scalars(
        select(ObservationCandidate)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == ObservationCandidate.document_version_id,
        )
        .where(PackageRevisionDocument.package_revision_id == package_revision_id)
        .order_by(ObservationCandidate.created_at, ObservationCandidate.id)
    ).all()
    return tuple(rows)


def qualify_exact_tags_for_revision(
    session: Session,
    *,
    package_revision_id: UUID,
    settings: AutomaticTypingSettings,
) -> TypingBatch:
    """Find exact tag/read pairs for one immutable revision and qualify only unambiguous ones."""

    rows = _candidate_rows(session, package_revision_id)
    lines_by_candidate = _line_keys_for_candidates(session, tuple(row.id for row in rows))
    tags_by_page: dict[tuple[UUID, UUID], list[ObservationCandidate]] = {}
    for row in rows:
        try:
            semantic_type = SemanticType(row.raw_text.strip())
        except ValueError:
            continue
        if semantic_type in settings.permitted_types:
            tags_by_page.setdefault((row.document_version_id, row.page_id), []).append(row)

    qualified: list[CanonicalObservation] = []
    review_required: list[SemanticTypingDecision] = []
    for reading in rows:
        if (
            reading.value_numerator is None
            or reading.value_denominator is None
            or reading.unit is None
            or _already_qualified(session, reading.id)
        ):
            continue
        candidates = [
            tag
            for tag in tags_by_page.get((reading.document_version_id, reading.page_id), [])
            if tag.id != reading.id
            and lines_by_candidate.get(reading.id, frozenset())
            & lines_by_candidate.get(tag.id, frozenset())
        ]
        if not candidates:
            continue
        types = {SemanticType(tag.raw_text.strip()) for tag in candidates}
        if len(types) > 1:
            review_required.append(
                _review(
                    candidate_id=reading.id,
                    reason="multiple exact tags name different semantic types for this dimension line",
                )
            )
            continue
        # Duplicate occurrences of one tag are not competing meanings.  Try the vector candidates
        # in stable order and stop at the first qualified binding; an OCR duplicate remains only a
        # review hint and cannot prevent a directly extracted exact tag from doing its job.
        outcomes = (
            qualify_exact_tag_pair(
                session, candidate_id=reading.id, tag_candidate_id=tag.id, settings=settings
            )
            for tag in candidates
        )
        for outcome in outcomes:
            if isinstance(outcome, CanonicalObservation):
                qualified.append(outcome)
                break
            review_required.append(outcome)
    return TypingBatch(tuple(qualified), tuple(review_required))
