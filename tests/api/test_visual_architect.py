"""The countertop results' architect block: what the vendor-vs-architect check recorded (#1054).

Values come from the recorded check, never recomputed; the difference is the vendor's value minus
the architect's, exactly. A row the check wrote nothing for says why. Synthetic values only.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.api import visual_countertops
from app.api.dependencies import get_session
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.main import API_PREFIX, create_app
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_architect_row_evidence import (
    ARCH_RULE,
    _architect_drawing,
    _architect_value,
    _overall,
    _pairing,
    _run,
    _sealed_rows,
)
from workflow.architect_pairing_contract import EffectivePairing, PairingSource

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


def _project_of(session: Session, package_id: UUID) -> UUID:
    from app.models import Package

    return session.get_one(Package, package_id).project_id


def _items(session: Session, package_id: UUID, project_id: UUID | None = None) -> dict[str, dict]:
    project = _project_of(session, package_id) if project_id is None else project_id
    app = create_app(Settings(database_url="postgresql+psycopg://gv:gv@localhost:5433/gv"))
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="reviewer", roles=frozenset({Role.REVIEWER}), projects=frozenset({project})
    )
    app.dependency_overrides[get_session] = lambda: session
    response = TestClient(app).get(
        f"{API_PREFIX}/projects/{project}/packages/{package_id}/countertop-results"
    )
    assert response.status_code == 200, response.text
    return {item["row_id"]: item for item in response.json()["items"]}


def test_a_compared_row_shows_both_values_and_the_exact_difference(
    session: Session, tmp_path: Path
) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 7 1/2\"")
    findings = _run(session, package_id, tmp_path, {anchors[0]: _pairing(_overall(overall))})
    finding = findings[ARCH_RULE][anchors[0]]
    width = findings["CT-WIDTH-001"][anchors[0]]

    item = _items(session, package_id)[str(anchors[0])]

    # The width check's own fields are untouched.
    assert item["finding_id"] == str(width.id)
    assert item["outcome"] == width.outcome == "PASS"
    block = item["architect"]
    assert block["finding_id"] == str(finding.id)
    assert block["outcome"] == "FAIL"
    assert block["needs_decision"] is True
    assert block["not_compared_reason"] is None
    assert block["pairing_source"] == "code+ais"
    assert block["pairing_judgments"] == "code and both AIs"
    # Where the architect's dimension is (#1066, `tests/api/test_architect_locations.py`).
    location = block["compared"][0].pop("architect_location")
    assert location["page_id"] == str(overall.page_id)
    assert location["coordinate_space"] == "stored"
    assert block["compared"] == [
        {
            "kind": "overall",
            "vendor_piece": None,
            "vendor": {"numerator": "43", "denominator": "1", "display": '43"'},
            "architect": {"numerator": "87", "denominator": "2", "display": '43 1/2"'},
            "delta": {"numerator": "-1", "denominator": "2", "display": '-1/2"'},
            "vendor_display": '43"',
            "architect_display": '43 1/2"',
            "delta_display": '-1/2"',
            "outcome": "FAIL",
        }
    ]


@pytest.mark.parametrize(
    ("source", "judgments", "who"),
    [("both-ais", "both AIs only", "the two AIs"), ("code", "code only", "code")],
)
@pytest.mark.parametrize("printed", ["3' - 7\"", "3' - 6\""])
def test_a_one_judgment_result_is_shown_as_waiting_for_the_reviewer(
    session: Session,
    tmp_path: Path,
    source: PairingSource,
    judgments: str,
    who: str,
    printed: str,
) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], printed)
    _run(
        session,
        package_id,
        tmp_path,
        {anchors[0]: _pairing(_overall(overall), source=source)},
    )

    block = _items(session, package_id)[str(anchors[0])]["architect"]

    assert block["outcome"] == "REVIEW_REQUIRED"
    assert block["needs_decision"] is True
    assert block["reason"] == (
        f"Only {who} paired these; confirm that the architect's {printed} and the vendor's 43\" "
        "measure the same thing."
    )
    assert block["pairing_source"] == source
    assert block["pairing_judgments"] == judgments
    # The numbers stay visible; no pair reads as decided.
    assert [pair["outcome"] for pair in block["compared"]] == ["REVIEW_REQUIRED"]
    assert block["compared"][0]["vendor_display"] == '43"'


def test_a_reviewers_pairing_is_shown_as_the_reviewers(session: Session, tmp_path: Path) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 7\"")
    _run(
        session,
        package_id,
        tmp_path,
        {anchors[0]: _pairing(_overall(overall), source="reviewer", status="reviewer")},
    )

    block = _items(session, package_id)[str(anchors[0])]["architect"]

    assert block["outcome"] == "PASS"
    assert block["pairing_source"] == "reviewer"
    assert block["pairing_judgments"] == "reviewer"


def test_a_row_not_compared_names_what_the_architects_dimensions_measure(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_id, anchors = _sealed_rows(session)
    pairing = _pairing(
        source="none",
        status="none",
        reasons=(),
        measures=((uuid4(), "blocking"), (uuid4(), "fixture_centre")),
    )
    _run(session, package_id, tmp_path, {anchors[0]: pairing})
    monkeypatch.setattr(
        visual_countertops,
        "effective_architect_pairings",
        lambda _session, rows: {
            anchor: pairing if anchor == anchors[0] else None for anchor in rows
        },
    )

    block = _items(session, package_id)[str(anchors[0])]["architect"]

    assert block["finding_id"] is None
    assert block["needs_decision"] is False
    assert block["not_compared_reason"] == (
        "The architect's dimensions on this sheet measure blocking and fixture centres, not the "
        "countertop. No architect dimension is paired with this row."
    )
    assert block["pairing_source"] == "none"
    assert block["pairing_judgments"] is None


def test_a_row_with_nothing_compared_says_why_and_needs_no_decision(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_id, anchors = _sealed_rows(session)
    pairing = _pairing(
        status="nothing_comparable",
        reasons=("every architect span ends on a fixture's centre line",),
    )
    _run(session, package_id, tmp_path, {anchors[0]: pairing})

    def lookup(_session: Session, rows: Collection[UUID]) -> dict[UUID, EffectivePairing | None]:
        return {anchor: pairing if anchor == anchors[0] else None for anchor in rows}

    monkeypatch.setattr(visual_countertops, "effective_architect_pairings", lookup)
    items = _items(session, package_id)

    first = items[str(anchors[0])]["architect"]
    assert first["finding_id"] is None
    assert first["outcome"] is None
    assert first["needs_decision"] is False
    assert first["compared"] == []
    assert first["not_compared_reason"] == "every architect span ends on a fixture's centre line."
    assert first["pairing_source"] == "code+ais"
    assert first["pairing_judgments"] == "code and both AIs"
    second = items[str(anchors[1])]["architect"]
    assert second["not_compared_reason"] == "No architect dimension is paired with this row."
    assert second["pairing_source"] is None


def test_a_pairing_made_after_the_last_run_asks_for_a_run(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 7\"")
    _run(session, package_id, tmp_path)
    pairing = _pairing(_overall(overall))
    monkeypatch.setattr(
        visual_countertops,
        "effective_architect_pairings",
        lambda _session, rows: {
            anchor: pairing if anchor == anchors[0] else None for anchor in rows
        },
    )

    block = _items(session, package_id)[str(anchors[0])]["architect"]

    assert block["finding_id"] is None
    assert block["not_compared_reason"] == visual_countertops.NOT_CHECKED_YET


def test_another_projects_reviewer_sees_nothing(session: Session, tmp_path: Path) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 7\"")
    _run(session, package_id, tmp_path, {anchors[0]: _pairing(_overall(overall))})
    other = uuid4()
    app = create_app(Settings(database_url="postgresql+psycopg://gv:gv@localhost:5433/gv"))
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="reviewer", roles=frozenset({Role.REVIEWER}), projects=frozenset({other})
    )
    app.dependency_overrides[get_session] = lambda: session

    response = TestClient(app).get(
        f"{API_PREFIX}/projects/{other}/packages/{package_id}/countertop-results"
    )

    assert response.status_code == 404
