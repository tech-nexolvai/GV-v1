"""Reviewer decisions for a slot-reader row never carry to another row."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.slot_rows import (
    TYPED_WIDTH_CLOSED,
    SlotRowReviewIn,
    list_slot_rows,
    review_slot_row,
)
from app.auth.roles import Principal, Role
from app.db.session import session_factory
from app.evidence.confirm import observation_from_candidate
from app.models import (
    CanonicalObservation,
    Document,
    DocumentVersion,
    EvidenceSupportingCandidate,
    MeasurementProposal,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    Project,
    SlotRowReviewDecision,
    SourceArtifact,
)
from app.models.document import DocumentKind, Page
from app.models.evidence import EvidenceCorroborationLane
from app.models.runs import REUSED_FROM_KEY, ExtractionRun, ModelInvocation, TaskRun, WorkflowRun
from app.models.verdicts import CheckRun, Finding, VerdictInput
from evidence.canonical import Authority, CorroborationLane, EvidenceStatus
from evidence.corroborate import independence_key
from evidence.gate import GateRefusal, seal
from extraction.slot_reader.bedrock import (
    CLAUDE_SPAN_PROMPT_ID,
    COUNTER_BREAK_PROMPT_ID,
    CROP_PROMPT_ID,
    ROW_PROMPT_ID,
    crop_prompt_id,
)
from extraction.slot_reader.labels import plain_dimension
from extraction.slot_reader.walls import WALL_PROMPT_ID
from rules.semantic_types import SemanticType
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_stages import _publish_rulebook
from vocabulary.semantic_types import ProductType
from vocabulary.wall_layouts import VENDOR_WALLS_PASS_REASON
from workflow.row_answers import slot_position, width_semantic
from workflow.slot_row_evidence import _canonical_for_candidate, _domain_observation
from workflow.stages import LAYOUT_PROMPT_ID, DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _without_removed(flags: list[str]) -> list[str]:
    """`flags` without each `-name` and without every `name` a `-name` removes."""
    removed = {flag[1:] for flag in flags if flag.startswith("-")}
    return [flag for flag in flags if not flag.startswith("-") and flag not in removed]


def _package_rows(
    session: Session,
    *,
    unsealed_page: int | None = None,
    unsealed_all: bool = False,
    wall_source: str = "readers",
    held_page: int | None = None,
    check_hold_page: int | None = None,
    check_hold_all: bool = False,
    extra_hold: str | None = None,
    overall_override: int | None = None,
    piece_count: int = 1,
    widths_add_up: bool = False,
    rows_per_page: int = 1,
    slot_flags: Callable[[int, int, str], list[str]] | None = None,
    row_pages: tuple[int, ...] = (0, 1),
    sealed_layout: str = "back_left_right",
    field_cuts: int = 2,
    walls_confirmed: bool | tuple[int, ...] = False,
    unsealed_proposal: bool = True,
) -> tuple[UUID, UUID, dict[int, UUID]]:
    """`slot_flags(page_index, row_offset, slot)` adds flags (e.g. a `slot-box:`) to a slot; a
    flag written `-name` removes `name` instead (e.g. `-ink:vendor`).

    `row_pages` are the page indexes (of 0 and 1) that get rows; `()` makes a run with none.
    `unsealed_proposal=False` leaves the unsealed slot with no form proposal, as the Claude-mode
    reader stores a label it held (#1339)."""
    _publish_rulebook(session)
    project = Project(name="row-scoped API tests")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Synthetic vendor", product_type="countertop")
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state="NEEDS_INPUT")
    source = SourceArtifact(storage_key=f"synthetic/{uuid4()}", sha256="a" * 64, size=1)
    document = Document(package_id=package.id, kind=DocumentKind.SHOP.value)
    session.add_all((revision, source, document))
    session.flush()
    version = DocumentVersion(
        document_id=document.id, source_artifact_id=source.id, sha256="a" * 64, page_count=2
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
    pages: dict[int, Page] = {}
    for index in (0, 1):
        page = Page(
            document_version_id=version.id,
            index=index,
            content_hash=f"{index:064x}",
            width_pt=Decimal(612),
            height_pt=Decimal(792),
            rotation=0,
            has_vector_text=True,
            media_box=["0", "0", "612", "792"],
            crop_box=["0", "0", "612", "792"],
        )
        session.add(page)
        pages[index] = page
    workflow_run = WorkflowRun(package_revision_id=revision.id, engine_run_id=str(uuid4()))
    session.add(workflow_run)
    session.flush()
    task = TaskRun(
        workflow_run_id=workflow_run.id,
        idempotency_key=str(uuid4()),
        task_type="extract",
        attempt=1,
        outcome="SUCCEEDED",
    )
    session.add(task)
    session.flush()
    extraction = ExtractionRun(
        task_run_id=task.id,
        extractor="extraction.form_reader",
        extractor_version="slot-reader-v1-test",
        config_hash="synthetic-test",
        dpi=150,
    )
    session.add(extraction)
    session.flush()

    anchors: dict[int, UUID] = {}
    for page_index in row_pages:
        for row_offset in range(rows_per_page):
            rank = str(row_offset * 2 + page_index + 1)
            slots = [
                (str(position), "SHOP:countertop_piece_width", 20 + page_index + position)
                for position in range(piece_count)
            ]
            # The published company standard adds one inch at each wall end (two by default).
            overall = (
                sum(value for _, _, value in slots) + field_cuts
                if widths_add_up
                else 40 + page_index
            )
            if page_index == 0 and row_offset == 0 and overall_override is not None:
                overall = overall_override
            slots.append(("overall", "SHOP:countertop_overall_width", overall))
            for slot, field, numerator in slots:
                candidate = ObservationCandidate(
                    document_version_id=version.id,
                    page_id=pages[page_index].id,
                    extraction_run_id=extraction.id,
                    raw_text=f"{numerator} inch",
                    value_numerator=numerator,
                    value_denominator=1,
                    unit="in",
                    polygon=[[0, 0], [10, 0], [10, 10], [0, 10]],
                    coordinate_space="image",
                    ambiguity_flags=_without_removed(
                        [
                            "slot-reader",
                            f"slot:{slot}",
                            f"row-rank:{rank}",
                            f"row-slot-count:{piece_count}",
                            "ink:vendor",
                            *(["row-hold:synthetic held row"] if page_index == held_page else []),
                            *(
                                ["check-hold:stone-short-of-ends"]
                                if check_hold_all or page_index == check_hold_page
                                else []
                            ),
                            *(
                                [extra_hold]
                                if page_index == 0 and row_offset == 0 and extra_hold is not None
                                else []
                            ),
                            *(
                                ()
                                if slot_flags is None
                                else slot_flags(page_index, row_offset, slot)
                            ),
                        ]
                    ),
                    review_reason="synthetic held row" if page_index == held_page else None,
                    corroboration_status=(
                        None
                        if unsealed_all or (page_index == unsealed_page and slot == "0")
                        else "CORROBORATED"
                    ),
                    corroboration_lane=(
                        None
                        if unsealed_all or (page_index == unsealed_page and slot == "0")
                        else "SECOND_READER"
                    ),
                )
                session.add(candidate)
                session.flush()
                if slot == "0":
                    anchors.setdefault(page_index, candidate.id)
                unsealed = unsealed_all or (page_index == unsealed_page and slot == "0")
                if unsealed and not unsealed_proposal:
                    continue
                session.add(
                    MeasurementProposal(
                        package_revision_id=revision.id,
                        page_number=page_index + 1,
                        proposal_id=uuid4(),
                        field_key=field,
                        position=0 if slot == "overall" else int(slot),
                        candidate_id=candidate.id,
                        placement_verified=True,
                        model_id="synthetic-reader-pair",
                        prompt_id="synthetic-slot-test",
                    )
                )
            wall = ObservationCandidate(
                document_version_id=version.id,
                page_id=pages[page_index].id,
                extraction_run_id=extraction.id,
                raw_text=f"walls: {sealed_layout}",
                polygon=[[0, 0], [10, 0], [10, 10], [0, 10]],
                coordinate_space="image",
                ambiguity_flags=[
                    "wall-reader",
                    f"row-rank:{rank}",
                    f"walls-sealed:{sealed_layout}",
                    f"wall-source:{wall_source}",
                    *(["row-hold:synthetic held row"] if page_index == held_page else []),
                ],
                review_reason="synthetic held row" if page_index == held_page else None,
            )
            session.add(wall)
    session.flush()
    if walls_confirmed:
        # The reviewer chose each row's walls: a PASS on them needs no further click (#1202).
        pages = (0, 1) if walls_confirmed is True else walls_confirmed
        for page_index, anchor in anchors.items():
            if page_index not in pages:
                continue
            session.add(
                SlotRowReviewDecision(
                    id=uuid4(),
                    row_candidate_id=anchor,
                    supersedes_id=None,
                    wall_config=sealed_layout,
                    measurements={},
                    confirmed_by="synthetic reviewer",
                )
            )
        session.flush()
    return project.id, package.id, anchors


def test_confirming_an_ai_wall_is_required_and_only_attaches_to_that_row(
    session: Session,
) -> None:
    project_id, package_id, anchors = _package_rows(session)
    principal = Principal(
        id="anant (synthetic test)",
        roles=frozenset({Role.REVIEWER}),
        projects=frozenset({project_id}),
    )

    listed = list_slot_rows(principal, session, project_id, package_id)
    by_page = {row.page_number: row for row in listed.rows}
    assert by_page[1].wall_proposal == "back_left_right"
    assert by_page[1].wall_source == "readers"
    assert by_page[1].decision_id is None

    with pytest.raises(HTTPException) as refused:
        review_slot_row(
            principal,
            principal,
            session,
            project_id,
            package_id,
            anchors[0],
            SlotRowReviewIn(),
        )
    assert refused.value.status_code == 422

    saved = review_slot_row(
        principal,
        principal,
        session,
        project_id,
        package_id,
        anchors[0],
        SlotRowReviewIn(wall_config="back_left_right"),
    )
    assert saved.wall_config == "back_left_right"
    assert saved.confirmed_by == "anant (synthetic test)"
    assert session.query(SlotRowReviewDecision).count() == 1


def test_a_typed_missing_width_is_refused_and_a_wall_is_saved_only_on_its_row(
    session: Session,
) -> None:
    """#1339 (confirm-only): the AI's suggestion for a held width is shown, a typed width is
    refused, and a wall choice is saved on the selected row only."""
    project_id, package_id, anchors = _package_rows(session, unsealed_page=0)
    principal = Principal(
        id="anant (synthetic test)",
        roles=frozenset({Role.REVIEWER}),
        projects=frozenset({project_id}),
    )
    first_page = next(
        row
        for row in list_slot_rows(principal, session, project_id, package_id).rows
        if row.page_number == 1
    )
    piece = next(value for value in first_page.values if value.key == "piece_widths:0")
    assert piece.needs_value
    assert piece.suggestion is not None

    with pytest.raises(HTTPException) as refused:
        review_slot_row(
            principal,
            principal,
            session,
            project_id,
            package_id,
            anchors[0],
            SlotRowReviewIn(
                wall_config="back_left_right",
                measurements={"piece_widths:0": "21 in"},
            ),
        )
    assert refused.value.status_code == 422
    assert refused.value.detail == TYPED_WIDTH_CLOSED
    assert session.query(SlotRowReviewDecision).count() == 0

    review_slot_row(
        principal,
        principal,
        session,
        project_id,
        package_id,
        anchors[0],
        SlotRowReviewIn(wall_config="back_left_right"),
    )
    refreshed = list_slot_rows(principal, session, project_id, package_id)
    by_page = {row.page_number: row for row in refreshed.rows}
    assert by_page[1].confirmed_by == "anant (synthetic test)"
    assert by_page[1].wall_config == "back_left_right"
    assert by_page[2].decision_id is None
    assert by_page[2].wall_config is None
    assert session.query(SlotRowReviewDecision).count() == 1


def test_a_reviewer_corrects_a_wall_choice_with_a_new_immutable_snapshot(
    session: Session,
) -> None:
    project_id, package_id, anchors = _package_rows(session, unsealed_page=0)
    principal = Principal(
        id="anant (synthetic test)",
        roles=frozenset({Role.REVIEWER}),
        projects=frozenset({project_id}),
    )
    first = review_slot_row(
        principal,
        principal,
        session,
        project_id,
        package_id,
        anchors[0],
        SlotRowReviewIn(wall_config="back_left_right"),
    )
    prior_id = first.decision_id

    corrected = review_slot_row(
        principal,
        principal,
        session,
        project_id,
        package_id,
        anchors[0],
        SlotRowReviewIn(wall_config="back_only"),
    )

    assert corrected.wall_config == "back_only"
    assert corrected.decision_id != prior_id
    decisions = (
        session.query(SlotRowReviewDecision).order_by(SlotRowReviewDecision.created_at).all()
    )
    assert len(decisions) == 2
    assert decisions[-1].supersedes_id == prior_id
    assert all(decision.measurements == {} for decision in decisions)


def _row_holds(session: Session, revision_id: UUID) -> set[UUID]:
    """Each countertop row's own live depth and sink results (#1201): on a package with several
    countertops every row has a held result of each of the six checks. Their own tests are in
    `tests/workflow/test_one_result_per_countertop.py`; the width tests here leave them out."""
    from app.models.rules import RuleDefinition, RuleSnapshot
    from workflow.several_countertops import WHOLE_PACKAGE_RULE_IDS

    return set(
        session.scalars(
            select(Finding.id)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
            .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
            .where(
                Finding.package_revision_id == revision_id,
                Finding.scope_row_candidate_id.is_not(None),
                CheckRun.superseded_at.is_(None),
                RuleDefinition.rule_id.in_(WHOLE_PACKAGE_RULE_IDS),
            )
        )
    )


def _run_current_checks(session: Session, package_id: UUID, root: Path) -> list[Finding]:
    """The rows' own width and architect results after a check run. Since #1201 a row also has
    its own held result of each depth and sink check on a package with several countertops; those
    are left out here (`tests/workflow/test_one_result_per_countertop.py` checks them)."""
    from app.models.rules import RuleDefinition, RuleSnapshot
    from workflow.several_countertops import WHOLE_PACKAGE_RULE_IDS

    revision = session.query(PackageRevision).filter_by(package_id=package_id).one()
    DatabaseStages(
        store=LocalStore(root=root / "store", ticket_secret=b"synthetic-test")
    ).run_checks(session, revision.id)
    return list(
        session.execute(
            select(Finding)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
            .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
            .where(
                Finding.package_revision_id == revision.id,
                Finding.scope_row_candidate_id.is_not(None),
                CheckRun.superseded_at.is_(None),
                RuleDefinition.rule_id.not_in(WHOLE_PACKAGE_RULE_IDS),
            )
        ).scalars()
    )


def _confirm_row_readings(
    session: Session, row_id: UUID, *, actor: str = "anant (synthetic test)"
) -> list[CanonicalObservation]:
    """A person confirming the AI's own reading of every label the readers held on this row, as
    `app/review/evidence_actions.confirm_candidate_reading` writes it (#1339). Widths are never
    typed for a row any more (confirm-only)."""
    anchor = session.get_one(ObservationCandidate, row_id)
    rank = next(flag for flag in anchor.ambiguity_flags if flag.startswith("row-rank:"))
    confirmed: list[CanonicalObservation] = []
    for candidate in session.scalars(
        select(ObservationCandidate)
        .where(
            ObservationCandidate.extraction_run_id == anchor.extraction_run_id,
            ObservationCandidate.page_id == anchor.page_id,
        )
        .order_by(ObservationCandidate.created_at, ObservationCandidate.id)
    ).all():
        flags = candidate.ambiguity_flags or []
        is_slot, position = slot_position(candidate)
        if (
            not is_slot
            or rank not in flags
            or "slot-reader" not in flags
            or candidate.corroboration_status == "CORROBORATED"
        ):
            continue
        made = observation_from_candidate(
            session,
            candidate,
            semantic_type=width_semantic(position).value,
            status=EvidenceStatus.HUMAN_CONFIRMED,
            actor=actor,
        )
        assert isinstance(made, CanonicalObservation), made
        confirmed.append(made)
    session.flush()
    return confirmed


def _save_all_row_widths(
    session: Session,
    project_id: UUID,
    package_id: UUID,
    row_id: UUID,
    *,
    wall_config: str | None = None,
) -> None:
    """Every held width of the row confirmed by a person from the AI's own reading (#1339), and the
    walls chosen when `wall_config` is given. Before #1339 this typed the widths."""
    _confirm_row_readings(session, row_id)
    if wall_config is None:
        return
    principal = Principal(
        id="anant (synthetic test)",
        roles=frozenset({Role.REVIEWER}),
        projects=frozenset({project_id}),
    )
    review_slot_row(
        principal,
        principal,
        session,
        project_id,
        package_id,
        row_id,
        SlotRowReviewIn(wall_config=wall_config),
    )


def test_end_to_end_drawing_clue_row_fails_with_its_own_scope(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id, anchors = _package_rows(
        session, unsealed_all=True, wall_source="code-ink-and-both-ais"
    )
    _save_all_row_widths(session, project_id, package_id, anchors[0])
    rows = list_slot_rows(
        Principal(
            id="anant (synthetic test)",
            roles=frozenset({Role.REVIEWER}),
            projects=frozenset({project_id}),
        ),
        session,
        project_id,
        package_id,
    ).rows
    assert rows[0].wall_source == "code-ink-and-both-ais"

    findings = _run_current_checks(session, package_id, tmp_path)
    first_row = next(
        finding for finding in findings if finding.scope_row_candidate_id == anchors[0]
    )
    assert first_row.outcome == "FAIL", first_row.reason
    assert first_row.scope_label == "Countertop row on page 1"
    assert first_row.scope_item_id is None


def test_end_to_end_reader_wall_is_review_until_confirmed_for_that_row(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id, anchors = _package_rows(
        session, unsealed_all=True, wall_source="readers"
    )
    _save_all_row_widths(session, project_id, package_id, anchors[0])
    first = _run_current_checks(session, package_id, tmp_path)
    row_finding = next(finding for finding in first if finding.scope_row_candidate_id == anchors[0])
    # #1202: these widths are wrong whatever the walls, so the walls are not needed for a FAIL.
    assert row_finding.outcome == "FAIL"
    assert (row_finding.reason or "").startswith("Wrong whatever the walls")
    assert row_finding.variant is None

    principal = Principal(
        id="anant (synthetic test)",
        roles=frozenset({Role.REVIEWER}),
        projects=frozenset({project_id}),
    )
    review_slot_row(
        principal,
        principal,
        session,
        project_id,
        package_id,
        anchors[0],
        SlotRowReviewIn(wall_config="back_left_right"),
    )
    second = _run_current_checks(session, package_id, tmp_path)
    row_finding = next(
        finding for finding in second if finding.scope_row_candidate_id == anchors[0]
    )
    assert row_finding.outcome == "FAIL"


def test_end_to_end_held_row_never_runs_arithmetic(session: Session, tmp_path: Path) -> None:
    _project_id, package_id, anchors = _package_rows(
        session, unsealed_all=True, wall_source="code-ink-and-both-ais", held_page=0
    )
    findings = _run_current_checks(session, package_id, tmp_path)
    held = next(finding for finding in findings if finding.scope_row_candidate_id == anchors[0])
    assert held.outcome == "REVIEW_REQUIRED"
    assert "held" in (held.reason or "").lower()


@pytest.mark.parametrize(
    "wall_source",
    ["code-ink-and-both-ais", "readers", "vendor-drawing-clues", "drawing-and-readers"],
)
def test_end_to_end_sealed_row_passes_only_with_its_own_qualified_walls(
    session: Session, tmp_path: Path, wall_source: str
) -> None:
    project_id, package_id, anchors = _package_rows(
        session, piece_count=2, widths_add_up=True, wall_source=wall_source
    )
    candidates = session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.ambiguity_flags.contains(["slot-reader"])
        )
    ).all()
    for candidate in candidates:
        _reader_support(session, candidate)
    assert session.query(SlotRowReviewDecision).count() == 0
    assert session.query(CanonicalObservation).count() == 0

    first = _run_current_checks(session, package_id, tmp_path)
    assert {finding.scope_row_candidate_id for finding in first} == set(anchors.values())
    # #1202: only code's wall ink at both ends with both AIs is automatic; an older run's code-alone
    # clues, like the readers' proposal, wait for the reviewer's click.
    if wall_source == "code-ink-and-both-ais":
        # #1202 review: walls read from the vendor's drawing never make a PASS on their own; the
        # engine's PASS waits for one click, and nothing is decided for the reviewer.
        assert all(finding.outcome == "REVIEW_REQUIRED" for finding in first)
        assert all(finding.reason == VENDOR_WALLS_PASS_REASON for finding in first)
        assert session.query(SlotRowReviewDecision).count() == 0
    else:
        assert all(finding.outcome == "REVIEW_REQUIRED" for finding in first)
        assert all("wall layout" in (finding.reason or "").lower() for finding in first)
        principal = Principal(
            id="synthetic reviewer",
            roles=frozenset({Role.REVIEWER}),
            projects=frozenset({project_id}),
        )
        review_slot_row(
            principal,
            principal,
            session,
            project_id,
            package_id,
            anchors[0],
            SlotRowReviewIn(wall_config="back_left_right"),
        )
        second = _run_current_checks(session, package_id, tmp_path)
        by_row = {finding.scope_row_candidate_id: finding for finding in second}
        assert by_row[anchors[0]].outcome == "PASS", by_row[anchors[0]].reason
        assert by_row[anchors[1]].outcome == "REVIEW_REQUIRED"
        decision = session.query(SlotRowReviewDecision).one()
        assert not decision.measurements
        first = [by_row[anchors[0]]]

    for finding in first:
        assert finding.scope_item_id is None
        assert finding.scope_label is not None
        inputs = session.scalars(
            select(VerdictInput).where(VerdictInput.check_run_id == finding.check_run_id)
        ).all()
        assert {item.operand_name for item in inputs} == {
            "countertop_width",
            "piece_widths[0]",
            "piece_widths[1]",
        }
        assert all(item.evidence_status == EvidenceStatus.CORROBORATED.value for item in inputs)
        assert all(item.canonical_observation_id is not None for item in inputs)
        assert all(item.slot_row_review_decision_id is None for item in inputs)


@pytest.mark.parametrize("mark", ["\u201d", "\u2033", "''"])
def test_a_reader_writing_a_typographic_inch_mark_still_supports_the_sealed_width(
    session: Session, tmp_path: Path, mark: str
) -> None:
    """#1090. One reader wrote the inch mark as `”` (or `″`, or two primes), the other as `"`.
    Outcome: the row is checked exactly as a straight-quote row is, not refused as conflicting."""
    _project_id, package_id, anchors = _package_rows(
        session, piece_count=2, widths_add_up=True, wall_source="readers", walls_confirmed=True
    )
    for candidate in session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.ambiguity_flags.contains(["slot-reader"])
        )
    ).all():
        _reader_support(session, candidate, second_mark=mark)

    findings = _run_current_checks(session, package_id, tmp_path)

    assert {finding.scope_row_candidate_id for finding in findings} == set(anchors.values())
    assert all(finding.outcome == "PASS" for finding in findings), [f.reason for f in findings]


