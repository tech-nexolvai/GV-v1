"""The Measure page's links: which confirmed reading is each confirmed part's width (#913).

Verification for `app/api/reading_parts.py` and `app/evidence/reading_parts.py`. The sheet is
`tests/workflow/test_part_proposals_route.py`'s, with the vendor's widths printed over its three
dimensions: `18"` and `22"` over the two cabinets drawn end to end, and `40"` over the overall
above them. The whole pipeline reads it: the page stage suggests the parts and attaches each width
to its dimension line. A person confirms the drawing as the vendor's, its three parts as suggested,
and each width as what it is, before each test asks which reading is which part's width. **Every
value and code here is invented.**

**The outcome that matters most is a count of zero**: listing the suggested links, however often,
writes no link. A link is written only by a person's decision (`workflow/reading_parts.py`).
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.api import reading_parts as reading_parts_api
from app.api.dependencies import get_artifact_store, get_session
from app.audit.events import AuditEvent
from app.config import Settings
from app.db.session import session_factory
from app.evidence.confirm import ConfirmationRefused, confirm_candidate_type
from app.main import create_app
from app.models import (
    CanonicalObservation,
    DrawingView,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    ReadingPart,
    ViewRole,
)
from storage.local import LocalStore
from tests.api.test_v1_loop import _settings
from tests.workflow.test_part_proposals_route import DRAWING, _extract, _sheet, _upgrade
from tests.workflow.test_reading_parts import _replace_in_review
from workflow import reading_parts as workflow_links
from workflow.part_pictures import PartPictureSettings
from workflow.reading_parts import REPLACED, WITHDRAWN, live_reading_parts
from workflow.stages import DatabaseStages
from workflow.view_roles import confirm_view_role, revision_views

pytest_plugins = ("tests.app.postgres_fixture",)

#: The deployment's stated tolerance for these tests: the reader's witness tolerance, which the
#: demo states for `GV_RUN_EDGE_TOLERANCE` (`tests/api/test_countertop_runs.py`).
TOLERANCE = Decimal("0.004")

#: The vendor's widths, printed just above the middle of each dimension, in the appearance's own
#: space: `18"` over the first cabinet (`x = 150..250`, `y = 550`), `22"` over the second
#: (`250..350`), and `40"` over the overall (`150..350`, `y = 640`).
WIDTHS = (
    b'BT /F1 12 Tf 185 553 Td (18") Tj ET\n'
    b'BT /F1 12 Tf 285 553 Td (22") Tj ET\n'
    b'BT /F1 12 Tf 235 643 Td (40") Tj ET\n'
)

SHEET_WITH_WIDTHS = _sheet(DRAWING + WIDTHS)

ACTOR = "reviewer@example.com"


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


def _client(
    session: Session,
    store: LocalStore,
    project_id: UUID,
    *roles: str,
    tolerance: Decimal | None = TOLERANCE,
) -> Any:
    from fastapi.testclient import TestClient

    from app.auth import Principal, Role, authenticate

    principal = Principal(
        id=ACTOR,
        roles=frozenset(Role(role) for role in (roles or ("reviewer",))),
        projects=frozenset({project_id}),
    )
    settings = Settings(
        **{**_settings().model_dump(), "run_edge_tolerance": tolerance}  # type: ignore[arg-type]
    )
    app = create_app(settings)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_artifact_store] = lambda: store
    app.dependency_overrides[authenticate] = lambda: principal
    return TestClient(app, raise_server_exceptions=False)


class Sheet:
    """The extracted sheet: its drawing confirmed as the vendor's, its three parts confirmed as
    suggested, and its three widths confirmed as what they are."""

    def __init__(self, session: Session, store: LocalStore) -> None:
        revision, _ = _extract(session, store, data=SHEET_WITH_WIDTHS)
        package = session.get_one(Package, revision.package_id)
        self.session, self.store = session, store
        self.revision: PackageRevision = revision
        self.project_id, self.package_id = package.project_id, package.id
        (entry,) = revision_views(session, revision.id)
        self.view: DrawingView = entry.view
        confirm_view_role(session, view=self.view, role=ViewRole.SHOP, actor=ACTOR)
        session.commit()
        client = self.client()
        listed = client.get(f"{self.base}/parts").json()["drawings"][0]["parts"]
        self.proposals: dict[str, str] = {}
        for name, part in zip(("left", "top", "right"), listed, strict=True):
            response = client.post(
                f"{self.base}/parts/{part['proposal_id']}/confirm",
                json={"kind": part["suggested_kind"], "code": part["suggested_code"]},
            )
            assert response.status_code == 201, response.text
            self.proposals[name] = part["proposal_id"]
        self.readings = {
            '18"': self._label('18"', "cabinet_width"),
            '22"': self._label('22"', "cabinet_width"),
            '40"': self._label('40"', "countertop_overall_width"),
        }
        session.commit()
        drawing = self.drawing(client)
        self.left, self.top, self.right = (part["item_id"] for part in drawing["parts"])

    def _label(self, text: str, semantic_type: str) -> str:
        """A person says what the reading `text` is: the only one with that text on the sheet."""
        (candidate,) = self.session.scalars(
            select(ObservationCandidate)
            .join(
                PackageRevisionDocument,
                PackageRevisionDocument.document_version_id
                == ObservationCandidate.document_version_id,
            )
            .where(
                PackageRevisionDocument.package_revision_id == self.revision.id,
                ObservationCandidate.raw_text == text,
            )
        )
        confirmed = confirm_candidate_type(
            self.session, candidate_id=candidate.id, semantic_type=semantic_type, confirmed_by=ACTOR
        )
        assert not isinstance(confirmed, ConfirmationRefused), confirmed
        return str(confirmed.id)

    @property
    def base(self) -> str:
        return f"/api/v1/projects/{self.project_id}/packages/{self.package_id}"

    def client(self, *roles: str, tolerance: Decimal | None = TOLERANCE) -> Any:
        return _client(self.session, self.store, self.project_id, *roles, tolerance=tolerance)

    def links(self, client: Any | None = None) -> dict[str, Any]:
        response = (client or self.client()).get(f"{self.base}/reading-parts")
        assert response.status_code == 200, response.text
        return cast(dict[str, Any], response.json())

    def drawing(self, client: Any | None = None) -> dict[str, Any]:
        drawings = self.links(client)["drawings"]
        assert len(drawings) == 1
        return cast(dict[str, Any], drawings[0])

    def part(self, item_id: str, client: Any | None = None) -> dict[str, Any]:
        (found,) = (part for part in self.drawing(client)["parts"] if part["item_id"] == item_id)
        return cast(dict[str, Any], found)

    def confirm(self, item_id: str, reading_id: str, client: Any | None = None) -> Any:
        return (client or self.client()).post(
            f"{self.base}/reading-parts/{item_id}/confirm", json={"reading_id": reading_id}
        )

    def withdraw(self, item_id: str, client: Any | None = None) -> Any:
        return (client or self.client()).post(f"{self.base}/reading-parts/{item_id}/withdraw")

    def read(self) -> list[ReadingPart]:
        return list(self.session.scalars(live_reading_parts().order_by(ReadingPart.created_at)))


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


@pytest.fixture
def sheet(session: Session, store: LocalStore) -> Sheet:
    return Sheet(session, store)


# -- the suggestion --------------------------------------------------------------------------------


def test_each_part_is_suggested_its_own_width_and_listing_writes_nothing(sheet: Sheet) -> None:
    """**Done when, 1.** Each confirmed part is suggested the reading on its own dimension line,
    with why; listing it three times writes no link."""
    for _ in range(3):
        drawing = sheet.drawing()

    suggested = {part["item_id"]: part["suggestion"] for part in drawing["parts"]}
    assert suggested[sheet.left]["reading_id"] == sheet.readings['18"']
    assert suggested[sheet.right]["reading_id"] == sheet.readings['22"']
    assert suggested[sheet.top]["reading_id"] == sheet.readings['40"']
    for suggestion in suggested.values():
        assert suggestion["said"].startswith("Its dimension line runs across the page")
        assert suggestion["spanning"] == [suggestion["reading_id"]]
        assert suggestion["edge_tolerance"] == "0.004"
    assert [part["number"] for part in drawing["parts"]] == [1, 2, 3]
    assert all(part["links"] == [] for part in drawing["parts"])
    assert _count(sheet.session, ReadingPart) == 0


def test_each_confirmed_part_shows_the_picture_of_the_suggestion_it_was_confirmed_from(
    sheet: Sheet,
) -> None:
    """**#897.** A confirmed part's picture is its suggestion's: each part names that suggestion and
    says whether its picture is stored. Cutting the pictures changes nothing else in the list."""
    before = sheet.drawing()["parts"]
    DatabaseStages(
        store=sheet.store,
        dpi=150,
        part_pictures=PartPictureSettings(margin_pt=Decimal(36), dpi=150),
    ).cut_part_pictures(sheet.session, sheet.revision.id)
    sheet.session.commit()
    after = sheet.drawing()["parts"]
    picture = sheet.client().get(f"{sheet.base}/parts/{after[0]['proposal_id']}/picture")

    assert [part["proposal_id"] for part in before] == [
        sheet.proposals[name] for name in ("left", "top", "right")
    ]
    assert [part["has_picture"] for part in before] == [False, False, False]
    assert [part["has_picture"] for part in after] == [True, True, True]
    unchanged = ("item_id", "proposal_id", "number", "suggestion", "links")
    assert [{key: part[key] for key in unchanged} for part in after] == [
        {key: part[key] for key in unchanged} for part in before
    ]
    assert picture.status_code == 200 and picture.headers["content-type"] == "image/png"
    assert _count(sheet.session, ReadingPart) == 0


def test_each_reading_is_listed_with_its_value_and_what_places_it(sheet: Sheet) -> None:
    """The person picking a reading sees its exact value, what they confirmed it is, and that its
    line places it: the value is the reading's own, never a length measured off the drawing."""
    readings = {reading["reading_id"]: reading for reading in sheet.drawing()["readings"]}

    assert set(readings) == set(sheet.readings.values())
    eighteen = readings[sheet.readings['18"']]
    assert (eighteen["value"], eighteen["semantic_type"]) == ("18 in", "cabinet_width")
    assert {reading["placed_by"] for reading in readings.values()} == {"line"}
    assert all(reading["linked_to"] is None for reading in readings.values())


