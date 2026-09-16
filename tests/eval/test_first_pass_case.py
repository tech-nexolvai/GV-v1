"""A generated countertop package that produces a real PASS through sealed evidence.

The drawing bytes are synthetic and created inside ``tmp_path``. The value still travels through the
reader, answer-key type confirmation, evidence gate, project parameters and ``verdict.engine.execute``;
the test never asserts a green finding directly.

Source: issue #621 · Verification: this file.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import Engine, select

from app.models.parameters import to_rows
from app.models.rules import RuleSnapshot
from app.models.verdicts import CheckRun, Finding, VerdictInput
from eval.gold_set.first_pass_case import (
    FAIL_EXACT_INCHES,
    FAIL_READING,
    PASS_EXACT_INCHES,
    PASS_READING,
    PROJECT_ID,
    answer_key,
)
from eval.gold_set.store import content_hash
from rules.parameters import ParameterLayer, Provenance
from rules.parameters import ParameterSet as InMemoryParameterSet
from rules.parameters import ParameterValue as InMemoryParameterValue
from rules.schema import Quantity
from scripts.evaluate_goldset import ANSWER_KEY, _pdf, _private_schema, _run_pipeline, load_package
from units.measurement import Unit
from workflow.association import AssociationSettings

pytest_plugins = ("tests.app.postgres_fixture",)


def _association() -> AssociationSettings:
    """The generated one-line PDF's explicit reader configuration."""
    return AssociationSettings(
        line_minimum_pt=Decimal(1),
        glyph_maximum_pt=Decimal(1),
        glyph_gap_pt=Decimal(1),
        proximity_limit=Decimal("0.1"),
        ambiguity_margin=Decimal("0.01"),
        witness_tolerance=Decimal("0.01"),
        minimum_span=Decimal("0.02"),
        straightness=Decimal("0.0005"),
        crossing_margin=Decimal("0.001"),
    )


def _write_package(root: Path, *, raw_text: str, exact_inches: str, expected_outcome: str) -> Path:
    root.mkdir()
    drawing = _pdf(raw_text)
    (root / "shop.pdf").write_bytes(drawing)
    (root / "arch.pdf").write_bytes(drawing)
    key = answer_key(
        raw_text=raw_text,
        exact_inches=exact_inches,
        shop_hash=content_hash(root / "shop.pdf"),
        expected_outcome=expected_outcome,
    )
    (root / ANSWER_KEY).write_text(json.dumps(key, indent=2), encoding="utf-8")
    return root


def _seed_depth_parameters(session: Any, project: Any, _revision: Any) -> None:
    """Project settings behind CT-DEPTH-001: 24 in cabinet + 1 1/2 in overhang."""
    set_at = datetime(2026, 9, 16, tzinfo=UTC)
    parameters = InMemoryParameterSet(
        project_id=str(project.id),
        layer=ParameterLayer.PROJECT,
        version=1,
        parameters={
            "cabinet_depth": InMemoryParameterValue(
                value=Quantity(value=Fraction(24), unit=Unit.INCH),
                provenance=Provenance.MEASURED,
                set_by="synthetic-first-pass-case",
                set_at=set_at,
            ),
            "countertop_overhang": InMemoryParameterValue(
                value=Quantity(value=Fraction(3, 2), unit=Unit.INCH),
                provenance=Provenance.MEASURED,
                set_by="synthetic-first-pass-case",
                set_at=set_at,
            ),
        },
    )
    stored, values = to_rows(parameters)
    session.add(stored)
    session.flush()
    for value in values:
        session.add(value)


def _run_case(database_url: str, package: Path) -> tuple[Any, str]:
    case = load_package(package)
    result = _run_pipeline(
        case,
        package,
        database_url,
        dpi=150,
        association=_association(),
        localized_ocr=None,
        vendor_stamps_only=False,
        seed_project_parameters=_seed_depth_parameters,
        project_id=PROJECT_ID,
    )
    finding, run = _depth_finding(result.session)
    inputs = list(
        result.session.execute(
            select(VerdictInput)
            .where(VerdictInput.check_run_id == run.id)
            .order_by(VerdictInput.operand_name)
        ).scalars()
    )
    snapshot = result.session.get(RuleSnapshot, run.rule_snapshot_id)
    assert snapshot is not None
    fingerprint = _input_fingerprint(finding, run, snapshot.snapshot_id, inputs)
    result.session.close()
    return finding, fingerprint


def _depth_finding(session: Any) -> tuple[Finding, CheckRun]:
    rows = session.execute(
        select(Finding, CheckRun)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .where(Finding.outcome.in_(["PASS", "FAIL"]))
    ).all()
    assert len(rows) == 1
    return cast(tuple[Finding, CheckRun], rows[0])


def _input_fingerprint(
    finding: Finding, run: CheckRun, rule_snapshot_hash: str, inputs: list[VerdictInput]
) -> str:
    """Hash the deterministic verdict inputs, excluding database row identities."""
    payload = {
        "engine_version": run.engine_version,
        "rule_snapshot_hash": rule_snapshot_hash,
        "project_parameter_set": finding.parameter_set_versions.get("project"),
        "comparison": finding.reason,
        "inputs": [
            {
                "name": row.operand_name,
                "value": f"{row.value_numerator}/{row.value_denominator}",
                "unit": row.unit,
                "status": row.evidence_status,
            }
            for row in inputs
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.fixture
def database_url(postgres_engine: Engine) -> Iterator[str]:
    with _private_schema(postgres_engine.url.render_as_string(hide_password=False)) as scoped:
        yield scoped


def test_countertop_depth_gold_case_passes_from_confirmed_reading_and_is_reproducible(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    """Balanced generated package: reader-confirmed CT010 seals, then CT-DEPTH-001 passes."""
    package = _write_package(
        tmp_path / "pass",
        raw_text=PASS_READING,
        exact_inches=PASS_EXACT_INCHES,
        expected_outcome="PASS",
    )

    first_schema = _private_schema(postgres_engine.url.render_as_string(hide_password=False))
    second_schema = _private_schema(postgres_engine.url.render_as_string(hide_password=False))
    with first_schema as first_url, second_schema as second_url:
        first, first_hash = _run_case(first_url, package)
        second, second_hash = _run_case(second_url, package)

    assert first.outcome == "PASS"
    assert second.outcome == "PASS"
    assert first.reason == second.reason
    assert first_hash == second_hash
    assert first.trace["outcome"] == "PASS"


def test_countertop_depth_gold_case_perturbation_fails(database_url: str, tmp_path: Path) -> None:
    """A 1 mm-equivalent larger shop depth proves the PASS is arithmetic, not hard-coded."""
    package = _write_package(
        tmp_path / "fail",
        raw_text=FAIL_READING,
        exact_inches=FAIL_EXACT_INCHES,
        expected_outcome="FAIL",
    )

    finding, _fingerprint = _run_case(database_url, package)

    assert finding.outcome == "FAIL"
    assert finding.reason != "25 1/2 in == 25 1/2 in"
    assert finding.trace["outcome"] == "FAIL"