def _reader_call(
    session: Session,
    candidate: ObservationCandidate,
    model_id: str,
    text: str,
    *,
    outcome: str = "ok",
    packet: bool = False,
    asked_about: UUID | None = None,
    reused_from: UUID | None = None,
    prompt_id: str | None = None,
    belongs: bool = True,
) -> ModelInvocation:
    """One reader call on record for `candidate`'s run, as `workflow/stages.py` stores it.

    `packet=True` is the Claude path's: the question packet binds the answer to one label
    (`asked_about`, by default `candidate`); a reused answer (#1112) also names the call it reused.
    The legacy readers' calls carry no packet unless the run stored packets. `prompt_id` defaults to
    the Claude span question with a packet and the legacy crop question without; `belongs=False`
    leaves the field out, as the legacy crop answer does."""
    if prompt_id is None:
        prompt_id = CLAUDE_SPAN_PROMPT_ID if packet else CROP_PROMPT_ID
    question: dict[str, object] | None = None
    if packet:
        page = session.get(Page, candidate.page_id)
        assert page is not None
        question = {
            "candidate_ids": [str(asked_about or candidate.id)],
            "document_version_id": str(candidate.document_version_id),
            "page_index": page.index,
            "prompt_id": prompt_id,
        }
        question["packet_sha256"] = hashlib.sha256(
            json.dumps(question, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if reused_from is not None:
            question[REUSED_FROM_KEY] = str(reused_from)
    call = ModelInvocation(
        extraction_run_id=candidate.extraction_run_id,
        model_id=model_id,
        prompt_id=prompt_id,
        template_id="synthetic-slot-test",
        input_tokens=0,
        output_tokens=0,
        latency_ms=0,
        outcome=outcome,
        private_raw_response=json.dumps(
            {
                **({"belongs": "yes"} if belongs else {}),
                "text": text,
                "readable": True,
                "no_dimension": False,
                "stacked": False,
                "combined": False,
            }
        ),
        reader_question_packet=question,
    )
    session.add(call)
    session.flush()
    return call


def _reader_support(
    session: Session,
    candidate: ObservationCandidate,
    *,
    second_mark: str = '"',
    readers: tuple[str, str] = ("amazon.qwen3-vl", "moonshotai.kimi-k3"),
    calls: bool = True,
) -> list[ObservationCandidate]:
    """The two readers' records for a sealed candidate, each with its call on record (`calls`)."""
    assert candidate.value_numerator is not None
    supporters: list[ObservationCandidate] = []
    for reader_id, mark in zip(readers, ('"', second_mark), strict=True):
        if calls:
            _reader_call(session, candidate, reader_id, f"{candidate.value_numerator}{mark}")
        support = ObservationCandidate(
            document_version_id=candidate.document_version_id,
            page_id=candidate.page_id,
            extraction_run_id=candidate.extraction_run_id,
            raw_text=f"{candidate.value_numerator}{mark}",
            value_numerator=candidate.value_numerator,
            value_denominator=1,
            unit="in",
            polygon=candidate.polygon,
            coordinate_space="image",
            ambiguity_flags=[
                "slot-reader-support",
                f"supports:{candidate.id}",
                f"reader-id:{reader_id}",
            ],
        )
        session.add(support)
        supporters.append(support)
    session.flush()
    return supporters


def _canonical_with_reader_support(
    session: Session,
    candidate: ObservationCandidate,
    *,
    semantic: SemanticType,
    value_numerator: int | None = None,
    parent_supported: bool = False,
    lanes: tuple[CorroborationLane, ...] | None = None,
) -> CanonicalObservation:
    """`lanes` defaults to what the row check stores: the second reader's for a reader-sealed one."""
    assert candidate.value_numerator is not None
    numerator = candidate.value_numerator if value_numerator is None else value_numerator
    supporters = _reader_support(session, candidate)
    observation = CanonicalObservation(
        document_version_id=candidate.document_version_id,
        page_id=candidate.page_id,
        document_role="SHOP",
        polygon=[["0", "0"], ["1", "0"], ["1", "1"], ["0", "1"]],
        coordinate_space="stored",
        semantic_type=semantic.value,
        value_numerator=numerator,
        value_denominator=1,
        unit="in",
        status=(
            EvidenceStatus.HUMAN_CONFIRMED.value
            if parent_supported
            else EvidenceStatus.CORROBORATED.value
        ),
        authority=Authority.AUTHORITATIVE.value,
        evidence_crop_uri=None,
    )
    session.add(observation)
    session.flush()
    supported = [candidate] if parent_supported else supporters
    session.add_all(
        EvidenceSupportingCandidate(
            canonical_observation_id=observation.id,
            candidate_id=support.id,
            role="primary" if index == 0 else "corroborating",
        )
        for index, support in enumerate(supported)
    )
    if lanes is None:
        lanes = () if parent_supported else (CorroborationLane.SECOND_READER,)
    session.add_all(
        EvidenceCorroborationLane(canonical_observation_id=observation.id, lane=lane.value)
        for lane in lanes
    )
    session.flush()
    return observation


def test_child_supported_canonical_is_reused_without_duplicate(session: Session) -> None:
    _project_id, _package_id, anchors = _package_rows(session)
    candidate = session.get(ObservationCandidate, anchors[0])
    assert candidate is not None
    existing = _canonical_with_reader_support(
        session, candidate, semantic=SemanticType.COUNTERTOP_PIECE_WIDTH
    )
    supporters = session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.extraction_run_id == candidate.extraction_run_id,
            ObservationCandidate.ambiguity_flags.contains(["slot-reader-support"]),
        )
    ).all()
    assert len(supporters) == 2
    assert all(plain_dimension(item.raw_text, allow_explicit_mm=True) for item in supporters)
    assert all(item.polygon == candidate.polygon for item in supporters)
    assert all(
        item.value_numerator == candidate.value_numerator
        and item.value_denominator == candidate.value_denominator
        and item.unit == candidate.unit
        and f"supports:{candidate.id}" in (item.ambiguity_flags or [])
        for item in supporters
    )
    assert {
        independence_key("bedrock-slot-reader", item.ambiguity_flags[-1].removeprefix("reader-id:"))
        for item in supporters
    } == {
        "vendor:amazon",
        "vendor:moonshot",
    }
    assert existing.document_version_id == candidate.document_version_id
    assert existing.page_id == candidate.page_id
    assert existing.value_numerator == candidate.value_numerator
    assert existing.value_denominator == candidate.value_denominator
    assert existing.unit == "in"
    assert existing.status == EvidenceStatus.CORROBORATED.value

    result = _canonical_for_candidate(
        session,
        candidate,
        "SHOP:countertop_piece_width",
        semantic=SemanticType.COUNTERTOP_PIECE_WIDTH,
    )

    assert result is not None and result.id == existing.id
    assert session.query(CanonicalObservation).count() == 1