def test_without_a_stated_tolerance_nothing_is_suggested_but_a_person_may_pick(
    sheet: Sheet,
) -> None:
    """No default: the page says why nothing is suggested, and a person's pick is still recorded,
    saying nothing was suggested."""
    client = sheet.client(tolerance=None)

    links = sheet.links(client)
    response = sheet.confirm(sheet.left, sheet.readings['18"'], client)

    assert links["can_suggest"] is False and "GV_RUN_EDGE_TOLERANCE" in links["why_not"]
    assert all(part["suggestion"] is None for part in links["drawings"][0]["parts"])
    assert response.status_code == 201, response.text
    (link,) = response.json()["links"]
    assert "Nothing was suggested" in link["signal"]
    assert [str(row.canonical_observation_id) for row in sheet.read()] == [sheet.readings['18"']]


# -- deciding --------------------------------------------------------------------------------------


def test_only_a_confirmation_writes_a_link(sheet: Sheet) -> None:
    """**Done when, 1.** One row, naming the person and the suggestion's sentence; audited; read."""
    response = sheet.confirm(sheet.left, sheet.readings['18"'])

    assert response.status_code == 201, response.text
    (link,) = response.json()["links"]
    assert link["reading_id"] == sheet.readings['18"']
    assert link["decided_by"] == ACTOR
    assert link["read"] is True and link["why_not_read"] is None
    assert link["signal"].startswith("Its dimension line runs across the page")
    (row,) = sheet.read()
    assert str(row.drawing_item_id) == sheet.left
    audited = sheet.session.scalars(select(AuditEvent).where(AuditEvent.target_id == row.id)).one()
    assert (audited.actor, audited.target_type) == (ACTOR, "reading_part")
    linked = {r["reading_id"]: r for r in sheet.drawing()["readings"]}[sheet.readings['18"']]
    assert (linked["linked_to"], linked["linked_to_number"]) == (sheet.left, 1)


