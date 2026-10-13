"""Workflow wiring for closed-question layout discriminator proposals (#660)."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
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
    ModelInvocation,
    ObservationCandidate,
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
from extraction.layout import (
    TOOL_NAME,
    BedrockClosedQuestionConfig,
    BedrockClosedQuestionReader,
    ClosedQuestion,
    LayoutReaderResult,
    full_page_region,
)
from rules.required_inputs import required_inputs
from rules.schema import Rule
from rules.snapshot import publish
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.test_reader import MISSING_SPACE, _pdf
from workflow.idempotency import stage_idempotency_key
from workflow.review import ENGINE_VERSION
from workflow.stages import (
    LAYOUT_PROMPT_ID,
    LAYOUT_TEMPLATE_ID,
    UNCONFIGURED_LAYOUT_MODEL_ID,
    DatabaseStages,
)

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

    DatabaseStages(
        store, layout_readers=[_AnsweringReader()], missing_space=MISSING_SPACE
    ).extract_pages(session, revision.id)

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

    DatabaseStages(store, missing_space=MISSING_SPACE).extract_pages(session, revision.id)

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
        missing_space=MISSING_SPACE,
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
    stages = DatabaseStages(store, layout_readers=[_AnsweringReader()], missing_space=MISSING_SPACE)

    stages.extract_pages(session, revision.id)
    first = session.execute(select(func.count()).select_from(LayoutProposal)).scalar_one()
    stages.extract_pages(session, revision.id)

    assert session.execute(select(func.count()).select_from(LayoutProposal)).scalar_one() == first


def test_layout_proposals_still_do_not_supply_run_checks_discriminators(
    session: Session, store: LocalStore
) -> None:
    _publish_rulebook(session)
    revision = _revision(session, store)
    DatabaseStages(
        store, layout_readers=[_AnsweringReader()], missing_space=MISSING_SPACE
    ).extract_pages(session, revision.id)

    result = DatabaseStages(missing_space=MISSING_SPACE).run_checks(session, revision.id)

    assert result["ran"] is True
    assert _outcome_for(session, revision, "CT-WIDTH-001") == "REVIEW_REQUIRED"


# ---------------------------------------------------------------------------
# #1221: every layout call writes one `model_invocations` row
# ---------------------------------------------------------------------------

LAYOUT_MODEL = "test-layout-model"


@pytest.fixture
def rates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stated price for the test model: $1 per 1k input tokens, $2 per 1k output tokens."""
    path = tmp_path / "rates.json"
    path.write_text(
        json.dumps(
            {
                "source": "test prices, authored",
                "retrieved": "2026-10-14",
                "currency": "USD",
                "rates": {LAYOUT_MODEL: {"input_per_1k_tokens": "1", "output_per_1k_tokens": "2"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GV_MODEL_RATES_FILE", str(path))


class _AnsweringBedrock:
    """Answers every layout question with its first permitted choice, and counts the calls."""

    def __init__(self) -> None:
        self.calls = 0

    def converse(self, **request: Any) -> Mapping[str, Any]:
        self.calls += 1
        schema = request["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
        return {
            "stopReason": "tool_use",
            "usage": {"inputTokens": 1000, "outputTokens": 100},
            "output": {
                "message": {
                    "content": [
                        {
                            "toolUse": {
                                "name": TOOL_NAME,
                                "input": {
                                    "answer": schema["properties"]["answer"]["enum"][0],
                                    "reason": "the plan view visibly matches this choice",
                                    "region": [["0", "0"], ["1", "0"], ["1", "1"], ["0", "1"]],
                                },
                            }
                        }
                    ]
                }
            },
        }


class _MalformedBedrock(_AnsweringBedrock):
    def converse(self, **request: Any) -> Mapping[str, Any]:
        response = dict(super().converse(**request))
        response["output"] = {"message": {"content": [{"text": "no tool call"}]}}
        return response


class _RaisingBedrock:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    def converse(self, **_: Any) -> Mapping[str, Any]:
        self.calls += 1
        raise self.error


def _bedrock_reader(client: Any) -> BedrockClosedQuestionReader:
    return BedrockClosedQuestionReader(
        BedrockClosedQuestionConfig(
            model_id=LAYOUT_MODEL,
            prompt_id=LAYOUT_PROMPT_ID,
            template_id=LAYOUT_TEMPLATE_ID,
        ),
        client,
    )


def _layout_rows(session: Session) -> list[ModelInvocation]:
    return list(
        session.execute(
            select(ModelInvocation).where(ModelInvocation.prompt_id == LAYOUT_PROMPT_ID)
        ).scalars()
    )


def _layout_run_ids(session: Session) -> set[Any]:
    """The extraction run the layout crops were filed under: the page's own run."""
    return set(
        session.execute(
            select(ObservationCandidate.extraction_run_id).where(
                ObservationCandidate.raw_text.like("layout %")
            )
        ).scalars()
    )


def test_each_layout_call_writes_one_priced_row_under_the_pages_run(
    session: Session, store: LocalStore, rates: None
) -> None:
    """**Outcome: one `ok` row per call, with tokens, cost, identity and the page's run.**

    Before #1221 the layout classifier recorded nothing: its calls were missing from Usage, cost and
    every scorecard.
    """
    rules = _publish_rulebook(session)
    revision = _revision(session, store)
    client = _AnsweringBedrock()

    DatabaseStages(
        store, layout_readers=[_bedrock_reader(client)], missing_space=MISSING_SPACE
    ).extract_pages(session, revision.id)

    rows = _layout_rows(session)
    assert client.calls == len(required_inputs(rules).discriminators)
    assert len(rows) == client.calls
    assert {row.extraction_run_id for row in rows} == _layout_run_ids(session)
    assert len(_layout_run_ids(session)) == 1
    for row in rows:
        assert row.package_revision_id is None
        assert row.model_id == LAYOUT_MODEL
        assert row.template_id == LAYOUT_TEMPLATE_ID
        assert (row.input_tokens, row.output_tokens) == (1000, 100)
        assert row.cost_micros == 1_200_000
        assert row.outcome == "ok"
        assert row.rejection_reason is None
    # The proposals are what an answering reader always produced.
    choices = {choice for need in required_inputs(rules).discriminators for choice in need.choices}
    assert {proposal.proposed_value for proposal in _proposals(session)} <= choices


def test_a_malformed_layout_answer_writes_one_rejected_row_and_still_abstains(
    session: Session, store: LocalStore, rates: None
) -> None:
    _publish_rulebook(session)
    revision = _revision(session, store)
    client = _MalformedBedrock()

    DatabaseStages(
        store, layout_readers=[_bedrock_reader(client)], missing_space=MISSING_SPACE
    ).extract_pages(session, revision.id)

    rows = _layout_rows(session)
    assert len(rows) == client.calls > 0
    assert {row.outcome for row in rows} == {"rejected"}
    assert all(
        row.rejection_reason and row.rejection_reason.startswith("ValueError:") for row in rows
    )
    # The model answered and was paid, so its tokens count.
    assert {row.cost_micros for row in rows} == {1_200_000}
    assert {proposal.proposed_value for proposal in _proposals(session)} == {"abstained"}


def test_a_failed_layout_call_writes_one_failed_row_with_its_reason_and_still_abstains(
    session: Session, store: LocalStore, rates: None
) -> None:
    from botocore.exceptions import ClientError  # type: ignore[import-untyped]

    _publish_rulebook(session)
    revision = _revision(session, store)
    client = _RaisingBedrock(
        ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "Converse")
    )

    DatabaseStages(
        store, layout_readers=[_bedrock_reader(client)], missing_space=MISSING_SPACE
    ).extract_pages(session, revision.id)

    rows = _layout_rows(session)
    assert len(rows) == client.calls > 0
    assert {row.outcome for row in rows} == {"failed"}
    assert {row.rejection_reason for row in rows} == {"ClientError: ThrottlingException"}
    assert {(row.input_tokens, row.output_tokens, row.cost_micros) for row in rows} == {(0, 0, 0)}
    assert {proposal.proposed_value for proposal in _proposals(session)} == {"abstained"}


def test_a_timed_out_layout_call_writes_one_timeout_row_and_still_abstains(
    session: Session, store: LocalStore, rates: None
) -> None:
    from botocore.exceptions import ReadTimeoutError  # type: ignore[import-untyped]

    _publish_rulebook(session)
    revision = _revision(session, store)
    client = _RaisingBedrock(ReadTimeoutError(endpoint_url="https://bedrock.invalid"))

    DatabaseStages(
        store, layout_readers=[_bedrock_reader(client)], missing_space=MISSING_SPACE
    ).extract_pages(session, revision.id)

    rows = _layout_rows(session)
    assert len(rows) == client.calls > 0
    assert {row.outcome for row in rows} == {"timeout"}
    assert all(row.rejection_reason for row in rows)
    assert {proposal.proposed_value for proposal in _proposals(session)} == {"abstained"}


def test_an_injected_reader_that_calls_no_model_writes_no_row(
    session: Session, store: LocalStore
) -> None:
    """**Outcome: unchanged.** Only a real Bedrock call is a model call."""
    _publish_rulebook(session)
    revision = _revision(session, store)

    DatabaseStages(
        store, layout_readers=[_AnsweringReader()], missing_space=MISSING_SPACE
    ).extract_pages(session, revision.id)

    assert _layout_rows(session) == []


def test_usage_counts_the_layout_calls(session: Session, store: LocalStore, rates: None) -> None:
    """**Outcome: the Usage page's totals include the layout calls, priced, under their package.**"""
    from app.api.usage import project_usage

    _publish_rulebook(session)
    revision = _revision(session, store)
    client = _AnsweringBedrock()
    DatabaseStages(
        store, layout_readers=[_bedrock_reader(client)], missing_space=MISSING_SPACE
    ).extract_pages(session, revision.id)
    package = session.get(Package, revision.package_id)
    assert package is not None

    usage = project_usage(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        session,
        package.project_id,
        from_=None,
        to=None,
        group_by="package",
    )

    layout = next(model for model in usage.groups[0].models if model.model == LAYOUT_MODEL)
    assert layout.calls == client.calls
    assert layout.input_tokens == 1000 * client.calls
    assert layout.cost_usd == f"{1.2 * client.calls:.6f}"
    assert usage.groups[0].package_id == package.id


# ---------------------------------------------------------------------------
# #1221 review: recording a layout call must not change the AI reading budget
# ---------------------------------------------------------------------------

UNPRICED_LAYOUT_MODEL = "test-layout-model-without-a-price"


def _extract_task_run(session: Session, revision: PackageRevision) -> TaskRun:
    return session.execute(
        select(TaskRun).where(
            TaskRun.idempotency_key
            == stage_idempotency_key(
                package_revision_id=revision.id,
                stage="extract_pages",
                engine_version=ENGINE_VERSION,
            )
        )
    ).scalar_one()


def test_committed_layout_rows_leave_the_reading_budget_where_it_was(
    session: Session, store: LocalStore, rates: None
) -> None:
    """**Outcome: `stage_spend_so_far` and the room it gives the readers ignore layout rows.**

    "Read the unread pages" rebuilds the meter from the stage's committed rows. Counting a layout
    row there would shrink the readers' room (a priced row) or close it to $0 (an unpriced row, which
    `_remaining_claude_budget` treats as unknown spend). Phase 1 changes no behaviour, so layout and
    assignment rows stay out of the reading meter until the shared budget (Sutra step 3).
    """
    from decimal import Decimal
    from time import monotonic_ns

    from app.runs.invocations import BedrockConverseInvocationRecorder
    from workflow.stages import _remaining_claude_budget, stage_spend_so_far

    _publish_rulebook(session)
    revision = _revision(session, store)
    priced, unpriced = _AnsweringBedrock(), _AnsweringBedrock()
    unpriced_reader = BedrockClosedQuestionReader(
        BedrockClosedQuestionConfig(
            model_id=UNPRICED_LAYOUT_MODEL,
            prompt_id=LAYOUT_PROMPT_ID,
            template_id=LAYOUT_TEMPLATE_ID,
        ),
        unpriced,
    )
    DatabaseStages(
        store,
        layout_readers=[_bedrock_reader(priced), unpriced_reader],
        missing_space=MISSING_SPACE,
    ).extract_pages(session, revision.id)
    rows = _layout_rows(session)
    assert {row.cost_micros for row in rows} == {1_200_000, None}, "one priced, one unpriced"
    [run_id] = _layout_run_ids(session)
    # A reader's own call under the same run still counts, as before.
    BedrockConverseInvocationRecorder(session, None, extraction_run_id=run_id).record(
        model_id=LAYOUT_MODEL,
        prompt_id="claude-slot-span-v5",
        template_id="slot-span",
        started_ns=monotonic_ns(),
        response={"usage": {"inputTokens": 100, "outputTokens": 0}},
        error=None,
    )
    session.commit()

    spent, unpriced_calls = stage_spend_so_far(session, _extract_task_run(session, revision))

    assert (spent, unpriced_calls) == (100_000, 0), "only the reader's call is reading spend"
    room = _remaining_claude_budget(
        Decimal(5), cap_micros=3_000_000, spent_micros=spent, unpriced_calls=unpriced_calls
    )
    assert room == _remaining_claude_budget(
        Decimal(5), cap_micros=3_000_000, spent_micros=100_000, unpriced_calls=0
    )
    assert room == Decimal("2.9")


def test_a_pending_invalid_row_still_raises_its_own_error_in_the_stage(
    session: Session, store: LocalStore, rates: None
) -> None:
    """**Outcome: the layout recorder never swallows someone else's database error.**

    The recorder writes after `classify_layout` returns and flushes the session outside its own
    guard, so a refused write that is not its own surfaces exactly as before.
    """
    from sqlalchemy.exc import IntegrityError

    from workflow.model_call_recorder import ModelCallRecorder

    _publish_rulebook(session)
    revision = _revision(session, store)
    session.flush()
    recorder = ModelCallRecorder(session, package_revision_id=revision.id)
    recorder.record(
        model_id=LAYOUT_MODEL,
        prompt_id=LAYOUT_PROMPT_ID,
        template_id=LAYOUT_TEMPLATE_ID,
        started_ns=1,
        response={"usage": {"inputTokens": 1, "outputTokens": 1}},
        error=None,
    )
    # Someone else's pending row, refused by the table's own check (a blank model id).
    session.add(
        ModelInvocation(
            package_revision_id=revision.id,
            model_id="",
            prompt_id="someone-else",
            template_id="someone-else",
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
            outcome="ok",
        )
    )

    with pytest.raises(IntegrityError):
        recorder.write_pending()


@pytest.mark.parametrize("prompt_id", ["layout-discriminator-v2", "reading-assignment-v3"])
def test_a_later_version_of_an_unmetered_prompt_stays_out_of_the_reading_budget(
    session: Session, store: LocalStore, prompt_id: str
) -> None:
    """**Outcome: the exclusion follows the prompt family, not one version's exact id.**"""
    from time import monotonic_ns

    from app.runs.invocations import BedrockConverseInvocationRecorder
    from workflow.stages import stage_spend_so_far

    _publish_rulebook(session)
    revision = _revision(session, store)
    DatabaseStages(
        store, layout_readers=[_AnsweringReader()], missing_space=MISSING_SPACE
    ).extract_pages(session, revision.id)
    [run_id] = _layout_run_ids(session)
    BedrockConverseInvocationRecorder(session, None, extraction_run_id=run_id).record(
        model_id=LAYOUT_MODEL,
        prompt_id=prompt_id,
        template_id="made-up",
        started_ns=monotonic_ns(),
        response={"usage": {"inputTokens": 100, "outputTokens": 0}},
        error=None,
    )
    session.commit()

    assert stage_spend_so_far(session, _extract_task_run(session, revision)) == (0, 0)