def test_mismatched_existing_canonical_refuses_row_without_creating_duplicate(
    session: Session,
) -> None:
    _project_id, _package_id, anchors = _package_rows(session)
    candidate = session.get(ObservationCandidate, anchors[0])
    assert candidate is not None
    _canonical_with_reader_support(
        session,
        candidate,
        semantic=SemanticType.COUNTERTOP_PIECE_WIDTH,
        value_numerator=999,
    )

    result = _canonical_for_candidate(
        session,
        candidate,
        "SHOP:countertop_piece_width",
        semantic=SemanticType.COUNTERTOP_PIECE_WIDTH,
    )

    assert result is None
    assert session.query(CanonicalObservation).count() == 1


def test_mismatched_canonical_becomes_row_review_without_crashing_stage(
    session: Session, tmp_path: Path
) -> None:
    _project_id, package_id, anchors = _package_rows(session, wall_source="code-ink-and-both-ais")
    anchor = session.get(ObservationCandidate, anchors[0])
    assert anchor is not None
    candidates = session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.extraction_run_id == anchor.extraction_run_id,
            ObservationCandidate.page_id == anchor.page_id,
            ObservationCandidate.ambiguity_flags.contains(["slot-reader"]),
        )
    ).all()
    for candidate in candidates:
        slot = next(
            flag.removeprefix("slot:")
            for flag in (candidate.ambiguity_flags or [])
            if flag.startswith("slot:")
        )
        _canonical_with_reader_support(
            session,
            candidate,
            semantic=(
                SemanticType.COUNTERTOP_OVERALL_WIDTH
                if slot == "overall"
                else SemanticType.COUNTERTOP_PIECE_WIDTH
            ),
            value_numerator=999 if candidate.id == anchor.id else None,
            parent_supported=True,
        )
    canonical_count = session.query(CanonicalObservation).count()

    findings = _run_current_checks(session, package_id, tmp_path)

    row_finding = next(item for item in findings if item.scope_row_candidate_id == anchor.id)
    assert row_finding.outcome == "REVIEW_REQUIRED"
    assert "conflicting or duplicate saved evidence" in (row_finding.reason or "").lower()
    assert session.query(CanonicalObservation).count() == canonical_count


