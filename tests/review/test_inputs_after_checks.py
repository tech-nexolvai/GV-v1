"""Sign-off waits for a re-run when a reviewer changed what the checks read (#1137).

The live results were computed from the inputs that existed when the checks ran. A typed measurement,
a project setting, a wall choice, a confirmed run or part, a reading confirmed after that leaves the
old results judging something the reviewer has since changed, so signing them off would certify
numbers nobody compared. Every input the check stage reads, recorded after the latest live check run,
blocks readiness (one revision and a page of them) and `approve_package`, until the checks run again.

Inputs the checks never read, and machine records, do not block.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from app.auth.roles import Principal, Role
from app.db.base import utc_now
from app.db.session import session_factory
from app.models import (
    ArchitectPairingRecord,
    CanonicalObservation,
    CheckRun,
    CountertopRunDecision,
    Document,
    DocumentKind,
    DocumentVersion,
    DrawingItem,
    DrawingView,
    ExtractionRun,
    Finding,
    LayoutConfirmation,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    Page,
    ParameterSet,
    PartConfirmation,
    PartDecision,
    PartProposal,
    Project,
    ReadingPart,
    ReviewAction,
    ReviewActionKind,
    RuleDefinition,
    RuleSnapshot,
    SlotRowReviewDecision,
    SourceArtifact,
    TaskRun,
    ViewRoleConfirmation,
    WorkflowRun,
)
from app.models.evidence import ArchitectViewMatchRecord, ItemClassification
from app.review.approval import (
    INPUTS_CHANGED_NEEDS_RERUN,
    UnaddressedReviewRequired,
    approval_readiness,
    approval_readiness_many,
    approve_package,
)
from app.review.session import open_session
from app.verdicts.record import supersede_runs
from evidence.canonical import Authority, EvidenceStatus
from tests.review.test_session import _upgrade
from units.measurement import Unit
from vocabulary.semantic_types import DocumentRole, SemanticType
from workflow.view_roles import CODE_CONFIRMER

pytest_plugins = ("tests.app.postgres_fixture",)

ACTOR = "reviewer@example.com"
HASH = "d" * 64
STORED_BOX: dict[str, object] = {
    "space": "stored",
    "points": [["0.10", "0.20"], ["0.30", "0.20"], ["0.30", "0.40"], ["0.10", "0.40"]],
}

#: When the checks ran: comfortably before anything the test records afterwards.
CHECKED_AT = utc_now() - timedelta(hours=3)
BEFORE_THE_RUN = CHECKED_AT - timedelta(hours=1)


def reviewer() -> Principal:
    return Principal(ACTOR, frozenset({Role.REVIEWER}))


@dataclass
class Sheet:
    """One revision with one vendor page, a drawing on it and a reading, already checked."""

    revision: PackageRevision
    project_id: UUID
    page: Page
    view: DrawingView
    reading: ObservationCandidate


def _digest() -> str:
    return f"sha256:{uuid4().hex}{uuid4().hex}"


def _checked(db: Session, revision: PackageRevision, *, at: datetime) -> Finding:
    """A live PASS from a check run made at `at`; nothing on it needs a reviewer decision."""
    definition = RuleDefinition(rule_id=f"CT-{uuid4().hex[:6]}")
    db.add(definition)
    db.flush()
    body = f'{{"id":"{definition.rule_id}"}}'
    snapshot = RuleSnapshot(
        rule_definition_id=definition.id,
        snapshot_id=f"sha256:{hashlib.sha256(body.encode()).hexdigest()}",
        version="1.0.0",
        canonical_json=body,
        product_type="countertop",
        check_type="internal",
        unconfirmed_tolerance_count=0,
    )
    db.add(snapshot)
    db.flush()
    run = CheckRun(
        package_revision_id=revision.id,
        rule_snapshot_id=snapshot.id,
        engine_version="verdict-1.2.3",
        created_at=at,
    )
    db.add(run)
    db.flush()
    finding = Finding(
        check_run_id=run.id,
        package_revision_id=revision.id,
        outcome="PASS",
        severity="CRITICAL",
        trace={},
        parameter_set_versions={},
        created_at=at,
    )
    db.add(finding)
    db.flush()
    return finding


def _rerun(db: Session, revision: PackageRevision) -> None:
    """What running the checks again does: supersede the live run, write a newer one."""
    supersede_runs(db, revision.id)
    _checked(db, revision, at=utc_now() + timedelta(minutes=1))
    db.flush()


def _sheet(db: Session) -> Sheet:
    project = Project(name=f"inputs after checks {uuid4()}")
    db.add(project)
    db.flush()
    package = Package(project_id=project.id, vendor=None)
    db.add(package)
    db.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.AWAITING_REVIEW.value
    )
    db.add(revision)
    db.flush()
    artifact = SourceArtifact(
        storage_key=f"originals/{project.id}/shop.pdf", sha256=HASH, size=1, backend_version_id=None
    )
    document = Document(package_id=package.id, kind=DocumentKind.SHOP)
    db.add_all((artifact, document))
    db.flush()
    version = DocumentVersion(
        document_id=document.id, source_artifact_id=artifact.id, sha256=HASH, page_count=1
    )
    db.add(version)
    db.flush()
    db.add(
        PackageRevisionDocument(
            package_revision_id=revision.id,
            package_id=package.id,
            document_id=document.id,
            document_version_id=version.id,
        )
    )
    page = Page(
        document_version_id=version.id,
        index=0,
        content_hash=HASH,
        width_pt=612,
        height_pt=792,
        rotation=0,
        has_vector_text=True,
        render_failed=False,
        sheet_number="X-1",
        page_type=None,
        revision_label=None,
    )
    db.add(page)
    db.flush()
    view = DrawingView(
        page_id=page.id, tag="D", region={"space": "pdf_points", "polygon": [0, 0, 100, 100]}
    )
    db.add(view)
    workflow = WorkflowRun(package_revision_id=revision.id, engine_run_id=f"run-{uuid4()}")
    db.add(workflow)
    db.flush()
    task = TaskRun(
        workflow_run_id=workflow.id,
        idempotency_key=f"extract-{uuid4()}",
        task_type="extract_page",
        attempt=1,
        outcome="ok",
    )
    db.add(task)
    db.flush()
    extraction = ExtractionRun(
        task_run_id=task.id, extractor="pdfplumber", extractor_version="1.0", config_hash="c"
    )
    db.add(extraction)
    db.flush()
    reading = ObservationCandidate(
        document_version_id=version.id,
        page_id=page.id,
        extraction_run_id=extraction.id,
        raw_text='12 3/4"',
        polygon=[[10, 10], [20, 10], [20, 20]],
        coordinate_space="image",
        confidence=None,
        ambiguity_flags=[],
        created_at=BEFORE_THE_RUN,
    )
    db.add(reading)
    db.flush()
    _checked(db, revision, at=CHECKED_AT)
    return Sheet(revision, project.id, page, view, reading)


def _observation(db: Session, sheet: Sheet, *, at: datetime | None = None) -> CanonicalObservation:
    value = Fraction(51, 4)
    observation = CanonicalObservation(
        document_version_id=sheet.page.document_version_id,
        page_id=sheet.page.id,
        document_role=DocumentRole.SHOP,
        polygon=[["0.1", "0.1"], ["0.2", "0.1"], ["0.2", "0.2"]],
        coordinate_space="stored",
        semantic_type=SemanticType.CABINET_WIDTH,
        value_numerator=value.numerator,
        value_denominator=value.denominator,
        unit=Unit.INCH,
        status=EvidenceStatus.HUMAN_CONFIRMED,
        authority=Authority.AUTHORITATIVE,
        evidence_crop_uri=None,
        **({} if at is None else {"created_at": at}),
    )
    db.add(observation)
    db.flush()
    return observation


def _item(db: Session, sheet: Sheet, *, at: datetime | None = None) -> DrawingItem:
    item = DrawingItem(
        drawing_view_id=sheet.view.id,
        item_type="CT001",
        extent={"space": "pdf_points", "polygon": [0, 0, 100, 100]},
        **({} if at is None else {"created_at": at}),
    )
    db.add(item)
    db.flush()
    return item


def _at(at: datetime | None) -> dict[str, datetime]:
    return {} if at is None else {"created_at": at}


# -- one recorder per input the check stage reads --------------------------------------------------


def _typed_measurement(db: Session, sheet: Sheet, at: datetime | None) -> None:
    """A RUN parameter set: the reviewer's typed measurements and settings for this review."""
    db.add(
        ParameterSet(
            set_id=_digest(),
            project_id=sheet.project_id,
            layer="run",
            version=1,
            package_revision_id=sheet.revision.id,
            **_at(at),
        )
    )


