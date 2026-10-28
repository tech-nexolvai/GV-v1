"""Structured, read-only data contracts for the visual reviewer."""

from __future__ import annotations

from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_session
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.main import API_PREFIX, create_app
from app.models import ModelInvocation, Package, PackageRevision
from app.review.approval import approval_readiness
from tests.api.test_slot_rows import (
    _confirm_row_readings,
    _package_rows,
    _run_current_checks,
)
from tests.app.countertop_results import countertop_results
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
    """A person confirms the row's held readings (#1339): the AI read `overall` and a 20" piece.
    Widths are never typed for a row any more."""
    _ = (project_id, package_id)
    confirmed = _confirm_row_readings(session, row_id, actor="reviewer")
    values = sorted(
        Fraction(observation.value_numerator, observation.value_denominator)
        for observation in confirmed
    )
    assert values == sorted([Fraction(overall), Fraction(20)]), values


def test_checked_countertop_values_are_the_recorded_exact_inputs(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id, anchors = _package_rows(
        session, unsealed_all=True, wall_source="code-ink-and-both-ais"
    )
    _save_widths(session, project_id, package_id, anchors[0], 40)
    findings = _run_current_checks(session, package_id, tmp_path)
    finding = next(row for row in findings if row.scope_row_candidate_id == anchors[0])

    response = countertop_results(session, project_id, package_id)
    assert response.status_code == 200, response.text
    item = next(row for row in response.json()["items"] if row["row_id"] == str(anchors[0]))
    assert item["finding_id"] == str(finding.id)
    assert item["outcome"] == finding.outcome
    assert item["delta"]["numerator"] == "18"
    assert item["delta"]["denominator"] == "1"
    assert item["printed_overall"]["display"] == '40"'
    assert item["pieces"][0]["value"]["display"] == '20"'
    assert item["expected_total"]["display"] == '22"'
    # #1399: what the pieces add up to is the check's own recorded row-total step.
    assert item["pieces_add_up_to"]["display"] == '20"'
    assert item["field_cut_per_end"]["display"] == '1"'
    assert item["field_cut_count"] == 2
    # #1202: code's wall ink at both ends with both AIs, said in the results line.
    assert item["wall_layout"]["source"] == "code ink and both AIs"
    assert item["wall_layout"]["chosen_by_reviewer"] is False
    assert item["wall_layout"]["line"] == (
        "walls at both ends — seen on the drawing at both ends and confirmed by both AIs"
    )
    revision = session.query(PackageRevision).filter_by(package_id=package_id).one()
    readiness = approval_readiness(session, revision.id)
    assert item["needs_decision"] == (finding.id in readiness.blocking_finding_ids)


def test_drawing_clue_pass_uses_the_recorded_zero_delta(session: Session, tmp_path: Path) -> None:
    project_id, package_id, anchors = _package_rows(
        session, unsealed_all=True, wall_source="readers", walls_confirmed=True, widths_add_up=True
    )
    _save_widths(session, project_id, package_id, anchors[0], 22)
    findings = _run_current_checks(session, package_id, tmp_path)
    finding = next(row for row in findings if row.scope_row_candidate_id == anchors[0])
    response = countertop_results(session, project_id, package_id)
    item = next(row for row in response.json()["items"] if row["row_id"] == str(anchors[0]))
    assert finding.outcome == "PASS"
    assert item["outcome"] == "PASS"
    assert item["expected_total"]["display"] == '22"'
    assert item["delta"]["display"] == '0"'


def test_project_boundary_hides_summary_and_usage(session: Session) -> None:
    project_id, _package_id, _ = _package_rows(session)
    other_project = uuid4()
    client = _client(session, project_id)
    summary = client.get(f"{API_PREFIX}/projects/{other_project}/packages-summary")
    usage = client.get(f"{API_PREFIX}/projects/{other_project}/usage")
    assert summary.status_code == 404
    assert usage.status_code == 404


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
        response = countertop_results(session, project_id, package_id)
    finally:
        event.remove(session.bind, "before_cursor_execute", count_statements)
    assert response.status_code == 200
    assert len(response.json()["items"]) == 20
    # 14 since #1202: the field cut a run would use now is asked once (a second statement only
    # when no stored setting holds it and the rulebook's default is read); same for any row count.
    # 15 since #1241: a reviewer's wall answer an earlier reading could not carry is asked for
    # once; still the same for any number of rows.
    # 16 since #1245: the live countertops are asked for once, to group each row's
    # results under its countertop; still the same for any number of rows.
    assert statements <= 16


def test_a_cabinet_package_s_results_cost_one_statement_more_at_most(session: Session) -> None:
    """#1205: the product comes with the revision's own query, and only a cabinet package asks
    whether its reading was skipped, once, whatever the row count."""
    from app.models import Package

    project_id, package_id, _ = _package_rows(session, rows_per_page=10)
    package = session.get(Package, package_id)
    assert package is not None
    package.product_type = "cabinet"
    session.commit()
    session.expire_all()
    statements = 0

    def count_statements(*_args: object) -> None:
        nonlocal statements
        statements += 1

    event.listen(session.bind, "before_cursor_execute", count_statements)
    try:
        response = countertop_results(session, project_id, package_id)
    finally:
        event.remove(session.bind, "before_cursor_execute", count_statements)
    assert response.status_code == 200
    body = response.json()
    assert (body["product_type"], body["countertops_checked"]) == ("cabinet", False)
    assert body["reading_note"] == "Cabinet checks use the cabinet lists on Measurements."
    assert statements <= 15


def test_held_countertop_has_no_recomputed_total_or_delta(session: Session, tmp_path: Path) -> None:
    project_id, package_id, anchors = _package_rows(
        session, wall_source="code-ink-and-both-ais", held_page=0
    )
    _run_current_checks(session, package_id, tmp_path)
    response = countertop_results(session, project_id, package_id)
    item = next(row for row in response.json()["items"] if row["row_id"] == str(anchors[0]))
    assert item["hold"] is not None
    assert item["printed_overall"] is not None
    assert item["expected_total"] is None
    assert item["delta"] is None
    # #1399: a held row recorded no sum, and none is added up for it.
    assert item["pieces_add_up_to"] is None