def test_all_piece_width_inputs_cite_their_own_observation_and_parent_confirmation_is_safe(
    session: Session, tmp_path: Path
) -> None:
    _project_id, package_id, anchors = _package_rows(
        session, piece_count=2, wall_source="code-ink-and-both-ais"
    )
    anchor = session.get(ObservationCandidate, anchors[0])
    assert anchor is not None
    candidates = session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.extraction_run_id == anchor.extraction_run_id,
            ObservationCandidate.page_id == anchor.page_id,
            ObservationCandidate.ambiguity_flags.contains(["slot-reader"]),
        )
    ).all()
    expected: dict[str, UUID] = {}
    for candidate in candidates:
        flags = set(candidate.ambiguity_flags or ())
        slot = next(flag.removeprefix("slot:") for flag in flags if flag.startswith("slot:"))
        semantic = (
            SemanticType.COUNTERTOP_OVERALL_WIDTH
            if slot == "overall"
            else SemanticType.COUNTERTOP_PIECE_WIDTH
        )
        observation = _canonical_with_reader_support(
            session, candidate, semantic=semantic, parent_supported=True
        )
        if slot != "overall":
            expected[f"piece_widths[{slot}]"] = observation.id

    findings = _run_current_checks(session, package_id, tmp_path)
    finding = next(item for item in findings if item.scope_row_candidate_id == anchors[0])
    assert finding.outcome == "FAIL"
    inputs = session.scalars(
        select(VerdictInput).where(VerdictInput.check_run_id == finding.check_run_id)
    ).all()
    actual = {
        item.operand_name: item.canonical_observation_id
        for item in inputs
        if item.operand_name.startswith("piece_widths[")
    }
    assert actual == expected
    assert all(
        item.evidence_status == EvidenceStatus.HUMAN_CONFIRMED.value
        for item in inputs
        if item.operand_name.startswith("piece_widths[")
    )


