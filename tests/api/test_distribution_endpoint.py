"""Distribution API for Q9/Q21: fillers first, reviewer-chosen cabinet only."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.main import API_PREFIX, create_app

PROJECT = uuid4()
PATH = f"{API_PREFIX}/projects/{PROJECT}/filler-distribution"


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://unused@localhost/unused",
        environment="test",
    )


def _client() -> TestClient:
    app = create_app(_settings())
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="reviewer",
        roles=frozenset({Role.REVIEWER}),
        projects=frozenset({PROJECT}),
    )
    return TestClient(app, raise_server_exceptions=False)


def _payload(**updates: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "assembly": {
            "cabinets": [
                {"id": "CAB-1", "width": '30"'},
                {"id": "CAB-2", "width": '30"'},
            ],
            "fillers": [
                {"id": "F-L", "width": '2"'},
                {"id": "F-R", "width": '2"'},
            ],
        },
        "field_width": '66"',
        "filler_min": '1"',
        "filler_max": '4"',
    }
    payload.update(updates)
    return payload


def test_difference_fillers_can_absorb_returns_proposed_filler_sizes() -> None:
    response = _client().post(PATH, json=_payload())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "PASS"
    assert body["condition"] == "fillers_absorb"
    assert [filler["proposed"]["display"] for filler in body["fillers"]] == ['3"', '3"']
    assert [cabinet["proposed"]["display"] for cabinet in body["cabinets"]] == ['30"', '30"']
    assert body["field_dimension"]["source"] == "USER_INPUT"
    assert body["field_dimension"]["value"]["display"] == '66"'


def test_larger_difference_distributes_to_reviewer_chosen_cabinet() -> None:
    response = _client().post(
        PATH,
        json=_payload(field_width='70"', adjustable_cabinet_id="CAB-2"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "PASS"
    assert body["condition"] == "cabinet_adjusted_by_reviewer_selection"
    assert [filler["proposed"]["display"] for filler in body["fillers"]] == ['4"', '4"']
    assert [cabinet["proposed"]["display"] for cabinet in body["cabinets"]] == ['30"', '32"']
    assert body["selected_adjustable_cabinet_id"] == "CAB-2"
    assert [cabinet["adjustable"] for cabinet in body["cabinets"]] == [False, True]


def test_fillers_insufficient_without_cabinet_choice_abstains() -> None:
    response = _client().post(PATH, json=_payload(field_width='70"'))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "REVIEW_REQUIRED"
    assert body["condition"] == "cabinet_selection_required"
    assert body["selected_adjustable_cabinet_id"] is None
    assert [cabinet["adjustable"] for cabinet in body["cabinets"]] == [False, False]
    assert "system did not pick one" in body["message"]


def test_missing_field_dimension_returns_not_found_with_user_input_source() -> None:
    response = _client().post(PATH, json=_payload(field_width=None))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "NOT_FOUND"
    assert body["condition"] == "field_width_missing"
    assert body["field_dimension"] == {
        "name": "field_width",
        "source": "USER_INPUT",
        "status": "HUMAN_CONFIRMED",
        "value": None,
    }
