"""The complete, exact finding chain returned by D1.2 (#223).

The fixture is intentionally small: two equal authored inch readings. The important assertion is
that the response alone can rebuild those operands and rerun the sealed rule to the same outcome.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from decimal import Decimal
from fractions import Fraction
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.api import finding_chain
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.main import API_PREFIX, create_app
from app.models import (
    CanonicalObservation,
    CheckRun,
    Document,
    DocumentKind,
    DocumentVersion,
    Finding,
    FindingEvidence,
    Package,
    PackageRevision,
    PackageState,
    Page,
    Project,
    RuleDefinition,
    RuleSnapshot,
    SourceArtifact,
    VerdictInput,
)
from evidence.canonical import Authority
from rules.schema import (
    CheckType,
    GlobalApplicability,
    InputSelector,
    OperationRef,
    Rule,
)
from rules.semantic_types import OperandSource, ProductType, SemanticType
from rules.snapshot import canonical_json, publish
from tests.app.postgres_fixture import alembic_config
from units.measurement import Measurement, Unit
from verdict.engine import execute
from verdict.operands import EvidenceStatus, VerdictOperand
from verdict.outcomes import Outcome, Severity

pytest_plugins = ("tests.app.postgres_fixture",)

DATABASE_URL = "postgresql+psycopg://gv:gv@localhost:5433/gv"


def _upgrade(engine: Engine) -> None:
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _rule() -> Rule:
    return Rule(
        id="CHAIN-EQUALS-001",
        version="1.0.0",
        product_type=ProductType.COUNTERTOP,
        check_type=CheckType.ARCH_VS_SHOP,
        severity=Severity.CRITICAL,
        arithmetic_unit=Unit.INCH,
        inputs={
            "actual": InputSelector(source=OperandSource.SHOP, semantic_type=SemanticType.CT001),
            "expected": InputSelector(source=OperandSource.ARCH, semantic_type=SemanticType.CT001),
        },
        applicability=GlobalApplicability(scope="global"),
        operation=OperationRef(
            type="equals", operands={"actual": "actual", "expected": "expected"}
        ),
    )


def _client(session: Session, project_id: UUID) -> TestClient:
    principal = Principal(
        id="reviewer-1", roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
    )
    app = create_app(Settings(database_url=DATABASE_URL))  # type: ignore[call-arg]
    app.dependency_overrides[authenticate] = lambda: principal
    app.dependency_overrides[finding_chain.get_session] = lambda: session
    return TestClient(app, raise_server_exceptions=False)


def _seed(session: Session) -> tuple[UUID, UUID, UUID]:
    project = Project(name="Chain project")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Fixture vendor")
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.RUNNING_CHECKS
    )
    session.add(revision)
    session.flush()

    digest = "a" * 64
    artifact = SourceArtifact(
        storage_key=f"sha256/{digest}", sha256=digest, size=10, backend_version_id=None
    )
    session.add(artifact)
    session.flush()
    document = Document(package_id=package.id, kind=DocumentKind.SHOP)
    session.add(document)
    session.flush()
    version = DocumentVersion(
        document_id=document.id,
        source_artifact_id=artifact.id,
        sha256=digest,
        page_count=1,
    )
    session.add(version)
    session.flush()
    page = Page(
        document_version_id=version.id,
        index=2,
        content_hash="b" * 64,
        width_pt=Decimal(612),
        height_pt=Decimal(792),
        rotation=0,
        has_vector_text=True,
        render_failed=False,
        sheet_number="A-103",
        page_type=None,
        revision_label=None,
        revision_date_raw=None,
        revision_date_interpretations=None,
        revision_sequence_index=None,
    )
    session.add(page)
    session.flush()

    observations: list[CanonicalObservation] = []
    for role, semantic in (("SHOP", SemanticType.CT001), ("ARCH", SemanticType.CT001)):
        observation = CanonicalObservation(
            document_version_id=version.id,
            page_id=page.id,
            document_role=role,
            polygon=[["0.1", "0.1"], ["0.2", "0.1"], ["0.2", "0.2"]],
            coordinate_space="stored",
            semantic_type=semantic,
            value_numerator=155,
            value_denominator=4,
            unit=Unit.INCH,
            status=EvidenceStatus.CORROBORATED,
            authority=Authority.AUTHORITATIVE,
            evidence_crop_uri=f"evidence/{role.lower()}-ct001.png",
        )
        session.add(observation)
        observations.append(observation)
    session.flush()

    authored_rule = _rule()
    body = canonical_json(authored_rule)
    definition = RuleDefinition(rule_id=authored_rule.id)
    session.add(definition)
    session.flush()
    snapshot = RuleSnapshot(
        rule_definition_id=definition.id,
        snapshot_id=f"sha256:{hashlib.sha256(body.encode()).hexdigest()}",
        version=authored_rule.version,
        canonical_json=body,
        product_type=authored_rule.product_type,
        check_type=authored_rule.check_type,
        unconfirmed_tolerance_count=0,
    )
    session.add(snapshot)
    session.flush()
    run = CheckRun(
        package_revision_id=revision.id,
        rule_snapshot_id=snapshot.id,
        engine_version="verdict-test-1",
    )
    session.add(run)
    session.flush()
    for name, observation in zip(("actual", "expected"), observations, strict=True):
        session.add(
            VerdictInput(
                check_run_id=run.id,
                operand_name=name,
                value_numerator=155,
                value_denominator=4,
                unit=Unit.INCH,
                evidence_status=EvidenceStatus.CORROBORATED,
                canonical_observation_id=observation.id,
            )
        )
    finding = Finding(
        check_run_id=run.id,
        package_revision_id=revision.id,
        outcome=Outcome.PASS,
        severity=Severity.CRITICAL,
        trace={"comparison": "155/4 == 155/4", "outcome": Outcome.PASS},
        parameter_set_versions={"global": "sha256:parameters"},
    )
    session.add(finding)
    session.flush()
    session.add_all(
        FindingEvidence(
            finding_id=finding.id, canonical_observation_id=observation.id, role="operand"
        )
        for observation in observations
    )
    session.flush()
    return project.id, package.id, finding.id


def test_chain_reconstructs_the_same_verdict_from_exact_stored_inputs(session: Session) -> None:
    """Input: two stored 38 3/4-inch readings. Output: PASS again. Why: the API is an audit chain,
    not merely a human-readable summary."""

    project_id, package_id, finding_id = _seed(session)
    response = _client(session, project_id).get(
        f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/findings/{finding_id}/chain"
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["rule_snapshot"]["snapshot_id"].startswith("sha256:")
    assert body["parameter_versions"] == {"global": "sha256:parameters"}
    assert body["engine_version"] == "verdict-test-1"
    assert [operand["name"] for operand in body["operands"]] == ["actual", "expected"]
    assert all(operand["numerator"] == "155" for operand in body["operands"])
    assert all(operand["denominator"] == "4" for operand in body["operands"])
    assert all(operand["evidence"]["page_index"] == 2 for operand in body["operands"])
    assert all(operand["evidence"]["polygon"] for operand in body["operands"])
    assert all(operand["evidence"]["crop_uri"] for operand in body["operands"])

    restored_rule = Rule.model_validate_json(body["rule_snapshot"]["canonical_json"])
    restored_operands = {
        operand["name"]: VerdictOperand(
            name=operand["name"],
            value=Measurement(
                Fraction(int(operand["numerator"]), int(operand["denominator"])),
                Unit(operand["unit"]),
                None,
            ),
            status=EvidenceStatus(operand["evidence_status"]),
            source=operand["evidence"]["document_role"],
            evidence_ref=str(operand["evidence"]["canonical_observation_id"]),
        )
        for operand in body["operands"]
    }
    recomputed = execute(publish(restored_rule), restored_operands)
    assert recomputed.outcome.value == body["outcome"]


def test_a_chain_in_another_project_is_not_disclosed(session: Session) -> None:
    """Input: a valid finding id under the wrong project. Output: 404. Why: audit detail must not
    become a cross-project existence oracle."""

    project_id, package_id, finding_id = _seed(session)
    other_project = Project(name="Other project")
    session.add(other_project)
    session.flush()
    response = _client(session, other_project.id).get(
        f"{API_PREFIX}/projects/{other_project.id}/packages/{package_id}/findings/{finding_id}/chain"
    )
    assert response.status_code == 404
    assert response.json()["error"] == "http_error"
    assert response.json()["message"] == "Not found"
    assert str(project_id) not in response.text
