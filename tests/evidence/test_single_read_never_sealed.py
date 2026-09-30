"""Verification for #623: one automatic read is never enough for the gate."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from uuid import UUID

from evidence.canonical import Authority, CanonicalObservation, CorroborationLane
from evidence.coordinates import StoredPoint
from evidence.gate import EvidenceStatus, GateRefusal, RefusalReason, VerdictOperand, seal
from evidence.polygon import Polygon
from rules.semantic_types import DocumentRole, SemanticType
from units.measurement import Measurement, Unit

DOCUMENT_ID = UUID("12345678-1234-5678-1234-567812345678")


def _observation(
    *,
    status: EvidenceStatus,
    supported_by: tuple[str, ...],
    corroborated_by: tuple[CorroborationLane, ...] = (),
    conflicts_with: tuple[str, ...] = (),
) -> CanonicalObservation:
    polygon = Polygon(
        points=(
            StoredPoint(Decimal("0.1"), Decimal("0.2")),
            StoredPoint(Decimal("0.4"), Decimal("0.2")),
            StoredPoint(Decimal("0.4"), Decimal("0.5")),
        ),
        space="stored",
        document_version_id=DOCUMENT_ID,
        page=3,
    )
    return CanonicalObservation(
        document_version_id=DOCUMENT_ID,
        document_role=DocumentRole.SHOP,
        page=3,
        polygon=polygon,
        semantic_type=SemanticType.CABINET_WIDTH,
        value=Measurement(Fraction(155, 4), Unit.INCH, "38 3/4"),
        status=status,
        authority=Authority.AUTHORITATIVE,
        supported_by=supported_by,
        corroborated_by=corroborated_by,
        conflicts_with=conflicts_with,
        evidence_crop_uri="s3://evidence/crop.png",
    )


def test_single_route_read_refuses_and_stays_reviewer_work() -> None:
    """Input: one extractor read. Outcome: NOT_QUALIFIED. Why: a lone read cannot pass."""

    result = seal(
        _observation(status=EvidenceStatus.RAW_CANDIDATE, supported_by=("vector",)),
        "cabinet_width",
    )

    assert result == GateRefusal(
        RefusalReason.NOT_QUALIFIED,
        "evidence status RAW_CANDIDATE is not qualified for a verdict",
    )


def test_single_route_dual_unit_read_still_refuses_before_the_gate() -> None:
    """Input: one route plus in-token unit agreement. Outcome: refusal, not a verdict."""

    result = seal(
        _observation(
            status=EvidenceStatus.CORROBORATED,
            supported_by=("vector",),
            corroborated_by=(CorroborationLane.DUAL_UNIT,),
        ),
        "cabinet_width",
    )

    assert result == GateRefusal(
        RefusalReason.NOT_QUALIFIED,
        "a single-route reading needs second-reader agreement or reviewer confirmation",
    )


def test_two_agreeing_readers_with_a_semantic_type_seal() -> None:
    """Input: two independent reads and a typed quantity. Outcome: sealed operand."""

    result = seal(
        _observation(
            status=EvidenceStatus.CORROBORATED,
            supported_by=("vector", "ocr"),
            corroborated_by=(CorroborationLane.SECOND_READER,),
        ),
        "cabinet_width",
    )

    assert isinstance(result, VerdictOperand)
    assert result.name == "cabinet_width"
    assert result.status is EvidenceStatus.CORROBORATED


def test_conflicting_readers_never_seal() -> None:
    """Input: numeric disagreement. Outcome: NOT_QUALIFIED. Why: no confidence tiebreak."""

    result = seal(
        _observation(
            status=EvidenceStatus.CONFLICTING,
            supported_by=("vector",),
            conflicts_with=("ocr",),
        ),
        "cabinet_width",
    )

    assert result == GateRefusal(
        RefusalReason.NOT_QUALIFIED,
        "evidence status CONFLICTING is not qualified for a verdict",
    )


def test_reviewer_confirmation_still_seals_one_read() -> None:
    """Input: a human-confirmed reading. Outcome: sealed operand. Why: a reviewer signed off."""

    result = seal(
        _observation(
            status=EvidenceStatus.HUMAN_CONFIRMED,
            supported_by=("vector",),
            corroborated_by=(CorroborationLane.HUMAN,),
        ),
        "cabinet_width",
    )

    assert isinstance(result, VerdictOperand)
    assert result.status is EvidenceStatus.HUMAN_CONFIRMED
