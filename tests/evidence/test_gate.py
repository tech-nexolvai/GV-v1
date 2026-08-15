"""Verification for issue #121: only qualified evidence can cross the gate."""

from __future__ import annotations

import ast
import json
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from uuid import UUID

import pytest

import verdict.operands
from evidence.canonical import Authority, CanonicalObservation, CorroborationLane
from evidence.coordinates import StoredPoint
from evidence.gate import (
    EvidenceStatus,
    GateRefusal,
    RefusalReason,
    VerdictOperand,
    seal,
)
from evidence.polygon import Polygon
from rules.semantic_types import DocumentRole, SemanticType
from units.measurement import Measurement, Unit

DOCUMENT_ID = UUID("12345678-1234-5678-1234-567812345678")
ROOT = Path(__file__).resolve().parents[2]


def _observation(
    status: EvidenceStatus = EvidenceStatus.CORROBORATED,
    *,
    authority: Authority = Authority.AUTHORITATIVE,
) -> CanonicalObservation:
    """Build valid provenance for each status before testing the gate decision."""

    supported_by = ("vector", "ocr")
    corroborated_by: tuple[CorroborationLane, ...] = ()
    conflicts_with: tuple[str, ...] = ()
    if status is EvidenceStatus.HUMAN_CONFIRMED:
        supported_by = ()
        corroborated_by = (CorroborationLane.HUMAN,)
    elif status is EvidenceStatus.RAW_CANDIDATE:
        supported_by = ("vector",)
    elif status is EvidenceStatus.CONFLICTING:
        supported_by = ("vector",)
        conflicts_with = ("ocr",)
    elif status is EvidenceStatus.REJECTED:
        supported_by = ()

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
        authority=authority,
        supported_by=supported_by,
        corroborated_by=corroborated_by,
        conflicts_with=conflicts_with,
        evidence_crop_uri="s3://evidence/crop.png",
    )


@pytest.mark.parametrize(
    "status",
    [EvidenceStatus.CORROBORATED, EvidenceStatus.HUMAN_CONFIRMED],
)
def test_qualified_authoritative_observation_seals(status: EvidenceStatus) -> None:
    """Input: qualified evidence. Outcome: operand. Why: it may reach arithmetic."""

    observation = _observation(status)

    result = seal(observation, "cabinet_width")

    assert isinstance(result, VerdictOperand)
    assert result.name == "cabinet_width"
    assert result.value is observation.value
    assert result.status is status
    assert result.source == DocumentRole.SHOP.value


@pytest.mark.parametrize(
    "status",
    [
        EvidenceStatus.RAW_CANDIDATE,
        EvidenceStatus.CONFLICTING,
        EvidenceStatus.REJECTED,
    ],
)
def test_unqualified_status_refuses_instead_of_guessing(status: EvidenceStatus) -> None:
    """Input: unqualified evidence. Outcome: refusal. Why: no false verdict input."""

    result = seal(_observation(status), "cabinet_width")

    assert result == GateRefusal(
        RefusalReason.NOT_QUALIFIED,
        f"evidence status {status.value} is not qualified for a verdict",
    )


def test_advisory_observation_refuses_even_when_corroborated() -> None:
    """Input: corroborated advisory value. Outcome: refusal. Why: advice is not truth."""

    result = seal(
        _observation(authority=Authority.ADVISORY),
        "cabinet_width",
    )

    assert isinstance(result, GateRefusal)
    assert result.reason is RefusalReason.ADVISORY


def test_missing_value_refuses_if_stale_data_bypassed_model_validation() -> None:
    """Input: corrupt qualified record with no value. Outcome: NO_VALUE, never default zero."""

    observation = _observation()
    object.__setattr__(observation, "value", None)

    result = seal(observation, "cabinet_width")

    assert isinstance(result, GateRefusal)
    assert result.reason is RefusalReason.NO_VALUE


def test_unknown_unit_refuses_if_stale_data_bypassed_model_validation() -> None:
    """Input: corrupt authored unit. Outcome: UNKNOWN_UNIT. Why: conversion cannot be guessed."""

    observation = _observation()
    measurement = Measurement(Fraction(984), Unit.MM, "984")
    object.__setattr__(measurement, "unit", "unknown")
    object.__setattr__(observation, "value", measurement)

    result = seal(observation, "cabinet_width")

    assert isinstance(result, GateRefusal)
    assert result.reason is RefusalReason.UNKNOWN_UNIT


def test_evidence_reference_preserves_exact_page_and_polygon() -> None:
    """Input: decimal polygon. Outcome: exact strings. Why: provenance must not round."""

    result = seal(_observation(), "cabinet_width")

    assert isinstance(result, VerdictOperand)
    assert result.evidence_ref is not None
    evidence = json.loads(result.evidence_ref)
    assert evidence == {
        "document_version_id": str(DOCUMENT_ID),
        "page": 3,
        "polygon": [["0.1", "0.2"], ["0.4", "0.2"], ["0.4", "0.5"]],
        "space": "stored",
    }


def test_empty_name_is_a_programming_error_not_a_gate_outcome() -> None:
    """Input: empty binding name. Outcome: exception. Why: callers must identify operands."""

    with pytest.raises(ValueError, match="must be named"):
        seal(_observation(), "  ")


def test_gate_reuses_the_verdict_contract_types() -> None:
    """Input: imported types. Outcome: identity. Why: duplicate contracts could drift."""

    assert EvidenceStatus is verdict.operands.EvidenceStatus
    assert VerdictOperand is verdict.operands.VerdictOperand


def test_only_the_gate_constructs_verdict_operands_in_evidence_package() -> None:
    """Input: evidence source tree. Outcome: no bypass. Why: all facts must be sealed."""

    offenders: list[str] = []
    for path in sorted((ROOT / "evidence").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if path.name == "gate.py":
            continue
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "VerdictOperand"
            for node in ast.walk(tree)
        ):
            offenders.append(path.relative_to(ROOT).as_posix())

    assert offenders == [], f"VerdictOperand bypasses the evidence gate: {offenders}"


def test_evidence_package_does_not_import_the_verdict_engine() -> None:
    """Input: evidence imports. Outcome: no engine dependency. Why: boundary stays one-way."""

    offenders: list[str] = []
    for path in sorted((ROOT / "evidence").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                alias.name == "verdict.engine" for alias in node.names
            ):
                offenders.append(path.relative_to(ROOT).as_posix())
            if isinstance(node, ast.ImportFrom) and node.module == "verdict.engine":
                offenders.append(path.relative_to(ROOT).as_posix())

    assert offenders == [], f"evidence must not import verdict.engine: {offenders}"
