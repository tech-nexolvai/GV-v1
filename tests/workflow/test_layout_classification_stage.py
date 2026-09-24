"""Workflow wiring for closed-question layout discriminator proposals (#660)."""

from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
import yaml  # type: ignore[import-untyped]
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.documents import storage_key
from app.db.session import session_factory
from app.models import (
    CheckRun,
    Document,
    DocumentVersion,
    Finding,
    LayoutProposal,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    Project,
    RuleDefinition,
    RuleSnapshot,
    SourceArtifact,
)
from app.models.evidence import EvidenceArtifact
from app.models.runs import TaskRun, WorkflowRun
from app.verdicts.rulebook import from_row
from evidence.crop import RenderedPage
from extraction.layout import ClosedQuestion, LayoutReaderResult, full_page_region
from rules.required_inputs import required_inputs
from rules.schema import Rule
from rules.snapshot import publish
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.test_reader import _pdf
from workflow.idempotency import stage_idempotency_key
from workflow.review import ENGINE_VERSION
from workflow.stages import LAYOUT_PROMPT_ID, UNCONFIGURED_LAYOUT_MODEL_ID, DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

RULEBOOK = Path(__file__).resolve().parents[2] / "rules" / "rulebook"
DRAWING = _pdf(b"BT /F1 10 Tf 1 0 0 1 20 70 Tm (PLAN VIEW) Tj ET\n")


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


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


class _AnsweringReader:
    def read(self, rendered: RenderedPage, question: ClosedQuestion) -> LayoutReaderResult:
        return LayoutReaderResult(
            reader="answering-reader",
            answer=question.choices[0],
            region=full_page_region(rendered),
            reason="the plan view visibly matches this closed choice",
        )


class _SecondChoiceReader:
    def read(self, rendered: RenderedPage, question: ClosedQuestion) -> LayoutReaderResult:
        return LayoutReaderResult(
            reader="second-choice-reader",
            answer=question.choices[1],
            region=full_page_region(rendered),
            reason="the plan view visibly matches a different closed choice",
        )


def _publish_rulebook(session: Session) -> tuple[Rule, ...]:
    rules: list[Rule] = []
    for path in sorted(RULEBOOK.glob("*.yaml")):
        rule = Rule.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        snapshot = publish(rule)
        definition = RuleDefinition(rule_id=rule.id)
        session.add(definition)
        session.flush()
        session.add(
            RuleSnapshot(
                rule_definition_id=definition.id,
                snapshot_id=snapshot.snapshot_id,
                version=rule.version,
                canonical_json=snapshot.canonical_json,
                product_type=rule.product_type.value,
                check_type=rule.check_type.value,
                unconfirmed_tolerance_count=0,
            )
        )
        rules.append(rule)
    session.flush()
    return tuple(rules)


def _revision(session: Session, store: LocalStore) -> PackageRevision:
    digest = hashlib.sha256(DRAWING).hexdigest()
    project = Project(name=f"layout classification {uuid4()}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Apex Glass & Stone")
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id,
        revision_number=1,
        state=PackageState.EXTRACTING,
    )
    session.add(revision)
    session.flush()

    document = Document(package_id=package.id, kind="shop")
    session.add(document)
    session.flush()
    key = storage_key(document.id, digest)
    artifact = SourceArtifact(storage_key=key, sha256=digest, size=len(DRAWING))
    session.add(artifact)
    session.flush()
    version = DocumentVersion(
        document_id=document.id,
        source_artifact_id=artifact.id,
        sha256=digest,
        page_count=1,
    )
    session.add(version)
    session.flush()
    session.add(
        PackageRevisionDocument(
            package_revision_id=revision.id,
            package_id=package.id,
            document_id=document.id,
            document_version_id=version.id,
        )
    )
    workflow_run = WorkflowRun(package_revision_id=revision.id, engine_run_id=str(uuid4()))
    session.add(workflow_run)
    session.flush()
    session.add(
        TaskRun(
            workflow_run_id=workflow_run.id,
            idempotency_key=stage_idempotency_key(
                package_revision_id=revision.id,
                stage="extract_pages",
                engine_version=ENGINE_VERSION,
            ),
            task_type="extract_pages",
            attempt=1,
            outcome="claimed",
        )
    )
    session.flush()
    store.put(key, io.BytesIO(DRAWING), content_type="application/pdf")
    return revision