def test_a_row_whose_stone_does_not_end_at_the_walls_is_never_checked(
    session: Session, tmp_path: Path
) -> None:
    """A complete row with drawing-clue walls would PASS; flagged `check-hold:` (the stone stops
    at fillers before the ends, so the field cut is theirs) it stays with the reviewer, with the
    reason, and the other row still checks."""
    _project_id, package_id, anchors = _package_rows(
        session,
        piece_count=2,
        widths_add_up=True,
        wall_source="readers",
        walls_confirmed=(1,),
        check_hold_page=0,
    )
    candidates = session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.ambiguity_flags.contains(["slot-reader"])
        )
    ).all()
    for candidate in candidates:
        _reader_support(session, candidate)

    findings = _run_current_checks(session, package_id, tmp_path)
    by_row = {finding.scope_row_candidate_id: finding for finding in findings}
    assert by_row[anchors[0]].outcome == "REVIEW_REQUIRED"
    assert "stone stops at fillers" in (by_row[anchors[0]].reason or "")
    assert by_row[anchors[1]].outcome == "PASS", by_row[anchors[1]].reason


_NO_STONE = "no-stone:anthropic.claude-sonnet-5-5"


def test_a_reader_saying_no_stone_adds_no_hold_and_the_row_still_checks(
    session: Session, tmp_path: Path
) -> None:
    """#1111: a counter-break reader's `no_stone` is a recorded note, never a hold: the row has no
    reason text of its own, stays open for a wall choice, and checks exactly as before."""
    project_id, package_id, anchors = _package_rows(
        session,
        piece_count=2,
        widths_add_up=True,
        wall_source="readers",
        walls_confirmed=True,
        slot_flags=lambda page, _row, _slot: [_NO_STONE] if page == 0 else [],
    )
    candidates = session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.ambiguity_flags.contains(["slot-reader"])
        )
    ).all()
    for candidate in candidates:
        _reader_support(session, candidate)
    principal = Principal(
        id="synthetic reviewer", roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
    )

    rows = {
        row.page_number: row
        for row in list_slot_rows(principal, session, project_id, package_id).rows
    }
    assert rows[1].held_reason is None and rows[2].held_reason is None
    assert rows[1].wall_confirmation_allowed == rows[2].wall_confirmation_allowed
    findings = _run_current_checks(session, package_id, tmp_path)
    by_row = {finding.scope_row_candidate_id: finding for finding in findings}
    assert by_row[anchors[0]].outcome == by_row[anchors[1]].outcome == "PASS"


def test_a_reader_saying_no_stone_is_named_in_a_held_rows_reason(session: Session) -> None:
    project_id, package_id, _anchors = _package_rows(
        session,
        held_page=0,
        check_hold_page=1,
        slot_flags=lambda _page, _row, _slot: [_NO_STONE],
    )
    principal = Principal(
        id="synthetic reviewer", roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
    )

    rows = {
        row.page_number: row
        for row in list_slot_rows(principal, session, project_id, package_id).rows
    }
    note = "A reader also said no stone top is drawn over this row."
    for row in rows.values():
        assert row.held_reason is not None and row.held_reason.endswith(note)
        assert row.held_reason.count(note) == 1
    assert (rows[2].held_reason or "").startswith("the stone stops at fillers")


@pytest.mark.parametrize("overall, expected", [(41, "PASS"), (42, "FAIL")])
def test_between_panels_requires_its_own_explicit_wall_choice(
    session: Session, tmp_path: Path, overall: int, expected: str
) -> None:
    project_id, package_id, anchors = _package_rows(
        session,
        piece_count=2,
        wall_source="code-ink-and-both-ais",
        check_hold_all=True,
        overall_override=overall,
    )
    candidates = session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.ambiguity_flags.contains(["slot-reader"])
        )
    ).all()
    for candidate in candidates:
        _reader_support(session, candidate)
    principal = Principal(
        id="synthetic reviewer", roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
    )
    rows = list_slot_rows(principal, session, project_id, package_id).rows
    assert rows[0].wall_proposal == "back_only"
    assert rows[0].wall_source == "between-panels"
    # #1138: `back_only` here means "no field cut", never "back wall only".
    assert "no field cut: the stone stops at panels" in (rows[0].wall_reason or "")
    assert "back only" not in (rows[0].wall_reason or "").lower()
    assert rows[0].wall_confirmation_allowed
    assert rows[0].held_reason is not None
    assert rows[0].wall_config is None
    first = _run_current_checks(session, package_id, tmp_path)
    assert all(finding.outcome == "REVIEW_REQUIRED" for finding in first)
    assert session.query(SlotRowReviewDecision).count() == 0

    saved = review_slot_row(
        principal,
        principal,
        session,
        project_id,
        package_id,
        anchors[0],
        SlotRowReviewIn(wall_config="back_only"),
    )
    assert saved.held_reason is None
    assert saved.wall_config == "back_only"
    assert saved.confirmed_by == principal.id
    second = _run_current_checks(session, package_id, tmp_path)
    by_row = {finding.scope_row_candidate_id: finding for finding in second}
    assert by_row[anchors[0]].outcome == expected, by_row[anchors[0]].reason
    assert by_row[anchors[1]].outcome == "REVIEW_REQUIRED"
    # #1138: the finding records the choice as "no field cut", never "back wall only".
    notes = " | ".join(by_row[anchors[0]].notes or ())
    assert "no field cut: the stone stops at panels" in notes
    assert "back wall only" not in notes

    # A correction is row-local; withdrawing the wall choice reinstates the hold.
    changed = review_slot_row(
        principal,
        principal,
        session,
        project_id,
        package_id,
        anchors[0],
        SlotRowReviewIn(wall_config="back_left_right"),
    )
    assert changed.wall_config == "back_left_right"
    assert changed.held_reason is None
    cleared = review_slot_row(
        principal,
        principal,
        session,
        project_id,
        package_id,
        anchors[0],
        SlotRowReviewIn(wall_config=None),
    )
    assert cleared.held_reason is not None
    third = _run_current_checks(session, package_id, tmp_path)
    assert all(finding.outcome == "REVIEW_REQUIRED" for finding in third)


