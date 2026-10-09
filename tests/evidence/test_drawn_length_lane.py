"""The architect's printed value, read by code and witnessed by its drawn length, may be sealed (#1054).

The architect's numbers are real text in the drawing, read by code (no model), and #1052 keeps a
value only when the length drawn between its ticks, through the drawing's own scale, agrees with it.
That is one exact reading plus a non-model witness — the shape `DUAL_UNIT` already has — and it is
named `DRAWN_LENGTH`. It qualifies the architect's side only: a vendor's reading still needs both
readers, and a model's reading never gets this lane.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from uuid import UUID

import pytest

from evidence.canonical import Authority, CanonicalObservation, CorroborationLane
from evidence.coordinates import StoredPoint
from evidence.gate import EvidenceStatus, GateRefusal, RefusalReason, VerdictOperand, seal
from evidence.polygon import Polygon
from rules.semantic_types import DocumentRole, SemanticType
from units.measurement import Measurement, Unit

DOCUMENT_ID = UUID("87654321-4321-8765-4321-876543218765")


def _observation(
    role: DocumentRole,
    *,
    supported_by: tuple[str, ...] = ("architect-text",),
    lanes: tuple[CorroborationLane, ...] = (CorroborationLane.DRAWN_LENGTH,),
    status: EvidenceStatus = EvidenceStatus.CORROBORATED,
) -> CanonicalObservation:
    polygon = Polygon(
        points=(
            StoredPoint(Decimal("0.1"), Decimal("0.2")),
            StoredPoint(Decimal("0.4"), Decimal("0.2")),
            StoredPoint(Decimal("0.4"), Decimal("0.5")),
        ),
        space="stored",
        document_version_id=DOCUMENT_ID,
        page=0,
    )
    return CanonicalObservation(
        document_version_id=DOCUMENT_ID,
        document_role=role,
        page=0,
        polygon=polygon,
        semantic_type=SemanticType.COUNTERTOP_OVERALL_WIDTH,
        value=Measurement(Fraction(42), Unit.INCH, "3' - 6\""),
        status=status,
        authority=Authority.AUTHORITATIVE,
        supported_by=supported_by,
        corroborated_by=lanes,
        conflicts_with=(),
        evidence_crop_uri=None,
    )


def test_an_architect_value_with_its_drawn_length_witness_is_sealed() -> None:
    operand = seal(_observation(DocumentRole.ARCH), "architect_overall")

    assert isinstance(operand, VerdictOperand)
    assert operand.source == "ARCH"
    assert operand.status is EvidenceStatus.CORROBORATED
    assert operand.value == Measurement(Fraction(42), Unit.INCH, "3' - 6\"")


def test_the_drawn_length_lane_never_qualifies_a_single_vendor_reading() -> None:
    with pytest.raises(ValueError, match="architect"):
        _observation(DocumentRole.SHOP)


def test_the_gate_refuses_a_vendor_reading_carrying_the_lane_even_if_built_around_validation() -> (
    None
):
    observation = _observation(DocumentRole.ARCH)
    # Defence in depth: bypass the model's own validation, as corrupt stored data might.
    object.__setattr__(observation, "document_role", DocumentRole.SHOP)

    refused = seal(observation, "vendor_overall")

    assert isinstance(refused, GateRefusal)
    assert refused.reason is RefusalReason.NOT_QUALIFIED


def test_a_single_architect_reading_without_the_witness_is_still_refused() -> None:
    with pytest.raises(ValueError):
        _observation(DocumentRole.ARCH, lanes=())


def test_the_lane_does_not_turn_a_raw_candidate_into_evidence() -> None:
    refused = seal(
        _observation(DocumentRole.ARCH, status=EvidenceStatus.RAW_CANDIDATE, lanes=()),
        "architect_overall",
    )

    assert isinstance(refused, GateRefusal)
    assert refused.reason is RefusalReason.NOT_QUALIFIED
