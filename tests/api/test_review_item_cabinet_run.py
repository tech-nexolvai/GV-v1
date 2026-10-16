"""A cabinet run proposed from the AI's readings and used in one answer (cabinets e2e).

With no run on Measurements, the cabinet check's item on the simple review list is a `cabinet_run`:
the runs proposed from the stored readings (`app/review/cabinet_run_proposal.py`), each piece's
width as two AIs read it. The person says what each piece is (filler, or which kind of cabinet),
enters the site wall-to-wall, and uses the run: the lists, marks and site width are stored where
`CAB-FILLER-001` reads them, with the sealed reading named on each width, and the checks run again.
No width is typed.

Every number and name is synthetic.
"""

from __future__ import annotations

from collections.abc import Iterator
from fractions import Fraction
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.models.parameters import ParameterValue
from app.review.cabinet_run_proposal import NOTHING_READ
from tests.api.test_waiting_values import PROJECT, _client, _package, _run, _upgrade
from tests.review.cabinet_run_fixtures import SAMPLE_RUN, add_reading
from tests.workflow.test_stages import _publish_rulebook
from workflow.classifications import confirmed_classifications
from workflow.measurements import operands_for

pytest_plugins = ("tests.app.postgres_fixture",)

KINDS = ["filler", "single_door", "sink_cabinet", "drawer", "filler"]


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


def _listed(client: Any, package: UUID) -> dict[str, Any]:
    response = client.get(f"/api/v1/projects/{PROJECT}/packages/{package}/review-items")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _items(listed: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [item for item in listed["items"] if item["kind"] == kind]


def _ready(session: Session, *, read: bool = True) -> tuple[Any, UUID, UUID]:
    client = _client(session)
    package, revision = _package(session, product="cabinet")
    if read:
        add_reading(session, package_id=package, revision_id=revision, pieces=SAMPLE_RUN)
    _run(session, revision)
    return client, package, revision


def _use(client: Any, package: UUID, item: dict[str, Any], **run: Any) -> Any:
    body = {
        "row_id": item["cabinet_proposal"]["runs"][0]["row_id"],
        "pieces": KINDS,
        "site_width": '80"',
        **run,
    }
    return client.post(
        f"/api/v1/projects/{PROJECT}/packages/{package}/review-items/{item['key']}/answer",
        json={
            "answer": "use_cabinet_run",
            "expected_finding_id": item["finding_id"],
            "cabinet_run": body,
        },
    )


def _inches(operand: Any) -> list[Fraction]:
    value = operand.value
    return [m.exact for m in value] if isinstance(value, tuple) else [value.exact]


def test_with_no_run_typed_the_cabinet_check_shows_the_proposed_run(session: Session) -> None:
    client, package, _revision = _ready(session)

    listed = _listed(client, package)
    runs = _items(listed, "cabinet_run")

    assert listed["unaccounted"] == []
    assert len(runs) == 1
    item = runs[0]
    assert item["what"] == "Which cabinets keep their width?"
    assert item["actions"] == ["use_cabinet_run"]
    assert item["status"] == "open"
    proposal = item["cabinet_proposal"]
    assert [piece["width"]["display"] for piece in proposal["runs"][0]["pieces"]] == [
        '2"',
        '21"',
        '30"',
        '24"',
        '1 1/2"',
    ]
    # Nothing is pre-marked: no printed word names a piece.
    assert [piece["proposed"] for piece in proposal["runs"][0]["pieces"]] == [None] * 5
    assert proposal["site_width"] is None
    assert {choice["category"] for choice in item["cabinet_choices"]} >= {"sink_cabinet", "drawer"}
    # Not listed again as a generic result, and no typed-list card yet.
    assert _items(listed, "cabinet_kinds") == []
    assert not [
        entry
        for entry in _items(listed, "other_result")
        if entry["finding_id"] == item["finding_id"]
    ]


def test_using_the_run_stores_the_read_widths_marks_and_site_width_and_rechecks(
    session: Session,
) -> None:
    client, package, revision = _ready(session)
    item = _items(_listed(client, package), "cabinet_run")[0]

    response = _use(client, package, item)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["rechecking"] is True
    assert "2 fillers and 3 cabinets" in body["recorded"]
    session.expire_all()
    operands = operands_for(session, revision)
    assert _inches(operands["CAB-FILLER-001"]["shop_fillers"]) == [Fraction(2), Fraction(3, 2)]
    assert _inches(operands["CAB-FILLER-001"]["shop_cabinets"]) == [21, 30, 24]
    assert _inches(operands["CAB-ARCH-VS-SHOP-001"]["shop_cabinets"]) == [21, 30, 24]
    assert _inches(operands["CAB-FILLER-001"]["field_width"]) == [80]
    marks = confirmed_classifications(session, revision)["CAB-FILLER-001"]["cabinet_type"]
    assert marks == ("single_door", "sink_cabinet", "drawer")
    # Each width names the sealed reading it came from; never a typed number.
    pieces = item["cabinet_proposal"]["runs"][0]["pieces"]
    references = set(session.scalars(select(ParameterValue.source_reference)))
    assert {f"confirmed AI reading {piece['candidate_id']}" for piece in pieces} <= references
    # The list now carries the run on Measurements: the marks card, already marked.
    listed = body["review_items"]
    assert _items(listed, "cabinet_run") == []
    card = _items(listed, "cabinet_kinds")
    assert [row["marked"] for row in card[0]["cabinets"]] == [
        "single_door",
        "sink_cabinet",
        "drawer",
    ]


@pytest.mark.parametrize(
    ("change", "status", "words"),
    [
        ({"pieces": ["filler", "single_door", "filler", "drawer", "filler"]}, 422, "one filler"),
        (
            {"pieces": ["single_door", "single_door", "sink_cabinet", "drawer", "filler"]},
            422,
            "one filler",
        ),
        ({"pieces": ["filler", "fridge?", "sink_cabinet", "drawer", "filler"]}, 422, "not one of"),
        ({"pieces": KINDS[:4]}, 409, "Reload"),
        ({"row_id": "00000000-0000-4000-8000-000000000000"}, 409, "no longer proposed"),
        ({"site_width": None}, 422, "site wall-to-wall"),
        ({"site_width": "2032 mm"}, 422, "in inches"),
        ({"site_width": "0"}, 422, "above zero"),
        ({"site_width": "about eighty"}, 422, "not an inch width"),
    ],
)
def test_a_refused_answer_stores_nothing(
    session: Session, change: dict[str, Any], status: int, words: str
) -> None:
    client, package, revision = _ready(session)
    item = _items(_listed(client, package), "cabinet_run")[0]

    response = _use(client, package, item, **change)

    assert response.status_code == status, response.text
    assert words in response.text
    session.expire_all()
    assert "CAB-FILLER-001" not in operands_for(session, revision)


def test_nothing_read_says_so_and_offers_no_run(session: Session) -> None:
    client, package, _revision = _ready(session, read=False)

    item = _items(_listed(client, package), "cabinet_run")[0]

    assert item["cabinet_proposal"]["runs"] == []
    assert item["cabinet_proposal"]["note"] == NOTHING_READ