def _proposals(session: Session) -> list[LayoutProposal]:
    return list(session.execute(select(LayoutProposal)).scalars())


def _outcome_for(session: Session, revision: PackageRevision, rule_id: str) -> str | None:
    rows = session.execute(
        select(Finding, RuleSnapshot)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .where(Finding.package_revision_id == revision.id)
    ).all()
    for finding, snapshot in rows:
        if from_row(snapshot).rule.id == rule_id:
            return str(finding.outcome)
    return None


def test_extract_pages_records_one_layout_proposal_per_declared_discriminator(
    session: Session, store: LocalStore
) -> None:
    rules = _publish_rulebook(session)
    revision = _revision(session, store)
    expected = len(required_inputs(rules).discriminators)
    assert expected > 0, "the published rulebook declared no layout discriminators"

    DatabaseStages(store, layout_readers=[_AnsweringReader()]).extract_pages(session, revision.id)

    proposals = _proposals(session)
    assert len(proposals) == expected
    assert {proposal.proposed_value for proposal in proposals} <= {
        choice for need in required_inputs(rules).discriminators for choice in need.choices
    }
    for proposal in proposals:
        artifact = session.get(EvidenceArtifact, proposal.crop_artifact_id)
        assert artifact is not None
        assert store.get(artifact.storage_key).read().startswith(b"\x89PNG")


def test_absent_layout_reader_configuration_records_abstentions(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    rules = _publish_rulebook(session)
    revision = _revision(session, store)
    assert required_inputs(rules).discriminators
    monkeypatch.delenv("GV_BEDROCK_LAYOUT_ENABLED", raising=False)
    monkeypatch.delenv("GV_BEDROCK_LAYOUT_MODEL", raising=False)
    monkeypatch.delenv("GV_BEDROCK_MODEL", raising=False)

    DatabaseStages(store).extract_pages(session, revision.id)

    proposals = _proposals(session)
    assert len(proposals) == len(required_inputs(rules).discriminators)
    assert {proposal.proposed_value for proposal in proposals} == {"abstained"}
    assert {proposal.model_id for proposal in proposals} == {UNCONFIGURED_LAYOUT_MODEL_ID}
    assert {proposal.prompt_id for proposal in proposals} == {LAYOUT_PROMPT_ID}


def test_layout_reader_disagreement_is_recorded_without_a_closed_choice(
    session: Session, store: LocalStore
) -> None:
    rules = _publish_rulebook(session)
    revision = _revision(session, store)
    assert all(len(need.choices) > 1 for need in required_inputs(rules).discriminators)

    DatabaseStages(
        store,
        layout_readers=[_AnsweringReader(), _SecondChoiceReader()],
    ).extract_pages(session, revision.id)

    choices = {choice for need in required_inputs(rules).discriminators for choice in need.choices}
    proposals = _proposals(session)
    assert {proposal.proposed_value for proposal in proposals} == {"disagreement"}
    assert all(proposal.proposed_value not in choices for proposal in proposals)


def test_rerunning_extraction_over_unchanged_layout_evidence_does_not_duplicate(
    session: Session, store: LocalStore
) -> None:
    _publish_rulebook(session)
    revision = _revision(session, store)
    stages = DatabaseStages(store, layout_readers=[_AnsweringReader()])

    stages.extract_pages(session, revision.id)
    first = session.execute(select(func.count()).select_from(LayoutProposal)).scalar_one()
    stages.extract_pages(session, revision.id)

    assert session.execute(select(func.count()).select_from(LayoutProposal)).scalar_one() == first


def test_layout_proposals_still_do_not_supply_run_checks_discriminators(
    session: Session, store: LocalStore
) -> None:
    _publish_rulebook(session)
    revision = _revision(session, store)
    DatabaseStages(store, layout_readers=[_AnsweringReader()]).extract_pages(session, revision.id)

    result = DatabaseStages().run_checks(session, revision.id)

    assert result["ran"] is True
    assert _outcome_for(session, revision, "CT-WIDTH-001") == "REVIEW_REQUIRED"