def test_a_withdrawn_link_is_not_read(sheet: Sheet) -> None:
    """**Done when, 2.** Its row stays; nothing reads it; the suggestion is still beside the part."""
    assert sheet.confirm(sheet.left, sheet.readings['18"']).status_code == 201

    response = sheet.withdraw(sheet.left)

    assert response.status_code == 201, response.text
    assert response.json()["links"] == []
    assert response.json()["suggestion"]["reading_id"] == sheet.readings['18"']
    assert sheet.read() == []
    rows = list(sheet.session.scalars(select(ReadingPart).order_by(ReadingPart.created_at)))
    assert [row.drawing_item_id for row in rows] == [UUID(sheet.left), None]
    assert rows[1].signal == WITHDRAWN


def test_a_corrected_link_replaces_the_one_before(sheet: Sheet) -> None:
    """**Done when, 2.** The cabinet was linked to the wrong reading; the person picks the right
    one. Only the right one is read, the wrong one's link is taken back in the same decision, and
    the picked reading's sentence says a person picked it."""
    wrong = sheet.confirm(sheet.left, sheet.readings['22"'])
    assert wrong.status_code == 201, wrong.text
    assert wrong.json()["links"][0]["signal"].startswith("Picked by a person.")

    response = sheet.confirm(sheet.left, sheet.readings['18"'])

    assert response.status_code == 201, response.text
    assert [link["reading_id"] for link in response.json()["links"]] == [sheet.readings['18"']]
    assert [str(row.canonical_observation_id) for row in sheet.read()] == [sheet.readings['18"']]
    taken_back = workflow_links.current_link(sheet.session, UUID(sheet.readings['22"']))
    assert taken_back is not None and taken_back.drawing_item_id is None
    assert taken_back.signal == REPLACED


