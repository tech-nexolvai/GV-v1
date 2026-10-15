"""A typed width or length of zero or less is refused on every path a person types one (#1182).

The audit (2026-10-11) proved a row `0 | 21 | 18` passing against 39: a `0` typed for a piece nobody
could read made the sum come out right. The width check itself now holds such a piece for the
reviewer (`tests/rules/test_ct1_width.py`); these tests prove the screen's three ways in refuse it
first, with a plain message, and that settings where zero is a real answer are untouched.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_session
from app.api.review import _corrected_inches
from app.api.slot_rows import TYPED_WIDTH_CLOSED, SlotRowReviewIn, review_slot_row
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models import Package, PackageRevision, PackageState, Project, SlotRowReviewDecision
from app.models.parameters import ParameterValue
from app.review.sizes import NOT_ABOVE_ZERO
from tests.api.test_slot_rows import _package_rows
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


# ---------------------------------------------------------------------------
# A slot-reader row's widths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("typed", ["0 in", '0"', "-1 in", '-1/16"'])
def test_a_rows_width_of_zero_or_less_is_refused_and_nothing_saved(
    session: Session, typed: str
) -> None:
    project_id, package_id, anchors = _package_rows(session, unsealed_all=True)
    principal = Principal(
        id="synthetic reviewer", roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
    )

    with pytest.raises(HTTPException) as refused:
        review_slot_row(
            principal,
            principal,
            session,
            project_id,
            package_id,
            anchors[0],
            SlotRowReviewIn(measurements={"countertop_width": "40 in", "piece_widths:0": typed}),
        )

    assert refused.value.status_code == 422
    # Since #1339 no width is typed for a row at all (confirm-only); zero or less never was saved.
    assert refused.value.detail == TYPED_WIDTH_CLOSED
    assert session.scalar(select(func.count()).select_from(SlotRowReviewDecision)) == 0


def test_a_rows_positive_typed_widths_are_refused_too(session: Session) -> None:
    """#1339 (confirm-only): a positive typed width is no longer saved either."""
    project_id, package_id, anchors = _package_rows(session, unsealed_all=True)
    principal = Principal(
        id="synthetic reviewer", roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
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
                measurements={"countertop_width": "40 in", "piece_widths:0": "1/16 in"}
            ),
        )

    assert refused.value.status_code == 422
    assert session.scalar(select(func.count()).select_from(SlotRowReviewDecision)) == 0


# ---------------------------------------------------------------------------
# Values saved in Measurements
# ---------------------------------------------------------------------------


def _measurements_client(session: Session) -> tuple[TestClient, str]:
    _publish_rulebook(session)
    project = Project(id=uuid4(), name="widths above zero")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Synthetic vendor", product_type="countertop")
    session.add(package)
    session.flush()
    session.add(
        PackageRevision(
            package_id=package.id, revision_number=1, state=PackageState.AWAITING_REVIEW.value
        )
    )
    session.commit()
    app = create_app(Settings(database_url="postgresql+psycopg://unused/unused"))  # type: ignore[call-arg]
    principal = Principal(
        id="a reviewer", roles=frozenset({Role.REVIEWER}), projects=frozenset({project.id})
    )
    app.dependency_overrides[authenticate] = lambda: principal
    app.dependency_overrides[get_session] = lambda: session
    url = f"/api/v1/projects/{project.id}/packages/{package.id}/measurements"
    return TestClient(app, raise_server_exceptions=False), url


@pytest.mark.parametrize(
    "measurement",
    [
        {"rule_id": "CT-WIDTH-001", "name": "piece_widths", "values": ["21 in", "0 in", "18 in"]},
        {"rule_id": "CAB-FILLER-001", "name": "field_width", "value": "-39 in"},
        {"rule_id": "CT-WIDTH-001", "name": "filler_widths", "values": ["0 in"]},
        {"rule_id": "CAB-FILLER-001", "name": "field_width", "value": "0 mm"},
    ],
    ids=["zero-piece", "negative-site-width", "zero-filler", "zero-site-width-mm"],
)
def test_a_saved_width_of_zero_or_less_is_refused_and_nothing_stored(
    session: Session, measurement: dict[str, Any]
) -> None:
    """Every size a person still types (#1251): the typed lists and the site width."""
    client, url = _measurements_client(session)

    response = client.post(url, json={"measurements": [measurement]})

    assert response.status_code == 422, response.text
    assert response.json()["message"].endswith(NOT_ABOVE_ZERO)
    assert session.scalar(select(func.count()).select_from(ParameterValue)) == 0


@pytest.mark.parametrize(
    "measurement",
    [
        {"rule_id": "CT-WIDTH-001", "name": "countertop_width", "value": "-39 in"},
        {"rule_id": "CT-SINK-CUTOUT-WIDTH-001", "name": "cutout_width", "value": "0 mm"},
    ],
    ids=["negative-overall", "zero-sink-cutout"],
)
def test_a_drawn_width_of_zero_or_less_is_never_stored(
    session: Session, measurement: dict[str, Any]
) -> None:
    """A size drawn on the vendor's drawing is no longer typed at all (#1251): a zero or a negative
    one is refused before its size is looked at, and nothing is stored either way."""
    client, url = _measurements_client(session)

    response = client.post(url, json={"measurements": [measurement]})

    assert response.status_code == 422, response.text
    assert "not typed here" in response.json()["message"]
    assert session.scalar(select(func.count()).select_from(ParameterValue)) == 0


def test_a_setting_of_zero_is_still_a_real_answer(session: Session) -> None:
    """A field cut of zero ("no field cut") is a setting, not a size: still accepted."""
    client, url = _measurements_client(session)

    response = client.post(url, json={"parameters": [{"name": "field_cut", "value": '0"'}]})

    assert response.status_code == 201, response.text


def test_positive_widths_are_still_saved(session: Session) -> None:
    client, url = _measurements_client(session)

    response = client.post(
        url,
        json={
            "measurements": [
                {"rule_id": "CT-WIDTH-001", "name": "piece_widths", "values": ["21 in", "18 in"]}
            ]
        },
    )

    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# A reviewer's correction of a reading
# ---------------------------------------------------------------------------


def _db_with(semantic_type: str) -> Any:
    reading = SimpleNamespace(unit="in", semantic_type=semantic_type)
    return SimpleNamespace(get=lambda _model, _id: reading)


@pytest.mark.parametrize("typed", ['0"', '-2 1/2"'])
def test_correcting_a_width_to_zero_or_less_is_refused(typed: str) -> None:
    with pytest.raises(HTTPException) as refused:
        _corrected_inches(_db_with("countertop_piece_width"), uuid4(), typed)

    assert refused.value.status_code == 422
    assert refused.value.detail == f"corrected_value: {NOT_ABOVE_ZERO}"


def test_correcting_a_value_that_is_not_a_size_to_zero_is_still_allowed() -> None:
    """A clearance or an offset may truly be zero; only sizes of real things are held to it."""
    assert _corrected_inches(_db_with("CT011"), uuid4(), '0"') == 0
