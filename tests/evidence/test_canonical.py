"""Verification for issue #118: normalised observations retain exact provenance."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from fractions import Fraction
from uuid import UUID

import pytest

import verdict.operands
from evidence.canonical import Authority, CanonicalObservation, CorroborationLane, EvidenceStatus
from evidence.coordinates import StoredPoint
from evidence.polygon import Polygon
from rules.semantic_types import (
    DOCUMENT_BACKED_SOURCES,
    DocumentRole,
    OperandSource,
    SemanticType,
)
from units.measurement import Measurement, Unit

DOCUMENT_ID = UUID("12345678-1234-5678-1234-567812345678")


def _polygon(*, document_version_id: UUID = DOCUMENT_ID, page: int = 2) -> Polygon:
    return Polygon(
        points=(
            StoredPoint(Decimal("0.1"), Decimal("0.1")),
            StoredPoint(Decimal("0.4"), Decimal("0.1")),
            StoredPoint(Decimal("0.4"), Decimal("0.4")),
        ),
        space="stored",
        document_version_id=document_version_id,
        page=page,
    )


def _observation(**changes: object) -> CanonicalObservation:
    values: dict[str, object] = {
        "document_version_id": DOCUMENT_ID,
        "document_role": DocumentRole.SHOP,
        "page": 2,
        "polygon": _polygon(),
        "semantic_type": SemanticType.CABINET_WIDTH,
        "value": Measurement(Fraction(984), Unit.MM, "984"),
        "status": EvidenceStatus.RAW_CANDIDATE,
        "authority": Authority.AUTHORITATIVE,
        "supported_by": ("candidate-vector",),
        "corroborated_by": (),
        "conflicts_with": (),
        "evidence_crop_uri": "s3://evidence/crop.png",
    }
    values.update(changes)
    return CanonicalObservation(**values)  # type: ignore[arg-type]


def test_canonical_observation_is_frozen_and_preserves_the_authored_measurement() -> None:
    observation = _observation()

    assert observation.value.exact == Fraction(984)
    assert observation.value.unit is Unit.MM
    assert observation.value.raw_text == "984"
    with pytest.raises(FrozenInstanceError):
        observation.page = 3  # type: ignore[misc]


def test_evidence_status_is_the_verdict_contract_not_a_second_enum() -> None:
    assert EvidenceStatus is verdict.operands.EvidenceStatus
    assert len(EvidenceStatus) == 5


def test_document_roles_are_exactly_the_document_backed_operand_sources() -> None:
    role_values = {role.value for role in DocumentRole}
    source_values = {source.value for source in DOCUMENT_BACKED_SOURCES}

    assert role_values == source_values
    assert DocumentRole.PRODUCT_SPEC.value == OperandSource.PRODUCT_SPEC.value


@pytest.mark.parametrize(
    ("supported_by", "corroborated_by"),
    [
        (("candidate-vector", "candidate-ocr"), ()),
        (("candidate-vector",), (CorroborationLane.DUAL_UNIT,)),
    ],
)
def test_corroborated_accepts_two_candidates_or_one_plus_the_dual_unit_lane(
    supported_by: tuple[str, ...],
    corroborated_by: tuple[CorroborationLane, ...],
) -> None:
    observation = _observation(
        status=EvidenceStatus.CORROBORATED,
        supported_by=supported_by,
        corroborated_by=corroborated_by,
    )

    assert observation.status is EvidenceStatus.CORROBORATED


def test_one_candidate_alone_cannot_claim_corroboration() -> None:
    with pytest.raises(ValueError, match="two candidates or one candidate"):
        _observation(status=EvidenceStatus.CORROBORATED)


def test_repeating_one_candidate_cannot_manufacture_two_sources() -> None:
    with pytest.raises(ValueError, match="must not repeat"):
        _observation(
            status=EvidenceStatus.CORROBORATED,
            supported_by=("candidate-vector", "candidate-vector"),
        )


def test_raw_candidate_requires_support_and_forbids_conflicts() -> None:
    with pytest.raises(ValueError, match="requires support"):
        _observation(supported_by=())
    with pytest.raises(ValueError, match="cannot record a conflict"):
        _observation(conflicts_with=("candidate-ocr",))


def test_human_confirmed_may_have_no_candidate_support() -> None:
    observation = _observation(
        status=EvidenceStatus.HUMAN_CONFIRMED,
        supported_by=(),
    )

    assert observation.supported_by == ()


def test_conflicting_requires_both_supporting_and_conflicting_candidates() -> None:
    accepted = _observation(
        status=EvidenceStatus.CONFLICTING,
        conflicts_with=("candidate-ocr",),
    )
    assert accepted.status is EvidenceStatus.CONFLICTING

    with pytest.raises(ValueError, match="supporting and conflicting"):
        _observation(status=EvidenceStatus.CONFLICTING, conflicts_with=())
    with pytest.raises(ValueError, match="supporting and conflicting"):
        _observation(
            status=EvidenceStatus.CONFLICTING,
            supported_by=(),
            conflicts_with=("candidate-ocr",),
        )


def test_rejected_accepts_recorded_support_and_conflicts_without_qualifying_them() -> None:
    observation = _observation(
        status=EvidenceStatus.REJECTED,
        supported_by=(),
        conflicts_with=("candidate-invalid",),
    )

    assert observation.status is EvidenceStatus.REJECTED


@pytest.mark.parametrize("mismatch", ["document", "page"])
def test_polygon_identity_must_match_the_observation(mismatch: str) -> None:
    if mismatch == "document":
        other_document = UUID("87654321-4321-8765-4321-876543218765")
        with pytest.raises(ValueError, match="share a document version"):
            _observation(polygon=_polygon(document_version_id=other_document))
    else:
        with pytest.raises(ValueError, match="share a page"):
            _observation(polygon=_polygon(page=3))


def test_float_value_is_rejected_instead_of_becoming_an_exact_fact() -> None:
    with pytest.raises(TypeError, match="exact Measurement"):
        _observation(value=984.0)


def test_authority_and_document_role_are_first_class_fields() -> None:
    observation = _observation(
        document_role=DocumentRole.PRODUCT_SPEC,
        authority=Authority.ADVISORY,
    )

    assert observation.document_role is DocumentRole.PRODUCT_SPEC
    assert observation.authority is Authority.ADVISORY


def test_provenance_collections_are_required_tuples() -> None:
    observation = _observation()

    with pytest.raises(TypeError, match="supported_by must be a tuple"):
        replace(observation, supported_by=["candidate-vector"])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="corroborated_by must be a tuple"):
        replace(observation, corroborated_by=[CorroborationLane.DUAL_UNIT])  # type: ignore[arg-type]