@pytest.mark.parametrize("taken_back", ["withdraw", "confirm"])
def test_a_link_whose_part_is_taken_back_is_not_read(sheet: Sheet, taken_back: str) -> None:
    """**Done when, 2.** The cabinet is said not to be a part, or corrected to a filler, after its
    width was linked: the link is not read, and the corrected part starts with no link."""
    assert sheet.confirm(sheet.left, sheet.readings['18"']).status_code == 201
    client = sheet.client()

    response = client.post(
        f"{sheet.base}/parts/{sheet.proposals['left']}/{taken_back}",
        **({"json": {"kind": "filler", "code": None}} if taken_back == "confirm" else {}),
    )

    assert response.status_code == 201, response.text
    assert sheet.read() == []
    parts = sheet.drawing(client)["parts"]
    assert sheet.left not in {part["item_id"] for part in parts}
    assert all(part["links"] == [] for part in parts)


def test_a_link_whose_reading_was_replaced_in_review_is_not_read_and_says_so(sheet: Sheet) -> None:
    """**Done when, 2.** A reviewer confirmed the linked reading again in review, which makes a new
    reading in its place: the old link is not read, the page says why, and the old reading is no
    longer offered."""
    assert sheet.confirm(sheet.left, sheet.readings['18"']).status_code == 201

    _replace_in_review(sheet.session, UUID(sheet.readings['18"']))
    sheet.session.commit()

    part = sheet.part(sheet.left)
    assert sheet.read() == []
    (link,) = part["links"]
    assert link["read"] is False and "in review" in link["why_not_read"]
    offered = {reading["reading_id"] for reading in sheet.drawing()["readings"]}
    assert sheet.readings['18"'] not in offered
    assert sheet.confirm(sheet.left, sheet.readings['18"']).status_code == 422