def _project_setting(db: Session, sheet: Sheet, at: datetime | None) -> None:
    """A PROJECT parameter set for this revision's own project."""
    db.add(
        ParameterSet(
            set_id=_digest(), project_id=sheet.project_id, layer="project", version=1, **_at(at)
        )
    )


def _slot_row_decision(db: Session, sheet: Sheet, at: datetime | None) -> None:
    """A wall choice and typed widths saved against one slot-reader row."""
    db.add(
        SlotRowReviewDecision(
            row_candidate_id=sheet.reading.id,
            wall_config="back_only",
            measurements={},
            confirmed_by=ACTOR,
            **_at(at),
        )
    )


def _countertop_run_decision(db: Session, sheet: Sheet, at: datetime | None) -> None:
    """A person's decision on the run beneath a countertop (a withdrawal changes it too)."""
    countertop = _item(db, sheet, at=BEFORE_THE_RUN)
    db.add(
        CountertopRunDecision(
            countertop_item_id=countertop.id,
            decision=PartDecision.WITHDRAWN.value,
            run_id=None,
            confirmed_by=ACTOR,
            **_at(at),
        )
    )


def _part_decision(db: Session, sheet: Sheet, at: datetime | None) -> None:
    """A person's decision on a suggested part."""
    proposal = PartProposal(
        drawing_view_id=sheet.view.id,
        kind="cabinet",
        extent=STORED_BOX,
        source="dimension-segments",
        source_version="v1",
        reason="the segment between two extension lines",
        created_at=BEFORE_THE_RUN,
    )
    db.add(proposal)
    db.flush()
    db.add(
        PartConfirmation(
            part_proposal_id=proposal.id,
            decision=PartDecision.WITHDRAWN.value,
            confirmed_by=ACTOR,
            **_at(at),
        )
    )


