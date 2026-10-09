"""Structured, read-only data contracts for the visual reviewer."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_session
from app.api.slot_rows import SlotRowReviewIn, review_slot_row
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.main import API_PREFIX, create_app
from app.models import Approval, ModelInvocation, Package, PackageRevision
from app.review.approval import approval_readiness
from tests.api.test_slot_rows import _package_rows, _run_current_checks, _save_all_row_widths
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


def _client(session: Session, project_id: UUID) -> TestClient:
    app = create_app(Settings(database_url="postgresql+psycopg://gv:gv@localhost:5433/gv"))
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="reviewer",
        roles=frozenset({Role.REVIEWER}),
        projects=frozenset({project_id}),
    )
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _save_widths(
    session: Session, project_id: UUID, package_id: UUID, row_id: UUID, overall: int
) -> None:
    principal = Principal(
        id="reviewer", roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
    )
    review_slot_row(
        principal,
        principal,
        session,
        project_id,
        package_id,
        row_id,
        SlotRowReviewIn(
            measurements={"countertop_width": f"{overall} in", "piece_widths:0": "20 in"}
        ),
    )


def test_checked_countertop_values_are_the_recorded_exact_inputs(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id, anchors = _package_rows(
        session, unsealed_all=True, wall_source="vendor-drawing-clues"
    )
    _save_widths(session, project_id, package_id, anchors[0], 40)
    findings = _run_current_checks(session, package_id, tmp_path)
    finding = next(row for row in findings if row.scope_row_candidate_id == anchors[0])

    response = _client(session, project_id).get(
        f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/countertop-results"
    )
    assert response.status_code == 200, response.text
    item = next(row for row in response.json()["items"] if row["row_id"] == str(anchors[0]))
    assert item["finding_id"] == str(finding.id)
    assert item["outcome"] == finding.outcome
    assert item["delta"]["numerator"] == "18"
    assert item["delta"]["denominator"] == "1"
    assert item["printed_overall"]["display"] == '40"'
    assert item["pieces"][0]["value"]["display"] == '20"'
    assert item["expected_total"]["display"] == '22"'
    assert item["field_cut_per_end"]["display"] == '1"'
    assert item["field_cut_count"] == 2
    assert item["wall_layout"]["source"] == "drawing clues"
    revision = session.query(PackageRevision).filter_by(package_id=package_id).one()
    readiness = approval_readiness(session, revision.id)
    assert item["needs_decision"] == (finding.id in readiness.blocking_finding_ids)


def test_drawing_clue_pass_uses_the_recorded_zero_delta(session: Session, tmp_path: Path) -> None:
    project_id, package_id, anchors = _package_rows(
        session, unsealed_all=True, wall_source="vendor-drawing-clues", widths_add_up=True
    )
    _save_widths(session, project_id, package_id, anchors[0], 22)
    findings = _run_current_checks(session, package_id, tmp_path)
    finding = next(row for row in findings if row.scope_row_candidate_id == anchors[0])
    response = _client(session, project_id).get(
        f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/countertop-results"
    )
    item = next(row for row in response.json()["items"] if row["row_id"] == str(anchors[0]))
    assert finding.outcome == "PASS"
    assert item["outcome"] == "PASS"
    assert item["expected_total"]["display"] == '22"'
    assert item["delta"]["display"] == '0"'


def test_between_panels_pass_requires_row_confirmation_and_has_no_end_cut(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id, anchors = _package_rows(
        session,
        unsealed_all=True,
        check_hold_page=0,
        wall_source="vendor-drawing-clues",
        overall_override=20,
    )
    principal = Principal(
        id="reviewer", roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
    )
    review_slot_row(
        principal,
        principal,
        session,
        project_id,
        package_id,
        anchors[0],
        SlotRowReviewIn(wall_config="back_only"),
    )
    _save_widths(session, project_id, package_id, anchors[0], 20)
    findings = _run_current_checks(session, package_id, tmp_path)
    finding = next(row for row in findings if row.scope_row_candidate_id == anchors[0])
    item = next(
        row
        for row in _client(session, project_id)
        .get(f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/countertop-results")
        .json()["items"]
        if row["row_id"] == str(anchors[0])
    )
    assert finding.outcome == "PASS"
    assert item["wall_layout"]["source"] == "between panels"
    # #1138: said as "no field cut", never "back wall only": nothing was said about the walls.
    assert item["wall_layout"]["label"] == "no field cut: the stone stops at panels"
    assert item["field_cut_count"] == 0
    assert item["expected_total"]["display"] == '20"'


def test_a_wall_at_one_end_is_labelled_and_takes_one_field_cut(
    session: Session, tmp_path: Path
) -> None:
    """#1138: the countertop results name the layout in words and need one inch, not two."""
    project_id, package_id, anchors = _package_rows(
        session, unsealed_all=True, wall_source="readers", sealed_layout="back_and_left"
    )
    principal = Principal(
        id="reviewer", roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
    )
    review_slot_row(
        principal,
        principal,
        session,
        project_id,
        package_id,
        anchors[0],
        SlotRowReviewIn(wall_config="back_and_left"),
    )
    _save_widths(session, project_id, package_id, anchors[0], 21)
    findings = _run_current_checks(session, package_id, tmp_path)
    finding = next(row for row in findings if row.scope_row_candidate_id == anchors[0])
    item = next(
        row
        for row in _client(session, project_id)
        .get(f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/countertop-results")
        .json()["items"]
        if row["row_id"] == str(anchors[0])
    )
    assert finding.outcome == "PASS", finding.reason
    assert item["wall_layout"] == {
        "config": "back_and_left",
        "label": "back wall and left end",
        "source": "reviewer",
    }
    assert item["field_cut_count"] == 1
    assert item["expected_total"]["display"] == '21"'


def test_project_boundary_hides_a_package_from_another_project(session: Session) -> None:
    _project_id, package_id, _ = _package_rows(session)
    other = uuid4()
    response = _client(session, other).get(
        f"{API_PREFIX}/projects/{other}/packages/{package_id}/countertop-results"
    )
    assert response.status_code == 404


def test_project_boundary_hides_summary_and_usage(session: Session) -> None:
    project_id, package_id, _ = _package_rows(session)
    other_project = uuid4()
    client = _client(session, project_id)
    summary = client.get(f"{API_PREFIX}/projects/{other_project}/packages-summary")
    usage = client.get(f"{API_PREFIX}/projects/{other_project}/usage")
    countertop = client.get(
        f"{API_PREFIX}/projects/{other_project}/packages/{package_id}/countertop-results"
    )
    assert summary.status_code == 404
    assert usage.status_code == 404
    assert countertop.status_code == 404


def test_summary_counts_match_existing_findings_summary(session: Session, tmp_path: Path) -> None:
    project_id, package_id, anchors = _package_rows(
        session, unsealed_all=True, wall_source="vendor-drawing-clues"
    )
    _save_all_row_widths(session, project_id, package_id, anchors[0])
    _run_current_checks(session, package_id, tmp_path)
    revision = session.query(PackageRevision).filter_by(package_id=package_id).one()
    # Historical approvals may coexist with a re-review. The summary's joins must not multiply
    # findings by the number of approval/export rows.
    session.add_all(
        [
            Approval(package_revision_id=revision.id, approved_by="reviewer-a"),
            Approval(package_revision_id=revision.id, approved_by="reviewer-b"),
        ]
    )
    session.flush()
    client = _client(session, project_id)
    legacy = client.get(
        f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/findings/summary"
    )
    summary = client.get(f"{API_PREFIX}/projects/{project_id}/packages-summary?limit=1")
    assert summary.status_code == 200, summary.text
    item = next(row for row in summary.json()["items"] if row["package_id"] == str(package_id))
    counts = item["outcomes"]
    assert counts["pass"] == legacy.json()["passed"]
    assert counts["fail"] == legacy.json()["failed"]
    assert counts["review"] == legacy.json()["review_required"]
    assert counts["not_found"] == legacy.json()["not_found"]
    assert counts["no_rule"] == legacy.json()["no_applicable_rule"]
    assert item["needs_decision"] == approval_readiness(session, revision.id).blocking_findings


def test_usage_totals_match_invocations_and_never_expose_raw_answers(session: Session) -> None:
    project_id, package_id, _ = _package_rows(session)
    revision = session.query(PackageRevision).filter_by(package_id=package_id).one()
    session.add_all(
        [
            ModelInvocation(
                package_revision_id=revision.id,
                extraction_run_id=None,
                model_id="synthetic-model-a",
                prompt_id="synthetic-prompt",
                template_id="synthetic-template",
                input_tokens=7,
                output_tokens=3,
                cost_micros=12500,
                latency_ms=100,
                outcome="ok",
                private_raw_response="never expose this",
            ),
            ModelInvocation(
                package_revision_id=revision.id,
                extraction_run_id=None,
                model_id="synthetic-model-a",
                prompt_id="synthetic-prompt",
                template_id="synthetic-template",
                input_tokens=4,
                output_tokens=2,
                cost_micros=None,
                latency_ms=100,
                outcome="failed",
                private_raw_response="also private",
            ),
        ]
    )
    session.flush()
    response = _client(session, project_id).get(
        f"{API_PREFIX}/projects/{project_id}/usage?group_by=package"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["totals"] == {
        "calls": 2,
        "failed_calls": 1,
        "input_tokens": 11,
        "output_tokens": 5,
        "cost_usd": "0.012500",
        "unpriced_calls": 1,
    }
    assert body["groups"][0]["package_id"] == str(package_id)
    assert "private_raw_response" not in response.text


def test_packages_summary_cursor_advances_without_repeating(session: Session) -> None:
    project_id, _first_package, _ = _package_rows(session)
    second_package = Package(
        project_id=project_id, vendor="Synthetic second", product_type="countertop"
    )
    session.add(second_package)
    session.flush()
    session.add(PackageRevision(package_id=second_package.id, revision_number=1, state="CREATED"))
    session.flush()
    client = _client(session, project_id)
    first = client.get(f"{API_PREFIX}/projects/{project_id}/packages-summary?limit=1").json()
    assert first["next_cursor"] is not None
    second = client.get(
        f"{API_PREFIX}/projects/{project_id}/packages-summary?limit=1&cursor={first['next_cursor']}"
    ).json()
    first_ids = {item["package_id"] for item in first["items"]}
    second_ids = {item["package_id"] for item in second["items"]}
    assert len(first_ids | second_ids) == 2
    assert not first_ids & second_ids


def test_countertop_result_query_count_stays_bounded_for_twenty_rows(
    session: Session,
) -> None:
    project_id, package_id, _ = _package_rows(session, rows_per_page=10)
    statements = 0

    def count_statements(*_args: object) -> None:
        nonlocal statements
        statements += 1

    event.listen(session.bind, "before_cursor_execute", count_statements)
    try:
        response = _client(session, project_id).get(
            f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/countertop-results"
        )
    finally:
        event.remove(session.bind, "before_cursor_execute", count_statements)
    assert response.status_code == 200
    assert len(response.json()["items"]) == 20
    assert statements <= 12


def test_held_countertop_has_no_recomputed_total_or_delta(session: Session, tmp_path: Path) -> None:
    project_id, package_id, anchors = _package_rows(
        session, wall_source="vendor-drawing-clues", held_page=0
    )
    _run_current_checks(session, package_id, tmp_path)
    response = _client(session, project_id).get(
        f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/countertop-results"
    )
    item = next(row for row in response.json()["items"] if row["row_id"] == str(anchors[0]))
    assert item["hold"] is not None
    assert item["printed_overall"] is not None
    assert item["expected_total"] is None
    assert item["delta"] is None
