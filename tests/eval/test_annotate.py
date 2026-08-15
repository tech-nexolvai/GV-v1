"""Readable verification examples for issue #129's gold-case annotation format."""

from __future__ import annotations

from datetime import date
from fractions import Fraction
from uuid import UUID

import pytest
import yaml
from pydantic import ValidationError

from eval.gold_set.annotate import build_case
from eval.gold_set.schema import (
    Disagreement,
    ExpectedFinding,
    GoldCase,
    GoldMatch,
    GoldObservation,
    GroundTruth,
    Provenance,
)
from rules.semantic_types import OperandSource, ProductType, SemanticType
from units.measurement import Measurement, Unit
from verdict.outcomes import Outcome

DOCUMENT_VERSION_ID = UUID("12345678-1234-5678-1234-567812345678")


def observation() -> GoldObservation:
    """Return the reviewed input 38 3/4 inches with its authored token intact."""

    return GoldObservation(
        semantic_type=SemanticType.CABINET_WIDTH,
        source=OperandSource.SHOP,
        value=Measurement(Fraction(155, 4), Unit.INCH, "38 3/4"),
        page=3,
        polygon=(10, 20, 110, 40),
        item_id="S-CAB-1",
    )


def provenance() -> Provenance:
    """Return explicit reviewer and immutable-document provenance for the fixture."""

    return Provenance(
        annotator="reviewer@example.com",
        annotated_on=date(2026, 8, 15),
        document_version_id=DOCUMENT_VERSION_ID,
        content_hash="sha256:reviewed-package",
    )


def ground_truth(outcome: Outcome = Outcome.PASS) -> GroundTruth:
    """Return one reviewed observation, match and explained finding."""

    return GroundTruth(
        observations=(observation(),),
        matches=(GoldMatch(arch_item="A-CAB-1", shop_item="S-CAB-1"),),
        expected_findings=(
            ExpectedFinding(
                check="CAB-WIDTH-001",
                outcome=outcome,
                reason="The reviewed architectural and shop dimensions agree exactly.",
            ),
        ),
    )


def test_build_case_preserves_exact_measurement_and_provenance() -> None:
    """Input 38 3/4 remains exact 155/4; no decimal or reconstructed token appears."""

    case = build_case(
        case_id="CAB-PROJECT-001",
        product_type=ProductType.CABINET,
        arch="private/arch.pdf",
        shop="private/shop.pdf",
        ground_truth=ground_truth(),
        provenance=provenance(),
    )

    value = case.ground_truth.observations[0].value
    assert value.exact == Fraction(155, 4)
    assert value.unit is Unit.INCH
    assert value.raw_text == "38 3/4"
    assert case.provenance.document_version_id == DOCUMENT_VERSION_ID


def test_measurement_round_trips_through_hand_editable_yaml() -> None:
    """The YAML input uses exact text and loads back without binary floating-point loss."""

    original = build_case(
        case_id="CAB-PROJECT-001",
        product_type=ProductType.CABINET,
        arch="private/arch.pdf",
        shop="private/shop.pdf",
        ground_truth=ground_truth(),
        provenance=provenance(),
    )
    text = yaml.safe_dump(original.model_dump(mode="json"), sort_keys=False)
    restored = GoldCase.model_validate(yaml.safe_load(text))

    assert "exact: 155/4" in text
    assert "raw_text: 38 3/4" in text
    assert restored == original


def test_float_measurement_is_rejected_loudly() -> None:
    """Input exact=38.75 is rejected because a float cannot enter the answer key."""

    data = observation().model_dump(mode="json")
    data["value"]["exact"] = 38.75

    with pytest.raises(ValidationError, match="exact"):
        GoldObservation.model_validate(data)


@pytest.mark.parametrize("outcome", tuple(Outcome), ids=lambda outcome: outcome.value)
def test_all_five_expected_outcomes_are_expressible(outcome: Outcome) -> None:
    """Every verdict outcome, including NO_APPLICABLE_RULE, is valid gold truth."""

    finding = ExpectedFinding(check="CHECK-1", outcome=outcome, reason="Reviewed expectation")

    assert finding.outcome is outcome


def test_expected_finding_without_reason_is_invalid() -> None:
    """An unexplained expected FAIL cannot adjudicate a later regression dispute."""

    with pytest.raises(ValidationError, match="reason"):
        ExpectedFinding.model_validate({"check": "CHECK-1", "outcome": "FAIL"})


@pytest.mark.parametrize(
    "missing",
    ["annotator", "annotated_on", "document_version_id", "content_hash"],
)
def test_missing_provenance_is_rejected(missing: str) -> None:
    """Reviewer identity, date and immutable document identity are all required inputs."""

    data = provenance().model_dump(mode="json")
    del data[missing]

    with pytest.raises(ValidationError, match=missing):
        Provenance.model_validate(data)


def test_disagreement_survives_case_round_trip_without_resolution() -> None:
    """Input '984' versus '985' remains recorded; the helper chooses neither value."""

    disagreement = Disagreement(
        annotator="second-reviewer@example.com",
        field="ground_truth.observations[0].value",
        their_value="985",
        note="Second reviewer reads the final digit differently.",
    )
    case = build_case(
        case_id="CAB-PROJECT-001",
        product_type=ProductType.CABINET,
        arch="private/arch.pdf",
        shop="private/shop.pdf",
        ground_truth=ground_truth(),
        provenance=provenance(),
        disagreements=(disagreement,),
    )

    restored = GoldCase.model_validate(case.model_dump(mode="json"))

    assert restored.disagreements == (disagreement,)
    assert restored.ground_truth.observations[0].value == observation().value
