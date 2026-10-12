"""Each wall choice's result on the review API, the results line, and the field cut (#1202).

Synthetic rows only (`tests/api/test_slot_rows.py`'s builder): page 1's row has pieces of 20" and
21" (41") and a 42" overall, so with GV's 1" field cut it is right with a wall at one end (42") and
wrong with no end walls (41") or walls at both ends (43").
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.slot_rows import SlotRowReviewIn, list_slot_rows, review_slot_row
from app.api.visual_countertops import _countertop_results_for_revision
from app.auth.roles import Principal, Role
from app.db.session import session_factory
from app.models import ObservationCandidate, PackageRevision
from app.review.wall_cases import NO_CHECK_RUN, NOT_ALL_SEALED, SAVED_AFTER_RUN, WALLS_ALREADY_USED
from tests.api.test_slot_rows import _package_rows, _reader_support, _run_current_checks
from tests.app.postgres_fixture import alembic_config

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


def _sealed(session: Session, **options: object) -> tuple[UUID, UUID, dict[int, UUID]]:
    project_id, package_id, anchors = _package_rows(
        session, piece_count=2, widths_add_up=True, **options  # type: ignore[arg-type]
    )
    for candidate in session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.ambiguity_flags.contains(["slot-reader"])
        )
    ).all():
        _reader_support(session, candidate)
    return project_id, package_id, anchors


def _principal(project_id: UUID) -> Principal:
    return Principal(
        id="synthetic reviewer", roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
    )


def _slot_row(session: Session, project_id: UUID, package_id: UUID, row_id: UUID):  # type: ignore[no-untyped-def]
    rows = list_slot_rows(_principal(project_id), session, project_id, package_id).rows
    return next(row for row in rows if row.row_id == row_id)


def _result(session: Session, package_id: UUID, row_id: UUID):  # type: ignore[no-untyped-def]
    revision = session.query(PackageRevision).filter_by(package_id=package_id).one()
    results = _countertop_results_for_revision(session, package_id, revision)
    return next(item for item in results.items if item.row_id == row_id)


def test_each_wall_choice_shows_its_needed_overall_and_result_with_nothing_chosen(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id, anchors = _sealed(session, wall_source="readers", field_cuts=1)
    findings = _run_current_checks(session, package_id, tmp_path)
    finding = next(item for item in findings if item.scope_row_candidate_id == anchors[0])
    assert finding.outcome == "REVIEW_REQUIRED"

    row = _slot_row(session, project_id, package_id, anchors[0])
    assert row.walls_needed is True
    assert row.wall_config is None and row.decision_id is None, "nothing is chosen for the reviewer"
    cases = {case.layout: case for case in row.wall_cases.cases}
    assert list(cases) == [
        "back_left_right",
        "back_and_left",
        "back_and_right",
        "back_only",
        "island",
    ]
    assert (cases["back_left_right"].expected_total.display, cases["back_left_right"].outcome) == (
        '43"',
        "FAIL",
    )
    assert (cases["back_and_left"].expected_total.display, cases["back_and_left"].outcome) == (
        '42"',
        "PASS",
    )
    assert (cases["island"].expected_total.display, cases["island"].outcome) == ('41"', "FAIL")
    assert cases["back_left_right"].words == "walls at both ends"
    assert cases["back_left_right"].field_cut_count == 2
    assert cases["back_left_right"].field_cut_total.display == '2"'
    assert row.wall_cases.vendor_wrote.display == '42"'
    assert row.wall_cases.pieces_add_up_to.display == '41"'
    assert row.wall_cases.field_cut_per_end.display == '1"'
    assert row.wall_cases.unavailable_reason is None
    assert row.field_cut_in_force == '1"'

    result = _result(session, package_id, anchors[0])
    assert [case.layout for case in result.wall_cases.cases] == list(cases)
    assert result.wall_layout.line is None, "the walls are still open"
    assert result.field_cut_per_end is not None and result.field_cut_per_end.display == '1"'
    assert result.field_cut_in_force == '1"'


def test_wrong_for_every_wall_case_needs_no_walls_and_says_so(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id, anchors = _sealed(
        session, wall_source="readers", field_cuts=1, overall_override=50
    )
    findings = _run_current_checks(session, package_id, tmp_path)
    finding = next(item for item in findings if item.scope_row_candidate_id == anchors[0])
    assert finding.outcome == "FAIL"
    assert finding.reason == (
        'Wrong whatever the walls: it should be 41", 42" or 43" depending on the walls; '
        'the vendor wrote 50".'
    )
    assert finding.variant is None

    row = _slot_row(session, project_id, package_id, anchors[0])
    assert row.walls_needed is False
    result = _result(session, package_id, anchors[0])
    assert result.wall_layout.source == "not needed"
    assert result.wall_layout.line == "not needed: wrong for every wall case"
    assert result.outcome is not None and result.outcome.value == "FAIL"


def test_automatic_walls_say_who_decided_and_the_choices_say_the_walls_are_used(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id, anchors = _sealed(session, wall_source="code-ink-and-both-ais")
    findings = _run_current_checks(session, package_id, tmp_path)
    finding = next(item for item in findings if item.scope_row_candidate_id == anchors[0])
    assert finding.outcome == "PASS", finding.reason

    result = _result(session, package_id, anchors[0])
    assert result.wall_layout.source == "code ink and both AIs"
    assert result.wall_layout.line == (
        "walls at both ends — seen on the drawing at both ends and confirmed by both AIs"
    )
    row = _slot_row(session, project_id, package_id, anchors[0])
    assert row.wall_cases.cases == ()
    assert row.wall_cases.unavailable_reason == WALLS_ALREADY_USED
    assert row.wall_reason == "Seen on the drawing at both ends and confirmed by both AIs."


def test_choices_say_why_they_carry_no_result(session: Session, tmp_path: Path) -> None:
    project_id, package_id, anchors = _sealed(session, wall_source="readers", held_page=1)
    before = _slot_row(session, project_id, package_id, anchors[0])
    assert before.wall_cases.cases == ()
    assert before.wall_cases.unavailable_reason == NO_CHECK_RUN

    _run_current_checks(session, package_id, tmp_path)
    held = _slot_row(session, project_id, package_id, anchors[1])
    assert held.wall_cases.unavailable_reason == NOT_ALL_SEALED

    review_slot_row(
        _principal(project_id),
        _principal(project_id),
        session,
        project_id,
        package_id,
        anchors[0],
        SlotRowReviewIn(wall_config="back_only"),
    )
    after = _slot_row(session, project_id, package_id, anchors[0])
    assert after.wall_cases.cases == ()
    assert after.wall_cases.unavailable_reason == SAVED_AFTER_RUN
    assert after.walls_needed is True


def test_the_reviewers_choice_is_named_in_the_results_line(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id, anchors = _sealed(session, wall_source="readers", field_cuts=1)
    review_slot_row(
        _principal(project_id),
        _principal(project_id),
        session,
        project_id,
        package_id,
        anchors[0],
        SlotRowReviewIn(wall_config="back_and_left"),
    )
    findings = _run_current_checks(session, package_id, tmp_path)
    finding = next(item for item in findings if item.scope_row_candidate_id == anchors[0])
    assert finding.outcome == "PASS", finding.reason
    result = _result(session, package_id, anchors[0])
    assert result.wall_layout.line == "wall at the left end — chosen by synthetic reviewer"
    assert result.field_cut_count == 1
