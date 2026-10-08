"""Reviewer decisions for a slot-reader row never carry to another row."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.api.slot_rows import SlotRowReviewIn, list_slot_rows, review_slot_row
from app.auth.roles import Principal, Role
from app.db.session import session_factory
from app.models import (
    Document,
    DocumentVersion,
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
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_stages import _publish_rulebook

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
    session: Session, *, unsealed_page: int | None = None
) -> tuple[UUID, UUID, dict[int, UUID]]:
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
    )
    session.add(extraction)
    session.flush()

    anchors: dict[int, UUID] = {}
    for page_index, rank in ((0, "1"), (1, "2")):
        row_candidates: list[ObservationCandidate] = []
        for slot, field, numerator in (
            ("0", "SHOP:countertop_piece_width", 20 + page_index),
            ("overall", "SHOP:countertop_overall_width", 40 + page_index),
        ):
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
                    "row-slot-count:1",
                    "ink:vendor",
                ],
                corroboration_status=(
                    None if page_index == unsealed_page and slot == "0" else "CORROBORATED"
                ),
                corroboration_lane=(
                    None if page_index == unsealed_page and slot == "0" else "SECOND_READER"
                ),
            )
            session.add(candidate)
            session.flush()
            if slot == "0":
                anchors[page_index] = candidate.id
            row_candidates.append(candidate)
            session.add(
                MeasurementProposal(
                    package_revision_id=revision.id,
                    page_number=page_index + 1,
                    proposal_id=uuid4(),
                    field_key=field,
                    position=0,
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
                "wall-source:readers",
            ],
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
