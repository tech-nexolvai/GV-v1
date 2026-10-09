"""Reviewer decisions for a slot-reader row never carry to another row."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.slot_rows import SlotRowReviewIn, list_slot_rows, review_slot_row
from app.auth.roles import Principal, Role
from app.db.session import session_factory
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
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from app.models.verdicts import CheckRun, Finding, VerdictInput
from evidence.canonical import Authority, EvidenceStatus
from evidence.corroborate import independence_key
from extraction.slot_reader.labels import plain_dimension
from rules.semantic_types import SemanticType
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_stages import _publish_rulebook
from workflow.slot_row_evidence import _canonical_for_candidate
from workflow.stages import DatabaseStages

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
) -> tuple[UUID, UUID, dict[int, UUID]]:
    """`slot_flags(page_index, row_offset, slot)` adds flags (e.g. a `slot-box:`) to a slot.

    `row_pages` are the page indexes (of 0 and 1) that get rows; `()` makes a run with none."""
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
            # The published company standard adds one inch at each of the two wall ends.
            overall = sum(value for _, _, value in slots) + 2 if widths_add_up else 40 + page_index
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
                    ambiguity_flags=[
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
                        *(() if slot_flags is None else slot_flags(page_index, row_offset, slot)),
                    ],
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
                raw_text="walls: back_left_right",
                polygon=[[0, 0], [10, 0], [10, 10], [0, 10]],
                coordinate_space="image",
                ambiguity_flags=[
                    "wall-reader",
                    f"row-rank:{rank}",
                    "walls-sealed:back_left_right",
                    f"wall-source:{wall_source}",
                    *(["row-hold:synthetic held row"] if page_index == held_page else []),
                ],
                review_reason="synthetic held row" if page_index == held_page else None,
            )
            session.add(wall)
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


def test_typed_missing_width_is_saved_only_on_the_selected_row(session: Session) -> None:
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

    saved = review_slot_row(
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
    saved_piece = next(value for value in saved.values if value.key == "piece_widths:0")
    assert saved_piece.value == "21 in"
    assert saved_piece.source == "reviewer"

    rows = {
        row.page_number: row
        for row in list_slot_rows(principal, session, project_id, package_id).rows
    }
    assert rows[1].decision_id is not None
    assert rows[2].decision_id is None
    assert rows[2].wall_config is None
    assert session.query(SlotRowReviewDecision).count() == 1
    refreshed = list_slot_rows(principal, session, project_id, package_id)
    by_page = {row.page_number: row for row in refreshed.rows}
    assert by_page[1].confirmed_by == "anant (synthetic test)"
    assert by_page[1].wall_config == "back_left_right"
    assert by_page[2].decision_id is None
    assert by_page[2].wall_config is None
    assert session.query(SlotRowReviewDecision).count() == 1


def test_reviewer_can_correct_a_typed_width_with_a_new_immutable_snapshot(
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
        SlotRowReviewIn(
            wall_config="back_left_right",
            measurements={"piece_widths:0": "21 in"},
        ),
    )
    prior_id = first.decision_id

    corrected = review_slot_row(
        principal,
        principal,
        session,
        project_id,
        package_id,
        anchors[0],
        SlotRowReviewIn(measurements={"piece_widths:0": "22 in"}),
    )

    piece = next(value for value in corrected.values if value.key == "piece_widths:0")
    assert piece.value == "22 in"
    assert corrected.decision_id != prior_id
    decisions = (
        session.query(SlotRowReviewDecision).order_by(SlotRowReviewDecision.created_at).all()
    )
    assert len(decisions) == 2
    assert decisions[-1].supersedes_id == prior_id


def _run_current_checks(session: Session, package_id: UUID, root: Path) -> list[Finding]:
    revision = session.query(PackageRevision).filter_by(package_id=package_id).one()
    DatabaseStages(
        store=LocalStore(root=root / "store", ticket_secret=b"synthetic-test")
    ).run_checks(session, revision.id)
    return list(
        session.execute(
            select(Finding)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .where(
                Finding.package_revision_id == revision.id,
                Finding.scope_row_candidate_id.is_not(None),
                CheckRun.superseded_at.is_(None),
            )
        ).scalars()
    )


def _save_all_row_widths(
    session: Session,
    project_id: UUID,
    package_id: UUID,
    row_id: UUID,
    *,
    wall_config: str | None = None,
) -> None:
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
        SlotRowReviewIn(
            wall_config=wall_config,
            measurements={"countertop_width": "40 in", "piece_widths:0": "20 in"},
        ),
    )


def test_end_to_end_drawing_clue_row_fails_with_its_own_scope(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id, anchors = _package_rows(
        session, unsealed_all=True, wall_source="vendor-drawing-clues"
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
    assert rows[0].wall_source == "vendor-drawing-clues"

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
    assert row_finding.outcome == "REVIEW_REQUIRED"
    assert (
        not row_finding.reason
        or "choose" in row_finding.reason.lower()
        or "confirm" in row_finding.reason.lower()
    )

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
        session, unsealed_all=True, wall_source="vendor-drawing-clues", held_page=0
    )
    findings = _run_current_checks(session, package_id, tmp_path)
    held = next(finding for finding in findings if finding.scope_row_candidate_id == anchors[0])
    assert held.outcome == "REVIEW_REQUIRED"
    assert "held" in (held.reason or "").lower()


@pytest.mark.parametrize("wall_source", ["vendor-drawing-clues", "readers"])
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
    if wall_source == "vendor-drawing-clues":
        assert all(finding.outcome == "PASS" for finding in first)
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
        session, piece_count=2, widths_add_up=True, wall_source="vendor-drawing-clues"
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


def _reader_support(
    session: Session,
    candidate: ObservationCandidate,
    *,
    second_mark: str = '"',
) -> list[ObservationCandidate]:
    assert candidate.value_numerator is not None
    supporters: list[ObservationCandidate] = []
    for reader_id, mark in (("amazon.qwen3-vl", '"'), ("moonshotai.kimi-k3", second_mark)):
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
) -> CanonicalObservation:
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
    _project_id, package_id, anchors = _package_rows(session, wall_source="vendor-drawing-clues")
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
        session, piece_count=2, wall_source="vendor-drawing-clues"
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
        wall_source="vendor-drawing-clues",
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


@pytest.mark.parametrize("overall, expected", [(41, "PASS"), (42, "FAIL")])
def test_between_panels_requires_its_own_explicit_wall_choice(
    session: Session, tmp_path: Path, overall: int, expected: str
) -> None:
    project_id, package_id, anchors = _package_rows(
        session,
        piece_count=2,
        wall_source="vendor-drawing-clues",
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
        wall_source="vendor-drawing-clues",
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


def test_values_the_reviewer_typed_are_not_readings_and_never_need_the_witness(
    session: Session, tmp_path: Path
) -> None:
    """#1107 (3). Input: a row whose unsealed readings carry the flag, every width typed by the
    reviewer for that row. Outcome: an automatic PASS — typed values are not readings."""
    project_id, package_id, anchors = _package_rows(
        session,
        unsealed_all=True,
        wall_source="vendor-drawing-clues",
        slot_flags=lambda _page, _offset, _slot: [NO_WITNESS],
    )
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
        SlotRowReviewIn(measurements={"countertop_width": "22 in", "piece_widths:0": "20 in"}),
    )

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