@pytest.mark.parametrize(
    "extra_hold",
    [
        "check-hold:stone-into-walls",
        "row-hold:counter-break",
        "row-ambiguous",
        "row-partial",
    ],
)
def test_between_panels_never_unlocks_another_hold(session: Session, extra_hold: str) -> None:
    project_id, package_id, anchors = _package_rows(
        session, check_hold_page=0, extra_hold=extra_hold
    )
    principal = Principal(
        id="synthetic reviewer", roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
    )
    rows = list_slot_rows(principal, session, project_id, package_id).rows
    assert not rows[0].wall_confirmation_allowed
    with pytest.raises(HTTPException) as refused:
        review_slot_row(
            principal,
            principal,
            session,
            project_id,
            package_id,
            anchors[0],
            SlotRowReviewIn(wall_config="back_only"),
        )
    assert refused.value.status_code == 409
    assert session.query(SlotRowReviewDecision).count() == 0


@pytest.mark.parametrize(
    "body",
    [
        SlotRowReviewIn(),
        SlotRowReviewIn(wall_config=None),
        SlotRowReviewIn(measurements={"piece_widths:0": "19 in"}),
        SlotRowReviewIn(wall_config="back_only", measurements={"piece_widths:0": "19 in"}),
    ],
)
def test_between_panels_hold_accepts_only_an_explicit_wall_decision(
    session: Session, body: SlotRowReviewIn
) -> None:
    project_id, package_id, anchors = _package_rows(session, check_hold_page=0, unsealed_page=0)
    principal = Principal(
        id="synthetic reviewer", roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
    )
    with pytest.raises(HTTPException) as refused:
        review_slot_row(principal, principal, session, project_id, package_id, anchors[0], body)
    assert refused.value.status_code == 409
    assert session.query(SlotRowReviewDecision).count() == 0


# ---------------------------------------------------------------------------
# #1138: a countertop with a wall at one end only
# ---------------------------------------------------------------------------


def test_a_row_with_one_wall_end_takes_one_field_cut_once_its_reviewer_confirms_it(
    session: Session, tmp_path: Path
) -> None:
    """Readers alone sealed "back wall and right end": the row waits for its reviewer's click, then
    needs its pieces plus one field cut, not two. The choice is stored as made."""
    project_id, package_id, anchors = _package_rows(
        session,
        piece_count=2,
        widths_add_up=True,
        field_cuts=1,
        sealed_layout="back_and_right",
        wall_source="readers",
    )
    for candidate in session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.ambiguity_flags.contains(["slot-reader"])
        )
    ).all():
        _reader_support(session, candidate)
    principal = Principal(
        id="synthetic reviewer", roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
    )
    rows = list_slot_rows(principal, session, project_id, package_id).rows
    assert rows[0].wall_proposal == "back_and_right"
    assert rows[0].wall_source == "readers"
    assert rows[0].wall_config is None
    assert {"back_and_left", "back_and_right"} <= set(rows[0].wall_layout_choices)

    first = _run_current_checks(session, package_id, tmp_path)
    assert all(finding.outcome == "REVIEW_REQUIRED" for finding in first)
    assert all("wall layout" in (finding.reason or "").lower() for finding in first)

    saved = review_slot_row(
        principal,
        principal,
        session,
        project_id,
        package_id,
        anchors[0],
        SlotRowReviewIn(wall_config="back_and_right"),
    )
    assert saved.wall_config == "back_and_right"
    session.expire_all()
    assert session.query(SlotRowReviewDecision).one().wall_config == "back_and_right"

    second = _run_current_checks(session, package_id, tmp_path)
    by_row = {finding.scope_row_candidate_id: finding for finding in second}
    assert by_row[anchors[0]].outcome == "PASS", by_row[anchors[0]].reason
    assert by_row[anchors[0]].variant == "back_and_right"
    assert (
        "Wall layout: back wall and right end (back_and_right), chosen by synthetic reviewer"
        in (" | ".join(by_row[anchors[0]].notes or ()))
    )
    # The other row's reader-only proposal was not confirmed: it still waits.
    assert by_row[anchors[1]].outcome == "REVIEW_REQUIRED"

    # Walls at both ends would need one more inch than the vendor drew.
    review_slot_row(
        principal,
        principal,
        session,
        project_id,
        package_id,
        anchors[0],
        SlotRowReviewIn(wall_config="back_left_right"),
    )
    third = _run_current_checks(session, package_id, tmp_path)
    by_row = {finding.scope_row_candidate_id: finding for finding in third}
    assert by_row[anchors[0]].outcome == "FAIL"


# ---------------------------------------------------------------------------
# #1107: a width PASS resting on a reading with no drawn-length witness
# ---------------------------------------------------------------------------

NO_WITNESS = "no-drawn-length-witness"


def _unwitnessed_on_first_page(page_index: int, _row_offset: int, slot: str) -> list[str]:
    """A two-piece row's pieces each have one neighbour to scale by: no scale, no witness. Its
    overall has two, so it is witnessed. Only the first page's row is flagged."""
    return [NO_WITNESS] if page_index == 0 and slot != "overall" else []


def _sealed_two_piece_rows(
    session: Session, **options: object
) -> tuple[UUID, UUID, dict[int, UUID]]:
    ids = _package_rows(
        session,
        piece_count=2,
        widths_add_up=True,
        wall_source="readers",
        walls_confirmed=True,
        slot_flags=_unwitnessed_on_first_page,
        **options,  # type: ignore[arg-type]
    )
    for candidate in session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.ambiguity_flags.contains(["slot-reader"])
        )
    ).all():
        _reader_support(session, candidate)
    return ids


def test_a_pass_resting_on_a_reading_with_no_drawn_length_witness_waits_for_one_click(
    session: Session, tmp_path: Path
) -> None:
    """#1107 (3). Input: two sealed two-piece rows that add up; the first page's pieces were
    sealed with no drawn-length witness. Outcome: that row's PASS becomes REVIEW_REQUIRED with
    the decided reason, the engine's PASS kept in the notes and the unchecked pieces named; the
    second row, whose readings were all checked, keeps its automatic PASS. The numbers stay in
    the recorded inputs."""
    from vocabulary.drawn_length import NO_WITNESS_REASON

    _project_id, package_id, anchors = _sealed_two_piece_rows(session)

    by_row = {
        finding.scope_row_candidate_id: finding
        for finding in _run_current_checks(session, package_id, tmp_path)
    }

    held = by_row[anchors[0]]
    assert held.outcome == "REVIEW_REQUIRED", held.reason
    assert held.reason == NO_WITNESS_REASON
    assert held.notes is not None
    assert held.notes[0].startswith("The engine's result, which counts only once a person ")
    assert "PASS" in held.notes[0]
    assert "Drawn length not checked (no scale): piece 1, piece 2." in held.notes
    inputs = session.scalars(
        select(VerdictInput).where(VerdictInput.check_run_id == held.check_run_id)
    ).all()
    assert {item.operand_name for item in inputs} >= {
        "countertop_width",
        "piece_widths[0]",
        "piece_widths[1]",
    }
    pieces = session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.ambiguity_flags.contains(["slot-reader"]),
            ObservationCandidate.ambiguity_flags.contains([NO_WITNESS]),
        )
    ).all()
    assert len(pieces) == 2, "the witness flag is on the two readings it concerns"

    witnessed = by_row[anchors[1]]
    assert witnessed.outcome == "PASS", witnessed.reason
    assert not any("Drawn length not checked" in note for note in witnessed.notes or ())