def _reading_part_link(db: Session, sheet: Sheet, at: datetime | None) -> None:
    """A person saying which part a reading measures."""
    observation = _observation(db, sheet, at=BEFORE_THE_RUN)
    item = _item(db, sheet, at=BEFORE_THE_RUN)
    db.add(
        ReadingPart(
            canonical_observation_id=observation.id,
            drawing_item_id=item.id,
            signal="the reading sits on the part's dimension line",
            confirmed_by=ACTOR,
            **_at(at),
        )
    )


def _view_role(db: Session, sheet: Sheet, at: datetime | None) -> None:
    """A person saying which drawing a view is."""
    db.add(
        ViewRoleConfirmation(
            drawing_view_id=sheet.view.id, role="shop", confirmed_by=ACTOR, **_at(at)
        )
    )


def _classification(db: Session, sheet: Sheet, at: datetime | None) -> None:
    """A reviewer's category for one item in a run."""
    db.add(
        ItemClassification(
            package_revision_id=sheet.revision.id,
            rule_id="CAB-TEST-001",
            input_name="cabinet_type",
            position=0,
            category="base",
            confirmed_by=ACTOR,
            **_at(at),
        )
    )


def _layout(db: Session, sheet: Sheet, at: datetime | None) -> None:
    """A reviewer-confirmed layout answer."""
    db.add(
        LayoutConfirmation(
            package_revision_id=sheet.revision.id,
            discriminator_name="wall_config",
            value="back_only",
            confirmed_by=ACTOR,
            **_at(at),
        )
    )


