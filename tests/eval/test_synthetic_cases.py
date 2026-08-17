"""Verification for issue #71: deterministic synthetic cases exercise the engine."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from eval.gold_set.schema import (
    GoldCase,
    GoldManifest,
    GroundTruth,
    Provenance,
    ReviewedDocument,
)
from eval.synthetic import (
    F1_AUTHORED_TOKEN,
    SYNTHETIC_CASES_DIRECTORY,
    SeededError,
    SyntheticCase,
    generate_synthetic_cases,
    load_synthetic_cases,
    run_synthetic_case,
)
from rules.semantic_types import OperandSource, ProductType
from units.dual import parse_dual
from units.policy import Consistency, check_dual
from verdict.operations.scalar import SCALAR_SPECS
from verdict.outcomes import Outcome
from verdict.registry import REGISTRY, register


@pytest.fixture(autouse=True)
def _registered_scalar_operations() -> object:
    previous = dict(REGISTRY)
    REGISTRY.clear()
    for spec in SCALAR_SPECS:
        register(spec)
    yield
    REGISTRY.clear()
    REGISTRY.update(previous)


def test_generator_is_deterministic_and_covers_all_four_primary_outcomes() -> None:
    first = generate_synthetic_cases()
    second = generate_synthetic_cases()

    assert first == second
    assert {case.expected.outcome for case in first} == {
        Outcome.PASS,
        Outcome.FAIL,
        Outcome.NOT_FOUND,
        Outcome.REVIEW_REQUIRED,
    }


def test_every_case_runs_through_the_engine_and_matches_its_authored_expectation() -> None:
    findings = [run_synthetic_case(case) for case in generate_synthetic_cases()]

    assert [finding.outcome for finding in findings] == [
        Outcome.PASS,
        Outcome.FAIL,
        Outcome.FAIL,
        Outcome.NOT_FOUND,
        Outcome.REVIEW_REQUIRED,
        Outcome.REVIEW_REQUIRED,
    ]


def test_every_required_error_class_has_one_explicit_builder_result() -> None:
    errors = [case.seeded_error for case in generate_synthetic_cases()]

    assert errors == [
        None,
        SeededError.OFF_BY_TOLERANCE,
        SeededError.COUNT_MISMATCH,
        SeededError.MISSING_OPERAND,
        SeededError.UNIT_MISMATCH,
        SeededError.F1_DUAL_UNIT_ROUNDING,
    ]


def test_tolerance_boundary_passes_and_one_beyond_fails_exactly() -> None:
    passing, failing = generate_synthetic_cases()[:2]

    assert run_synthetic_case(passing).outcome is Outcome.PASS
    assert run_synthetic_case(failing).outcome is Outcome.FAIL
    assert run_synthetic_case(passing).delta is not None
    assert run_synthetic_case(failing).delta is not None
    assert run_synthetic_case(passing).delta.exact == 2
    assert run_synthetic_case(failing).delta.exact == 3


def test_f1_case_preserves_and_exercises_the_authored_dual_token() -> None:
    case = generate_synthetic_cases()[-1]
    token = case.operands["authored_dual_token"].value

    assert token == F1_AUTHORED_TOKEN == "984 [38 3/4]"
    assert check_dual(parse_dual(token)) is Consistency.CONSISTENT_WITHIN_ROUNDING
    assert run_synthetic_case(case).outcome is Outcome.REVIEW_REQUIRED


def test_all_three_synthetic_identity_markers_are_enforced() -> None:
    case = generate_synthetic_cases()[0]

    assert isinstance(case, SyntheticCase)
    assert case.synthetic is True
    assert case.case_id.startswith("SYNTH-")
    with pytest.raises(ValueError, match="SYNTH-"):
        SyntheticCase(
            case_id="REAL-CASE",
            synthetic=True,
            rule_snapshot=case.rule_snapshot,
            operands=case.operands,
            parameters=case.parameters,
            discriminators=case.discriminators,
            expected=case.expected,
            seeded_error=None,
        )
    with pytest.raises(ValueError, match="synthetic=True"):
        SyntheticCase(
            case_id="SYNTH-BAD-FLAG",
            synthetic=False,  # type: ignore[arg-type]
            rule_snapshot=case.rule_snapshot,
            operands=case.operands,
            parameters=case.parameters,
            discriminators=case.discriminators,
            expected=case.expected,
            seeded_error=None,
        )


def test_synthetic_loader_rejects_real_cases_and_real_case_directory(tmp_path: Path) -> None:
    real_case = GoldCase(
        id="REAL-1",
        product_type=ProductType.CABINET,
        arch=Path("arch.pdf"),
        shop=Path("shop.pdf"),
        ground_truth=GroundTruth(observations=(), matches=(), expected_findings=()),
        provenance=Provenance(
            annotator="reviewer@example.com",
            annotated_on=date(2026, 8, 15),
            documents=(
                ReviewedDocument(
                    source=OperandSource.SHOP,
                    document_version_id=UUID("12345678-1234-5678-1234-567812345678"),
                    content_hash="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                ),
            ),
        ),
    )

    with pytest.raises(TypeError, match="GoldCase"):
        load_synthetic_cases([real_case], directory=tmp_path / "synthetic")
    with pytest.raises(ValueError, match="real gold-set directory"):
        load_synthetic_cases(
            generate_synthetic_cases(),
            directory=SYNTHETIC_CASES_DIRECTORY.parent / "gold_set" / "cases",
        )


def test_synthetic_loader_creates_only_its_dedicated_directory(tmp_path: Path) -> None:
    directory = tmp_path / "synthetic" / "cases"

    loaded = load_synthetic_cases(generate_synthetic_cases(), directory=directory)

    assert loaded == generate_synthetic_cases()
    assert directory.is_dir()


def test_real_gold_manifest_rejects_synthetic_case_shape() -> None:
    with pytest.raises(ValidationError):
        GoldManifest.model_validate(
            {
                "version": 0,
                "cases": [
                    {
                        "case_id": "SYNTH-NOT-REAL",
                        "synthetic": True,
                    }
                ],
            }
        )