def test_a_fail_resting_on_a_reading_with_no_drawn_length_witness_is_unchanged(
    session: Session, tmp_path: Path
) -> None:
    """#1107 (3). Input: the same unwitnessed row, its printed overall wrong. Outcome: FAIL, as
    before: a FAIL already waits for the reviewer."""
    _project_id, package_id, anchors = _sealed_two_piece_rows(session, overall_override=99)

    by_row = {
        finding.scope_row_candidate_id: finding
        for finding in _run_current_checks(session, package_id, tmp_path)
    }

    assert by_row[anchors[0]].outcome == "FAIL", by_row[anchors[0]].reason
    assert not any("Drawn length not checked" in note for note in by_row[anchors[0]].notes or ())


def test_readings_a_person_confirmed_never_need_the_witness(
    session: Session, tmp_path: Path
) -> None:
    """#1107 (3), #1339. Input: a row whose unsealed readings carry the flag, every one confirmed by
    a person for that row. Outcome: an automatic PASS: a person's confirmation is the second
    judgment, so the drawn-length witness is not asked for."""
    _project_id, package_id, anchors = _package_rows(
        session,
        unsealed_all=True,
        wall_source="readers",
        walls_confirmed=True,
        widths_add_up=True,
        slot_flags=lambda _page, _offset, _slot: [NO_WITNESS],
    )
    _confirm_row_readings(session, anchors[0])

    by_row = {
        finding.scope_row_candidate_id: finding
        for finding in _run_current_checks(session, package_id, tmp_path)
    }

    assert by_row[anchors[0]].outcome == "PASS", by_row[anchors[0]].reason


def test_the_countertop_results_say_drawn_length_not_checked_for_that_row(
    session: Session, tmp_path: Path
) -> None:
    """#1107 (2). Input: the unwitnessed and the witnessed row, checked. Outcome: the countertop
    results say "Drawn length not checked (no scale)" for the first row's two pieces only."""
    from app.api.visual_countertops import _countertop_results_for_revision

    _project_id, package_id, anchors = _sealed_two_piece_rows(session)
    _run_current_checks(session, package_id, tmp_path)
    revision = session.query(PackageRevision).filter_by(package_id=package_id).one()

    results = _countertop_results_for_revision(session, package_id, revision)

    by_row = {item.row_id: item for item in results.items}
    assert by_row[anchors[0]].drawn_length_note == (
        "Drawn length not checked (no scale): piece 1, piece 2."
    )
    assert by_row[anchors[0]].outcome == "REVIEW_REQUIRED"
    assert by_row[anchors[0]].needs_decision
    assert by_row[anchors[1]].drawn_length_note is None
    assert by_row[anchors[1]].outcome == "PASS"


def test_a_reading_with_the_files_own_text_among_its_sources_never_needs_the_click(
    session: Session, tmp_path: Path
) -> None:
    """#1107 (decided 2026-10-09): the file's own text is a non-model witness. Input: the
    unwitnessed row, its pieces read from the PDF text and one reader. Outcome: automatic PASS."""
    _project_id, package_id, anchors = _package_rows(
        session,
        piece_count=2,
        widths_add_up=True,
        wall_source="readers",
        walls_confirmed=True,
        slot_flags=lambda page, _offset, slot: (
            [NO_WITNESS, "reader-id:pdf-text-layer", "reader-id:amazon.qwen3-vl"]
            if page == 0 and slot != "overall"
            else []
        ),
    )
    for candidate in session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.ambiguity_flags.contains(["slot-reader"])
        )
    ).all():
        _reader_support(session, candidate)

    by_row = {
        finding.scope_row_candidate_id: finding
        for finding in _run_current_checks(session, package_id, tmp_path)
    }

    assert by_row[anchors[0]].outcome == "PASS", by_row[anchors[0]].reason


def test_a_typed_squeezed_width_is_refused_like_any_typed_width(session: Session) -> None:
    """#1199, #1339: a typed `163/8"` width is never saved; no width is typed for a row."""
    project_id, package_id, anchors = _package_rows(session, unsealed_page=0)
    principal = Principal(
        id="anant (synthetic test)",
        roles=frozenset({Role.REVIEWER}),
        projects=frozenset({project_id}),
    )

    with pytest.raises(HTTPException) as refused:
        review_slot_row(
            principal,
            principal,
            session,
            project_id,
            package_id,
            anchors[0],
            SlotRowReviewIn(
                wall_config="back_left_right",
                measurements={"piece_widths:0": '163/8"'},
            ),
        )

    assert refused.value.status_code == 422
    assert refused.value.detail == TYPED_WIDTH_CLOSED
    assert session.query(SlotRowReviewDecision).count() == 0


OPUS = "anthropic.claude-opus-5-5"
SONNET = "anthropic.claude-sonnet-5-5"


def test_a_saved_value_seals_only_with_the_second_reader_lane_on_record(session: Session) -> None:
    """The gate's two-reader rule is checked against the stored lanes, never assumed. Input: a
    saved reader-sealed value with its two reader records, with and without the second reader's
    lane on record. Outcome: without it the gate refuses; with it the value seals."""
    _project_id, _package_id, anchors = _package_rows(session)
    candidate = session.get(ObservationCandidate, anchors[0])
    assert candidate is not None
    unlaned = _canonical_with_reader_support(
        session, candidate, semantic=SemanticType.COUNTERTOP_PIECE_WIDTH, lanes=()
    )

    observation = _domain_observation(session, unlaned)

    assert observation is not None
    assert observation.corroborated_by == ()
    refusal = seal(observation, "piece_width_1")
    assert isinstance(refusal, GateRefusal)
    assert "second-reader agreement" in refusal.detail

    other = session.get(ObservationCandidate, anchors[1])
    assert other is not None
    laned = _canonical_with_reader_support(
        session, other, semantic=SemanticType.COUNTERTOP_PIECE_WIDTH
    )
    sealed_observation = _domain_observation(session, laned)
    assert sealed_observation is not None
    assert not isinstance(seal(sealed_observation, "piece_width_1"), GateRefusal)


def _pair_records(session: Session, candidate: ObservationCandidate) -> None:
    """The Claude pair's two reader records for `candidate`, as the slot reader writes them."""
    assert candidate.value_numerator is not None
    _reader_support(session, candidate, readers=(OPUS, SONNET), calls=False)


@pytest.mark.parametrize(
    "sonnet",
    [
        "failed",  # Sonnet's call came back with no answer
        "timeout",
        "missing",  # no call by Sonnet on record at all
        "another-label",  # Sonnet's only answer on record is to a different label
        "another-value",  # Sonnet's answer on record says a different number
    ],
)
def test_a_reader_record_without_its_answer_on_record_never_seals(
    session: Session, sonnet: str
) -> None:
    """Two readers must really have agreed: each reader record needs that reader's own `ok` call
    in this run, answering this label with this exact value. Input: the pair's two records, Opus's
    answer on record, Sonnet's call failed, missing, about another label or another value.
    Outcome: no evidence is created and the row is refused (the person decides)."""
    _project_id, _package_id, anchors = _package_rows(session)
    candidate = session.get(ObservationCandidate, anchors[0])
    assert candidate is not None
    value = f'{candidate.value_numerator}"'
    _pair_records(session, candidate)
    _reader_call(session, candidate, OPUS, value, packet=True)
    if sonnet in {"failed", "timeout"}:
        _reader_call(session, candidate, SONNET, value, packet=True, outcome=sonnet)
    elif sonnet == "another-label":
        _reader_call(session, candidate, SONNET, value, packet=True, asked_about=anchors[1])
    elif sonnet == "another-value":
        _reader_call(session, candidate, SONNET, '99"', packet=True)

    result = _canonical_for_candidate(
        session,
        candidate,
        "SHOP:countertop_piece_width",
        semantic=SemanticType.COUNTERTOP_PIECE_WIDTH,
    )

    assert result is None
    assert session.query(CanonicalObservation).count() == 0