def _reading_confirmation(db: Session, sheet: Sheet, at: datetime | None) -> None:
    """A reviewer naming what a reading is (`confirm_candidate`): HUMAN_CONFIRMED evidence."""
    _observation(db, sheet, at=at)


def _architect_pairing(db: Session, sheet: Sheet, at: datetime | None) -> None:
    """A reviewer's architect pairing (#1088), still covered by the general rule."""
    db.add(
        ArchitectPairingRecord(
            package_revision_id=sheet.revision.id,
            page_id=sheet.page.id,
            row_anchor_candidate_id=sheet.reading.id,
            pairs=[],
            details={},
            source="reviewer",
            status="reviewer",
            decided_by=ACTOR,
            **_at(at),
        )
    )


def _match(sheet: Sheet, **values: object) -> ArchitectViewMatchRecord:
    return ArchitectViewMatchRecord(
        package_revision_id=sheet.revision.id,
        vendor_page_id=sheet.page.id,
        row_anchor_candidate_id=sheet.reading.id,
        ai_picks=[],
        candidates=[],
        vendor_references=[],
        reasons=[],
        details={},
        **values,
    )


def _architect_view_match(db: Session, sheet: Sheet, at: datetime | None) -> None:
    """A reviewer's choice of the architect's view for a countertop (#1166): the next run reads it."""
    db.add(_match(sheet, source="reviewer", status="none_matches", decided_by=ACTOR, **_at(at)))


READ_BY_THE_CHECKS: dict[str, Callable[[Session, Sheet, datetime | None], None]] = {
    "typed-measurement": _typed_measurement,
    "project-setting": _project_setting,
    "slot-row-decision": _slot_row_decision,
    "countertop-run-decision": _countertop_run_decision,
    "part-decision": _part_decision,
    "reading-part-link": _reading_part_link,
    "view-role": _view_role,
    "classification": _classification,
    "layout": _layout,
    "reading-confirmation": _reading_confirmation,
    "architect-pairing": _architect_pairing,
    "architect-view-match": _architect_view_match,
}


@pytest.mark.parametrize("record", list(READ_BY_THE_CHECKS.values()), ids=list(READ_BY_THE_CHECKS))
def test_an_input_recorded_after_the_checks_blocks_sign_off_until_a_rerun(
    postgres_engine: Engine, record: Callable[[Session, Sheet, datetime | None], None]
) -> None:
    _upgrade(postgres_engine)
    with session_factory(postgres_engine).begin() as db:
        sheet = _sheet(db)
        assert approval_readiness(db, sheet.revision.id).can_approve

        record(db, sheet, None)
        db.flush()

        single = approval_readiness(db, sheet.revision.id)
        assert not single.can_approve
        assert single.reason == INPUTS_CHANGED_NEEDS_RERUN
        assert single.blocking_findings == 0
        batched = approval_readiness_many(db, [sheet.revision.id])[sheet.revision.id]
        assert (batched.can_approve, batched.reason) == (False, INPUTS_CHANGED_NEEDS_RERUN)
        review = open_session(db, package_revision_id=sheet.revision.id, reviewer=ACTOR)
        with pytest.raises(UnaddressedReviewRequired, match="Run the checks before signing off"):
            approve_package(db, principal=reviewer(), review_session_id=review.id)

        _rerun(db, sheet.revision)

        assert approval_readiness(db, sheet.revision.id).can_approve
        assert approval_readiness_many(db, [sheet.revision.id])[sheet.revision.id].can_approve


@pytest.mark.parametrize("record", list(READ_BY_THE_CHECKS.values()), ids=list(READ_BY_THE_CHECKS))
def test_an_input_recorded_before_the_checks_does_not_block(
    postgres_engine: Engine, record: Callable[[Session, Sheet, datetime | None], None]
) -> None:
    _upgrade(postgres_engine)
    with session_factory(postgres_engine).begin() as db:
        sheet = _sheet(db)
        record(db, sheet, BEFORE_THE_RUN)
        db.flush()

        assert approval_readiness(db, sheet.revision.id).can_approve
        assert approval_readiness_many(db, [sheet.revision.id])[sheet.revision.id].can_approve