def test_two_decisions_at_the_same_moment_are_not_both_recorded(
    sheet: Sheet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second of two people deciding on one reading at once is told to reload. Simulated by
    hiding the first decision from the second, as a concurrent request would not see it."""
    assert sheet.confirm(sheet.left, sheet.readings['18"']).status_code == 201
    monkeypatch.setattr(workflow_links, "current_link", lambda *_: None)

    response = sheet.confirm(sheet.right, sheet.readings['18"'])

    assert response.status_code == 409, response.text
    assert "Reload the page" in response.json()["message"]
    assert _count(sheet.session, ReadingPart) == 1


# -- what is refused -------------------------------------------------------------------------------


def _other_drawings_reading(sheet: Sheet, session: Session, store: LocalStore) -> str:
    """A width confirmed on another package's sheet."""
    other = Sheet(session, store)
    return other.readings['18"']


@pytest.mark.parametrize(
    ("case", "status_code", "said"),
    [
        ("not-a-reading", 422, "Reload the page"),
        ("another-drawings-reading", 422, "own drawing"),
        ("an-unknown-part", 404, "Not found"),
        ("a-part-taken-back", 404, "Not found"),
    ],
)
def test_a_link_a_person_could_not_have_meant_is_refused(
    sheet: Sheet, session: Session, store: LocalStore, case: str, status_code: int, said: str
) -> None:
    item, reading = sheet.left, sheet.readings['18"']
    if case == "not-a-reading":
        reading = str(uuid4())
    elif case == "another-drawings-reading":
        reading = _other_drawings_reading(sheet, session, store)
    elif case == "an-unknown-part":
        item = str(uuid4())
    else:
        sheet.client().post(f"{sheet.base}/parts/{sheet.proposals['left']}/withdraw")

    response = sheet.confirm(item, reading)

    assert response.status_code == status_code, response.text
    assert said in response.json()["message"]
    assert _count(sheet.session, ReadingPart) == 0


def test_taking_back_a_link_that_does_not_exist_is_refused(sheet: Sheet) -> None:
    response = sheet.withdraw(sheet.left)

    assert response.status_code == 422, response.text
    assert "nothing to take back" in response.json()["message"]
    assert _count(sheet.session, ReadingPart) == 0


def test_on_a_drawing_no_longer_the_vendors_a_link_is_refused_but_can_be_taken_back(
    sheet: Sheet,
) -> None:
    """A person since said this is the architect's drawing. Its parts still stand, so it is listed,
    with why nothing can be linked; taking a link back still works, because it links nothing."""
    assert sheet.confirm(sheet.left, sheet.readings['18"']).status_code == 201
    confirm_view_role(sheet.session, view=sheet.view, role=ViewRole.ARCH, actor=ACTOR)
    sheet.session.commit()

    drawing = sheet.drawing()
    confirmed = sheet.confirm(sheet.right, sheet.readings['22"'])
    withdrawn = sheet.withdraw(sheet.left)

    assert drawing["can_confirm"] is False
    assert "no longer confirmed as the vendor's" in drawing["why_not"]
    assert confirmed.status_code == 409
    assert withdrawn.status_code == 201, withdrawn.text
    assert sheet.read() == []


def test_someone_who_cannot_confirm_evidence_cannot_decide_a_link(sheet: Sheet) -> None:
    """A rule administrator may read the links but not decide one; the refusal looks like an
    absence."""
    client = sheet.client("rule_admin")

    assert client.get(f"{sheet.base}/reading-parts").status_code == 200
    assert sheet.confirm(sheet.left, sheet.readings['18"'], client).status_code == 404
    assert sheet.withdraw(sheet.left, client).status_code == 404
    assert _count(sheet.session, ReadingPart) == 0


def test_another_projects_part_is_not_found(
    sheet: Sheet, session: Session, store: LocalStore
) -> None:
    """Project A's part, asked for through project B's own package, is absent."""
    other = Sheet(session, store)
    client = other.client()

    responses = [
        client.post(
            f"{other.base}/reading-parts/{sheet.left}/confirm",
            json={"reading_id": sheet.readings['18"']},
        ),
        client.post(f"{other.base}/reading-parts/{sheet.left}/withdraw"),
        client.get(f"{sheet.base}/reading-parts"),
    ]

    assert [response.status_code for response in responses] == [404, 404, 404]
    assert _count(session, ReadingPart) == 0


def test_there_is_no_confirm_all() -> None:
    """Every endpoint that decides a link decides one part's, named in its path, and a
    confirmation names one reading. Read off the router itself, as #893's test does."""
    deciding = [
        route
        for route in reading_parts_api.router.routes
        if isinstance(route, APIRoute) and "POST" in route.methods
    ]

    assert len(deciding) == 2
    assert all("{item_id}" in route.path for route in deciding)
    assert set(reading_parts_api.ConfirmLinkIn.model_fields) == {"reading_id"}


def test_a_link_changes_no_reading(sheet: Sheet) -> None:
    """**Widths always come from readings.** Confirming and taking back links writes no reading and
    changes none: the canonical readings are exactly what they were."""
    before = {
        row.id: (row.value_numerator, row.value_denominator, row.unit, row.semantic_type)
        for row in sheet.session.scalars(select(CanonicalObservation))
    }

    assert sheet.confirm(sheet.left, sheet.readings['18"']).status_code == 201
    assert sheet.confirm(sheet.left, sheet.readings['22"']).status_code == 201
    assert sheet.withdraw(sheet.left).status_code == 201

    after = {
        row.id: (row.value_numerator, row.value_denominator, row.unit, row.semantic_type)
        for row in sheet.session.scalars(select(CanonicalObservation))
    }
    assert after == before