@pytest.mark.parametrize("reused", [(), (SONNET,), (OPUS, SONNET)])
def test_reused_answers_on_record_still_seal(session: Session, reused: tuple[str, ...]) -> None:
    """#1112: a reused answer is that reader's own earlier answer to the identical question,
    recorded under the new run. Input: the pair's two records, each reader's answer on record,
    live or reused. Outcome: the value seals with the second reader's lane."""
    _project_id, _package_id, anchors = _package_rows(session)
    candidate = session.get(ObservationCandidate, anchors[0])
    assert candidate is not None
    value = f'{candidate.value_numerator}"'
    _pair_records(session, candidate)
    for model in (OPUS, SONNET):
        _reader_call(
            session,
            candidate,
            model,
            value,
            packet=True,
            reused_from=uuid4() if model in reused else None,
        )

    result = _canonical_for_candidate(
        session,
        candidate,
        "SHOP:countertop_piece_width",
        semantic=SemanticType.COUNTERTOP_PIECE_WIDTH,
    )

    assert result is not None
    observation = _domain_observation(session, result)
    assert observation is not None
    assert observation.corroborated_by == (CorroborationLane.SECOND_READER,)
    assert not isinstance(seal(observation, "piece_width_1"), GateRefusal)


def test_a_legacy_reader_record_needs_a_call_by_that_reader_in_the_run(session: Session) -> None:
    """The legacy readers' calls carry no question packet, so each record needs at least an `ok`
    call by that reader in this run. Input: two records, one reader with no call. Outcome: refused.
    """
    _project_id, _package_id, anchors = _package_rows(session)
    candidate = session.get(ObservationCandidate, anchors[0])
    assert candidate is not None
    _reader_support(session, candidate, calls=False)
    _reader_call(session, candidate, "amazon.qwen3-vl", f'{candidate.value_numerator}"')

    result = _canonical_for_candidate(
        session,
        candidate,
        "SHOP:countertop_piece_width",
        semantic=SemanticType.COUNTERTOP_PIECE_WIDTH,
    )

    assert result is None


def _piece_evidence(
    session: Session, candidate: ObservationCandidate
) -> CanonicalObservation | None:
    return _canonical_for_candidate(
        session,
        candidate,
        "SHOP:countertop_piece_width",
        semantic=SemanticType.COUNTERTOP_PIECE_WIDTH,
    )


#: The other questions a Claude reader answers in a run besides the labels: the row choice, the
#: walls, the counter break and (#1221) the layout classifier.
_OTHER_CALLS = (ROW_PROMPT_ID, WALL_PROMPT_ID, COUNTER_BREAK_PROMPT_ID, LAYOUT_PROMPT_ID)


@pytest.mark.parametrize(
    ("opus_answered", "sonnet", "other_call"),
    [
        *((True, sonnet, other) for sonnet in ("failed", "missing") for other in _OTHER_CALLS),
        # Neither reader answered any label; each made only a row-choice call.
        (False, "missing", ROW_PROMPT_ID),
    ],
)
def test_a_claude_reader_never_passes_on_a_call_that_is_not_its_answer_to_this_label(
    session: Session, opus_answered: bool, sonnet: str, other_call: str
) -> None:
    """A Claude reader is matched by its answer to this very label, never by any other `ok` call
    it made in the run (#1226 review). Input: the pair's two records; Opus's answer on record (or
    not); Sonnet's label call failed or missing; plus an `ok` call by each reader without its own
    answer to some other question (row choice, walls, counter break or layout), bound to this
    label's packet and worded like a matching answer. Outcome: no evidence; the row is refused."""
    _project_id, _package_id, anchors = _package_rows(session)
    candidate = session.get(ObservationCandidate, anchors[0])
    assert candidate is not None
    value = f'{candidate.value_numerator}"'
    _pair_records(session, candidate)
    if opus_answered:
        _reader_call(session, candidate, OPUS, value, packet=True)
    else:
        _reader_call(session, candidate, OPUS, value, packet=True, prompt_id=other_call)
    if sonnet == "failed":
        _reader_call(session, candidate, SONNET, value, packet=True, outcome="failed")
    _reader_call(session, candidate, SONNET, value, packet=True, prompt_id=other_call)

    assert _piece_evidence(session, candidate) is None
    assert session.query(CanonicalObservation).count() == 0


def test_a_claude_mode_value_never_passes_on_the_legacy_rule(session: Session) -> None:
    """A value the Claude readers read (`reader-mode:claude`) is matched by each reader's answer
    to this label, whatever its readers are named. Input: such a value with two records whose
    readers each made one `ok` legacy crop call carrying no packet. Outcome: refused."""
    _project_id, _package_id, anchors = _package_rows(
        session, slot_flags=lambda _page, _row, _slot: ["reader-mode:claude"]
    )
    candidate = session.get(ObservationCandidate, anchors[0])
    assert candidate is not None
    _reader_support(session, candidate)

    assert _piece_evidence(session, candidate) is None


def test_a_legacy_reader_needs_a_crop_call_not_any_call(session: Session) -> None:
    """The no-packet fallback accepts only the legacy crop question (`slot-crop-v1`, with or
    without the product). Input: two legacy records; one reader's only `ok` call is a row choice.
    Outcome: refused."""
    _project_id, _package_id, anchors = _package_rows(session)
    candidate = session.get(ObservationCandidate, anchors[0])
    assert candidate is not None
    value = f'{candidate.value_numerator}"'
    _reader_support(session, candidate, calls=False)
    _reader_call(session, candidate, "amazon.qwen3-vl", value, belongs=False)
    _reader_call(session, candidate, "moonshotai.kimi-k3", value, prompt_id=ROW_PROMPT_ID)

    assert _piece_evidence(session, candidate) is None


@pytest.mark.parametrize("matches", [False, True])
def test_a_legacy_reader_whose_calls_carry_packets_is_matched_by_its_answer(
    session: Session, matches: bool
) -> None:
    """A run that stored question packets for the legacy crop question binds each answer to its
    label, so the reader is matched by that answer, as a Claude reader is. Input: two legacy
    records; each reader's crop answer (the legacy shape, no `belongs`) carries a packet, about
    this label or another one. Outcome: about another label → refused; about this one → sealed."""
    _project_id, _package_id, anchors = _package_rows(session)
    candidate = session.get(ObservationCandidate, anchors[0])
    assert candidate is not None
    value = f'{candidate.value_numerator}"'
    _reader_support(session, candidate, calls=False)
    for reader in ("amazon.qwen3-vl", "moonshotai.kimi-k3"):
        _reader_call(
            session,
            candidate,
            reader,
            value,
            packet=True,
            prompt_id=crop_prompt_id(ProductType.COUNTERTOP),
            belongs=False,
            asked_about=None if matches else anchors[1],
        )

    assert (_piece_evidence(session, candidate) is not None) is matches


def test_a_legacy_reader_with_a_product_crop_call_and_no_packet_still_seals(
    session: Session,
) -> None:
    """Control: a legacy run (no packets) whose crop question named the product. Input: two
    legacy records, each reader with one `ok` crop call (`slot-crop-v1+product=…`), no packet.
    Outcome: sealed, as before."""
    _project_id, _package_id, anchors = _package_rows(session)
    candidate = session.get(ObservationCandidate, anchors[0])
    assert candidate is not None
    value = f'{candidate.value_numerator}"'
    _reader_support(session, candidate, calls=False)
    for reader in ("amazon.qwen3-vl", "moonshotai.kimi-k3"):
        _reader_call(
            session,
            candidate,
            reader,
            value,
            prompt_id=crop_prompt_id(ProductType.COUNTERTOP),
            belongs=False,
        )

    assert _piece_evidence(session, candidate) is not None
