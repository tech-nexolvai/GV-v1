"""A cabinet run's equipment marks on the simple review list (#648, Step 2).

The reviewer says which cabinets keep their width (equipment: sink, microwave, fridge, other) and
which can be resized (single door, double door, drawer). The list shows one `cabinet_kinds` item per
cabinet run check waiting on a person, each cabinet a row; the answer records the marks through the
existing classification path (`workflow/classifications.py:record_classifications`, what
`CAB-FILLER-001` reads) and queues one coalesced re-check. The per-type width limits it lacks are
listed with their Company settings key, never guessed.

Every number and name is synthetic.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.models import PackageRevision
from tests.api.test_waiting_values import PROJECT, _client, _package, _run, _upgrade
from tests.workflow.test_stages import _publish_rulebook
from workflow.classifications import confirmed_classifications

pytest_plugins = ("tests.app.postgres_fixture",)

BOUNDS = [
    "double_door_cab_width_max",
    "double_door_cab_width_min",
    "drawer_cab_width_max",
    "drawer_cab_width_min",
    "single_door_cab_width_max",
    "single_door_cab_width_min",
]


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    _publish_rulebook(opened)
    opened.commit()
    try:
        yield opened
    finally:
        opened.close()


def _lists(client: Any, package: UUID) -> None:
    response = client.post(
        f"/api/v1/projects/{PROJECT}/packages/{package}/measurements",
        json={
            "measurements": [
                {
                    "rule_id": "CAB-FILLER-001",
                    "name": "architectural_cabinets",
                    "values": ['24"', '36"', '24"'],
                },
                {
                    "rule_id": "CAB-FILLER-001",
                    "name": "shop_cabinets",
                    "values": ['24"', '36"', '23 1/2"'],
                },
            ]
        },
    )
    assert response.status_code == 201, response.text


def _listed(client: Any, package: UUID) -> dict[str, Any]:
    response = client.get(f"/api/v1/projects/{PROJECT}/packages/{package}/review-items")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _cabinet_item(listed: dict[str, Any]) -> dict[str, Any]:
    found: list[dict[str, Any]] = [
        item for item in listed["items"] if item["kind"] == "cabinet_kinds"
    ]
    assert len(found) == 1, listed["items"]
    return found[0]


def _ready(session: Session) -> tuple[Any, UUID, UUID]:
    client = _client(session)
    package, revision = _package(session, product="cabinet")
    _lists(client, package)
    _run(session, revision)
    return client, package, revision


def test_a_cabinet_run_waiting_on_a_person_lists_each_cabinet_to_mark(session: Session) -> None:
    client, package, _revision = _ready(session)

    listed = _listed(client, package)
    item = _cabinet_item(listed)

    assert listed["unaccounted"] == []
    assert item["key"] == f"cabinets:{item['finding_id']}"
    assert item["status"] == "open"
    assert item["actions"] == ["mark_cabinets"]
    assert [
        (row["position"], row["architect_width"], row["vendor_width"], row["marked"])
        for row in item["cabinets"]
    ] == [(1, '24"', '24"', None), (2, '36"', '36"', None), (3, '24"', '23 1/2"', None)]
    keeps = {choice["category"]: choice["keeps_width"] for choice in item["cabinet_choices"]}
    assert keeps["sink_cabinet"] is True and keeps["equipment"] is True
    assert keeps["single_door"] is False and keeps["drawer"] is False
    # The per-type limits have no company value: listed by their settings key, never guessed.
    assert sorted(setting["key"] for setting in item["settings"]) == BOUNDS
    assert all(setting["project_id"] == str(PROJECT) for setting in item["settings"])
    # Not listed a second time as a generic result.
    assert not [
        entry
        for entry in listed["items"]
        if entry["kind"] == "other_result" and entry["finding_id"] == item["finding_id"]
    ]


def test_saving_the_marks_records_them_where_the_check_reads_and_rechecks_once(
    session: Session,
) -> None:
    client, package, revision = _ready(session)
    item = _cabinet_item(_listed(client, package))

    response = client.post(
        f"/api/v1/projects/{PROJECT}/packages/{package}/review-items/{item['key']}/answer",
        json={
            "answer": "mark_cabinets",
            "expected_finding_id": item["finding_id"],
            "cabinet_kinds": ["single_door", "sink_cabinet", "drawer"],
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["rechecking"] is True
    assert confirmed_classifications(session, revision) == {
        "CAB-FILLER-001": {"cabinet_type": ("single_door", "sink_cabinet", "drawer")}
    }
    after = _cabinet_item(body["review_items"])
    assert after["status"] == "rechecking"
    assert after["actions"] == []
    assert [row["marked"] for row in after["cabinets"]] == ["single_door", "sink_cabinet", "drawer"]
    assert [row["keeps_width"] for row in after["cabinets"]] == [False, True, False]

    # The re-check runs: the marks are read, and the item comes back with them pre-filled.
    _run(session, revision)
    again = _cabinet_item(_listed(client, package))
    assert again["status"] == "open"
    assert [row["marked"] for row in again["cabinets"]] == ["single_door", "sink_cabinet", "drawer"]


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"cabinet_kinds": ["single_door", "drawer"]}, 409),
        ({"cabinet_kinds": ["single_door", "toaster", "drawer"]}, 422),
        ({}, 422),
    ],
)
def test_marks_for_another_run_or_an_unknown_kind_save_nothing(
    session: Session, body: dict[str, Any], code: int
) -> None:
    client, package, revision = _ready(session)
    item = _cabinet_item(_listed(client, package))

    response = client.post(
        f"/api/v1/projects/{PROJECT}/packages/{package}/review-items/{item['key']}/answer",
        json={"answer": "mark_cabinets", "expected_finding_id": item["finding_id"], **body},
    )

    assert response.status_code == code, response.text
    session.expire_all()
    assert confirmed_classifications(session, revision) == {}
    assert session.get(PackageRevision, revision) is not None


def test_a_stale_result_saves_nothing(session: Session) -> None:
    client, package, revision = _ready(session)
    item = _cabinet_item(_listed(client, package))
    _run(session, revision)

    response = client.post(
        f"/api/v1/projects/{PROJECT}/packages/{package}/review-items/{item['key']}/answer",
        json={
            "answer": "mark_cabinets",
            "expected_finding_id": item["finding_id"],
            "cabinet_kinds": ["single_door", "sink_cabinet", "drawer"],
        },
    )

    assert response.status_code == 409, response.text
    assert confirmed_classifications(session, revision) == {}


def test_a_run_with_no_cabinet_list_stays_a_result_to_check(session: Session) -> None:
    client = _client(session)
    package, revision = _package(session, product="cabinet")
    _run(session, revision)

    listed = _listed(client, package)

    assert listed["unaccounted"] == []
    assert not [item for item in listed["items"] if item["kind"] == "cabinet_kinds"]