def test_another_projects_setting_and_a_global_default_do_not_block(
    postgres_engine: Engine,
) -> None:
    """A PROJECT set is project-wide: only this revision's project's sets count. GLOBAL is the
    company layer, not a reviewer's input for this review."""
    _upgrade(postgres_engine)
    with session_factory(postgres_engine).begin() as db:
        sheet = _sheet(db)
        other = _sheet(db)
        _project_setting(db, other, None)
        # Unique per (project, layer, version); GLOBAL has no project, so take a fresh version.
        db.add(ParameterSet(set_id=_digest(), project_id=None, layer="global", version=900_000))
        db.flush()

        assert approval_readiness(db, sheet.revision.id).can_approve
        assert not approval_readiness(db, other.revision.id).can_approve


def test_machine_records_do_not_block(postgres_engine: Engine) -> None:
    """Code confirming a drawing's role, an automatic architect pairing, an automatic architect
    view match and an evidence action's own copy of a reading are not unchecked reviewer input."""
    _upgrade(postgres_engine)
    with session_factory(postgres_engine).begin() as db:
        sheet = _sheet(db)
        db.add(
            ViewRoleConfirmation(
                drawing_view_id=sheet.view.id, role="shop", confirmed_by=CODE_CONFIRMER
            )
        )
        db.add(
            ArchitectPairingRecord(
                package_revision_id=sheet.revision.id,
                page_id=sheet.page.id,
                row_anchor_candidate_id=sheet.reading.id,
                extraction_run_id=sheet.reading.extraction_run_id,
                pairs=[],
                details={},
                source="code+ais",
                status="paired",
                decided_by=None,
            )
        )
        db.add(
            _match(
                sheet,
                source="automatic",
                status="needs_reviewer",
                extraction_run_id=sheet.reading.extraction_run_id,
            )
        )
        db.flush()

        assert approval_readiness(db, sheet.revision.id).can_approve


def test_an_evidence_action_is_not_counted_again(postgres_engine: Engine) -> None:
    """A confirm on a finding's evidence writes a HUMAN_CONFIRMED copy of the same value. It is a
    reviewer decision on that finding, governed by the decision and correction rules, never a new
    reading the old results missed."""
    _upgrade(postgres_engine)
    with session_factory(postgres_engine).begin() as db:
        sheet = _sheet(db)
        finding = _checked(db, sheet.revision, at=CHECKED_AT)
        original = _observation(db, sheet, at=BEFORE_THE_RUN)
        copy = _observation(db, sheet)
        review = open_session(db, package_revision_id=sheet.revision.id, reviewer=ACTOR)
        db.add(
            ReviewAction(
                review_session_id=review.id,
                finding_id=finding.id,
                package_revision_id=sheet.revision.id,
                action=ReviewActionKind.CONFIRM.value,
                actor=ACTOR,
                original_observation_id=original.id,
                resulting_observation_id=copy.id,
            )
        )
        db.flush()

        assert approval_readiness(db, sheet.revision.id).can_approve


def test_the_input_rule_costs_one_statement_whatever_the_number_of_revisions(
    postgres_engine: Engine,
) -> None:
    """The Documents summary reads a page of revisions; the rule must not cost a query each."""
    _upgrade(postgres_engine)
    with session_factory(postgres_engine).begin() as db:
        sheets = [_sheet(db) for _ in range(4)]
        for sheet, record in zip(sheets, READ_BY_THE_CHECKS.values(), strict=False):
            record(db, sheet, None)
        db.flush()
        bind = db.get_bind()

        def statements(revision_ids: list[UUID]) -> int:
            count = 0

            def counted(*_args: object) -> None:
                nonlocal count
                count += 1

            event.listen(bind, "before_cursor_execute", counted)
            try:
                result = approval_readiness_many(db, revision_ids)
            finally:
                event.remove(bind, "before_cursor_execute", counted)
            assert all(not entry.can_approve for entry in result.values())
            return count

        assert statements([sheets[0].revision.id]) == statements(
            [sheet.revision.id for sheet in sheets]
        )
