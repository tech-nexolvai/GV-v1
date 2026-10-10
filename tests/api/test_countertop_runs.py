"""The Measure page's runs: the parts beneath each countertop, suggested, then decided by a person.

Verification for `app/api/countertop_runs.py` and `app/evidence/countertop_runs.py` (#893). The
sheet is `tests/workflow/test_part_proposals_route.py`'s, as in `tests/api/test_drawing_parts.py`:
one vendor drawing on which the page stage suggests two cabinets drawn end to end and a countertop
dimensioned above them. A person confirms all three as suggested before each test asks for a run.
**Every code here is invented.**

**The outcome that matters most is a count of zero**: listing the suggested runs, however often,
writes no run. A run is written only by a person's confirmation (`workflow/countertop_runs.py`).
"""

from __future__ import annotations

import re
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

from app.api import countertop_runs as countertop_runs_api
from app.api.dependencies import get_artifact_store, get_session
from app.audit.events import AuditEvent
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models import (
    CountertopRun,
    CountertopRunDecision,
    DrawingView,
    Package,
    PackageRevision,
    PartProposal,
    ViewRole,
)
from storage.local import LocalStore
from tests.api.test_v1_loop import _settings
from tests.workflow.test_part_proposals_route import _extract, _upgrade
from tests.workflow.test_stages import _publish_rulebook
from workflow import countertop_runs as workflow_runs
from workflow.countertop_runs import RUN_PROPOSAL_SOURCE, live_run_rows
from workflow.view_roles import confirm_view_role, revision_views

pytest_plugins = ("tests.app.postgres_fixture",)

#: The deployment's stated tolerance for these tests: the reader's witness tolerance the suggester
#: used on this sheet (`tests/workflow/test_association.py`'s `SETTINGS`).
TOLERANCE = Decimal("0.004")


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        _publish_rulebook(opened)
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
        id="reviewer@example.com",
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
    """The extracted sheet, its drawing confirmed as the vendor's and its three parts confirmed."""

    def __init__(self, session: Session, store: LocalStore) -> None:
        revision, _ = _extract(session, store)
        package = session.get_one(Package, revision.package_id)
        self.session, self.store = session, store
        self.revision: PackageRevision = revision
        self.project_id, self.package_id = package.project_id, package.id
        (entry,) = revision_views(session, revision.id)
        self.view: DrawingView = entry.view
        confirm_view_role(session, view=self.view, role=ViewRole.SHOP, actor="reviewer@example.com")
        session.commit()
        client = self.client()
        listed = client.get(f"{self.base}/parts").json()["drawings"][0]["parts"]
        self.parts: dict[str, dict[str, Any]] = {}
        for name, part in zip(("left", "top", "right"), listed, strict=True):
            response = client.post(
                f"{self.base}/parts/{part['proposal_id']}/confirm",
                json={"kind": part["suggested_kind"], "code": part["suggested_code"]},
            )
            assert response.status_code == 201, response.text
            self.parts[name] = response.json()
        drawing = self.runs(client)["drawings"][0]
        self.top = drawing["countertops"][0]["countertop_item_id"]
        self.left, self.right = (part["item_id"] for part in drawing["parts"])

    @property
    def base(self) -> str:
        return f"/api/v1/projects/{self.project_id}/packages/{self.package_id}"

    def client(self, *roles: str, tolerance: Decimal | None = TOLERANCE) -> Any:
        return _client(self.session, self.store, self.project_id, *roles, tolerance=tolerance)

    def runs(self, client: Any | None = None) -> dict[str, Any]:
        response = (client or self.client()).get(f"{self.base}/countertop-runs")
        assert response.status_code == 200, response.text
        return cast(dict[str, Any], response.json())

    def countertop(self, client: Any | None = None) -> dict[str, Any]:
        drawings = self.runs(client)["drawings"]
        assert len(drawings) == 1 and len(drawings[0]["countertops"]) == 1
        return cast(dict[str, Any], drawings[0]["countertops"][0])

    def confirm(self, part_ids: list[str], client: Any | None = None) -> Any:
        return (client or self.client()).post(
            f"{self.base}/countertop-runs/{self.top}/confirm",
            json={"part_ids": part_ids, "wall_config": "back_left_right"},
        )

    def withdraw(self, client: Any | None = None) -> Any:
        return (client or self.client()).post(f"{self.base}/countertop-runs/{self.top}/withdraw")

    def read(self) -> list[CountertopRun]:
        return list(self.session.scalars(live_run_rows().order_by(CountertopRun.position)))


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _nothing_written(session: Session) -> None:
    assert _count(session, CountertopRun) == 0
    assert _count(session, CountertopRunDecision) == 0


@pytest.fixture
def sheet(session: Session, store: LocalStore) -> Sheet:
    return Sheet(session, store)


# -- the suggestion --------------------------------------------------------------------------------


def test_a_suggested_run_writes_nothing_however_often_it_is_listed(sheet: Sheet) -> None:
    """**Done when, 1.** The run is suggested, both cabinets in it left to right, each with why;
    listing it three times writes no run and no decision."""
    for _ in range(3):
        countertop = sheet.countertop()

    suggestion = countertop["suggestion"]
    assert [member["item_id"] for member in suggestion["members"]] == [sheet.left, sheet.right]
    assert [member["position"] for member in suggestion["members"]] == [1, 2]
    assert [member["number"] for member in suggestion["members"]] == [1, 3]
    assert all("not drawn above the top" in member["signal"] for member in suggestion["members"])
    assert suggestion["left_out"] == [] and suggestion["warnings"] == []
    assert suggestion["edge_tolerance"] == "0.004"
    assert countertop["decision"] is None and countertop["number"] == 2
    _nothing_written(sheet.session)


def _wall_cabinet(sheet: Sheet) -> str:
    """A person adds a cabinet drawn above the countertop, between the countertop's top and the top
    of the drawing, over the first cabinet. Confirmed as a cabinet: its kind does not say wall."""
    region = cast(list[list[str]], sheet.view.region["points"])
    drawing_top = min(Decimal(y) for _, y in region)
    countertop = sheet.session.get_one(PartProposal, UUID(sheet.parts["top"]["proposal_id"]))
    countertop_top = min(Decimal(y) for _, y in cast(list[list[str]], countertop.extent["points"]))
    assert drawing_top < countertop_top
    height = str((drawing_top + countertop_top) / 2)
    left, right = sheet.parts["left"]["left_end"], sheet.parts["left"]["right_end"]
    response = sheet.client().post(
        f"{sheet.base}/views/{sheet.view.id}/parts",
        json={
            "kind": "cabinet",
            "ends": [{"x": left["x"], "y": height}, {"x": right["x"], "y": height}],
        },
    )
    assert response.status_code == 201, response.text
    drawing = sheet.runs()["drawings"][0]
    (wall,) = [
        part["item_id"]
        for part in drawing["parts"]
        if part["item_id"] not in (sheet.left, sheet.right)
    ]
    return cast(str, wall)


def test_a_wall_cabinet_above_the_top_is_never_suggested(sheet: Sheet) -> None:
    """**Done when, 2.** A confirmed cabinet along the countertop but above its top is listed as
    left out, with why, and is not in the suggested run."""
    wall = _wall_cabinet(sheet)

    suggestion = sheet.countertop()["suggestion"]

    assert [member["item_id"] for member in suggestion["members"]] == [sheet.left, sheet.right]
    assert [entry["item_id"] for entry in suggestion["left_out"]] == [wall]
    assert "higher up the page" in suggestion["left_out"][0]["reason"]


def test_without_a_stated_tolerance_nothing_is_suggested_or_confirmed(sheet: Sheet) -> None:
    """No default: the page says why, and a confirmation is refused rather than recorded with a
    tolerance nobody stated."""
    client = sheet.client(tolerance=None)

    runs = sheet.runs(client)
    confirmed = sheet.confirm([sheet.left, sheet.right], client)

    assert runs["can_suggest"] is False
    assert "GV_RUN_EDGE_TOLERANCE" in runs["why_not"]
    assert runs["drawings"][0]["countertops"][0]["suggestion"] is None
    assert confirmed.status_code == 409
    assert "GV_RUN_EDGE_TOLERANCE" in confirmed.json()["message"]
    _nothing_written(sheet.session)


# -- deciding --------------------------------------------------------------------------------------


def test_a_new_run_requires_a_chosen_wall_layout(sheet: Sheet) -> None:
    from app.api.countertop_runs import ConfirmRunIn

    assert "wall_config" in ConfirmRunIn.model_json_schema()["required"]
    response = sheet.client().post(
        f"{sheet.base}/countertop-runs/{sheet.top}/confirm",
        json={"part_ids": [sheet.left, sheet.right]},
    )

    assert response.status_code == 422
    assert "choose this countertop's wall layout" in response.json()["message"].lower()
    _nothing_written(sheet.session)


def test_a_new_run_refuses_an_unpublished_wall_layout(sheet: Sheet) -> None:
    response = sheet.client().post(
        f"{sheet.base}/countertop-runs/{sheet.top}/confirm",
        json={"part_ids": [sheet.left, sheet.right], "wall_config": "made_up"},
    )

    assert response.status_code == 422
    _nothing_written(sheet.session)


def test_a_run_with_a_wall_at_one_end_is_offered_and_stored(sheet: Sheet) -> None:
    """#1138: "back wall and left/right end" are published choices and the database keeps one."""
    assert {"back_and_left", "back_and_right"} <= set(sheet.runs()["wall_layout_choices"])
    response = sheet.client().post(
        f"{sheet.base}/countertop-runs/{sheet.top}/confirm",
        json={"part_ids": [sheet.left, sheet.right], "wall_config": "back_and_left"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["decision"]["wall_config"] == "back_and_left"
    sheet.session.expire_all()
    stored = sheet.session.scalars(select(CountertopRunDecision)).one()
    assert stored.wall_config == "back_and_left"


def test_only_a_confirmation_writes_a_run_in_the_drawings_order(sheet: Sheet) -> None:
    """**Done when, 1.** Picked right first, written left to right; one decision, audited, naming
    the person; every row the stated tolerance and the suggester."""
    response = sheet.confirm([sheet.right, sheet.left])

    assert response.status_code == 201, response.text
    decision = response.json()["decision"]
    assert decision["decision"] == "confirmed"
    assert decision["decided_by"] == "reviewer@example.com"
    assert decision["read"] is True and decision["why_not_read"] is None
    assert decision["edge_tolerance"] == "0.004"
    assert decision["wall_config"] == "back_left_right"
    assert "back_only" in sheet.runs()["wall_layout_choices"]
    assert [member["item_id"] for member in decision["members"]] == [sheet.left, sheet.right]
    assert [member["position"] for member in decision["members"]] == [1, 2]
    rows = sheet.read()
    assert [str(row.member_item_id) for row in rows] == [sheet.left, sheet.right]
    assert {row.proposal_source for row in rows} == {RUN_PROPOSAL_SOURCE}
    assert {row.edge_tolerance for row in rows} == {TOLERANCE}
    stored = sheet.session.scalars(select(CountertopRunDecision)).one()
    audited = sheet.session.scalars(
        select(AuditEvent).where(AuditEvent.target_id == stored.id)
    ).one()
    assert (audited.actor, audited.target_type) == (
        "reviewer@example.com",
        "countertop_run_decision",
    )


def test_a_withdrawn_run_is_not_read(sheet: Sheet) -> None:
    """**Done when, 3.** Withdrawn after it was confirmed: its rows stay, nothing reads them, and
    the page shows the withdrawal with the suggestion still beside it."""
    assert sheet.confirm([sheet.left, sheet.right]).status_code == 201

    response = sheet.withdraw()

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["decision"]["decision"] == "withdrawn"
    assert body["decision"]["wall_config"] is None
    assert body["decision"]["members"] == [] and body["decision"]["read"] is False
    assert len(body["suggestion"]["members"]) == 2
    assert sheet.read() == []
    assert _count(sheet.session, CountertopRun) == 2


def test_a_correction_requires_its_own_new_layout_and_never_inherits_the_old_one(
    sheet: Sheet,
) -> None:
    assert sheet.confirm([sheet.left, sheet.right]).status_code == 201
    first = sheet.session.scalars(select(CountertopRunDecision)).one()

    missing = sheet.client().post(
        f"{sheet.base}/countertop-runs/{sheet.top}/confirm",
        json={"part_ids": [sheet.left]},
    )
    assert missing.status_code == 422
    assert sheet.countertop()["decision"]["wall_config"] == "back_left_right"
    assert _count(sheet.session, CountertopRunDecision) == 1

    corrected = sheet.client().post(
        f"{sheet.base}/countertop-runs/{sheet.top}/confirm",
        json={"part_ids": [sheet.left], "wall_config": "back_only"},
    )
    assert corrected.status_code == 201, corrected.text
    assert corrected.json()["decision"]["wall_config"] == "back_only"
    current = sheet.session.scalars(
        select(CountertopRunDecision).where(CountertopRunDecision.supersedes_id == first.id)
    ).one()
    assert current.wall_config == "back_only"
    assert first.wall_config == "back_left_right"


def test_a_suggested_run_can_be_said_to_be_wrong_before_anything_is_confirmed(
    sheet: Sheet,
) -> None:
    """Rejecting the suggestion writes a decision and no run."""
    response = sheet.withdraw()

    assert response.status_code == 201, response.text
    assert response.json()["decision"]["decision"] == "withdrawn"
    assert _count(sheet.session, CountertopRun) == 0
    assert sheet.read() == []


def test_a_corrected_run_replaces_the_one_before(sheet: Sheet) -> None:
    """**Done when, 3.** The person corrects the run to hold the first cabinet only: only that run
    is read, and the earlier one's rows stay."""
    assert sheet.confirm([sheet.left, sheet.right]).status_code == 201

    response = sheet.confirm([sheet.left])

    assert response.status_code == 201, response.text
    assert [str(row.member_item_id) for row in sheet.read()] == [sheet.left]
    assert _count(sheet.session, CountertopRun) == 3
    decisions = list(sheet.session.scalars(select(CountertopRunDecision)))
    assert len(decisions) == 2
    assert {decision.supersedes_id for decision in decisions} == {
        None,
        next(d.id for d in decisions if d.supersedes_id is None),
    }


def test_a_run_whose_part_is_taken_back_is_not_read_and_says_so(sheet: Sheet) -> None:
    """**Done when, 3.** The second cabinet is said not to be a part after the run was confirmed:
    the run is not read at all, and the page says why and which part no longer stands."""
    assert sheet.confirm([sheet.left, sheet.right]).status_code == 201
    client = sheet.client()

    taken_back = client.post(f"{sheet.base}/parts/{sheet.parts['right']['proposal_id']}/withdraw")
    decision = sheet.countertop(client)["decision"]

    assert taken_back.status_code == 201
    assert sheet.read() == []
    assert decision["read"] is False
    assert "taken back or corrected" in decision["why_not_read"]
    assert [member["stands"] for member in decision["members"]] == [True, False]


def test_a_person_may_add_a_part_the_suggestion_left_out(sheet: Sheet) -> None:
    """The person's call stands, and the row says it was the person's: the filter decides only
    what is suggested."""
    wall = _wall_cabinet(sheet)

    response = sheet.confirm([sheet.left, sheet.right, wall])

    assert response.status_code == 201, response.text
    signals = {str(row.member_item_id): row.signal for row in sheet.read()}
    assert signals[wall].startswith("Added to the run by a person.")


def test_two_decisions_at_the_same_moment_are_not_both_recorded(
    sheet: Sheet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second of two people deciding at once is told to reload. Simulated by hiding the first
    decision from the second, as a concurrent request would not see it."""
    assert sheet.confirm([sheet.left, sheet.right]).status_code == 201

    monkeypatch.setattr(workflow_runs, "current_run_decision", lambda *_: None)
    response = sheet.withdraw()

    assert response.status_code == 409
    assert "Reload the page" in response.json()["message"]
    assert _count(sheet.session, CountertopRunDecision) == 1


# -- what is refused -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "status_code", "said"),
    [
        ("no-parts", 422, "at least one part"),
        ("a-part-twice", 422, "once"),
        ("the-countertop-in-its-own-run", 422, "only cabinets and fillers"),
        ("a-part-taken-back", 422, "Reload the page"),
        ("an-unknown-part", 422, "only cabinets and fillers"),
    ],
)
def test_a_run_a_person_could_not_have_meant_is_refused(
    sheet: Sheet, case: str, status_code: int, said: str
) -> None:
    parts = {
        "no-parts": [],
        "a-part-twice": [sheet.left, sheet.left],
        "the-countertop-in-its-own-run": [sheet.top],
        "a-part-taken-back": [sheet.left],
        "an-unknown-part": [str(uuid4())],
    }[case]
    if case == "a-part-taken-back":
        client = sheet.client()
        client.post(f"{sheet.base}/parts/{sheet.parts['left']['proposal_id']}/withdraw")

    response = sheet.confirm(parts)

    assert response.status_code == status_code, response.text
    assert said in response.json()["message"]
    _nothing_written(sheet.session)


def test_a_cabinet_or_an_unknown_id_is_not_a_countertop(sheet: Sheet) -> None:
    client = sheet.client()
    responses = [
        client.post(
            f"{sheet.base}/countertop-runs/{countertop}/confirm",
            json={"part_ids": [sheet.left], "wall_config": "back_left_right"},
        )
        for countertop in (sheet.left, str(uuid4()))
    ]

    assert [response.status_code for response in responses] == [404, 404]
    _nothing_written(sheet.session)


def test_on_a_drawing_no_longer_the_vendors_a_run_is_refused_but_can_be_withdrawn(
    sheet: Sheet,
) -> None:
    """A person since said this is the architect's drawing. Its parts still stand, so it is listed,
    with why no run can be confirmed; saying a run is wrong still works, because it makes none."""
    confirm_view_role(
        sheet.session, view=sheet.view, role=ViewRole.ARCH, actor="reviewer@example.com"
    )
    sheet.session.commit()

    drawing = sheet.runs()["drawings"][0]
    confirmed = sheet.confirm([sheet.left, sheet.right])
    withdrawn = sheet.withdraw()

    assert drawing["can_confirm"] is False
    assert "no longer confirmed as the vendor's" in drawing["why_not"]
    assert confirmed.status_code == 409
    assert withdrawn.status_code == 201, withdrawn.text
    assert _count(sheet.session, CountertopRun) == 0


def test_someone_who_cannot_confirm_evidence_cannot_decide_a_run(sheet: Sheet) -> None:
    """A rule administrator may read the runs but not decide one; the refusal looks like an
    absence."""
    client = sheet.client("rule_admin")

    assert client.get(f"{sheet.base}/countertop-runs").status_code == 200
    assert sheet.confirm([sheet.left], client).status_code == 404
    assert sheet.withdraw(client).status_code == 404
    _nothing_written(sheet.session)


def test_another_projects_countertop_is_not_found(
    sheet: Sheet, session: Session, store: LocalStore
) -> None:
    """Project A's countertop, asked for through project B's own package, is absent."""
    other = Sheet(session, store)
    client = other.client()

    responses = [
        client.post(
            f"{other.base}/countertop-runs/{sheet.top}/confirm",
            json={"part_ids": [sheet.left], "wall_config": "back_left_right"},
        ),
        client.post(f"{other.base}/countertop-runs/{sheet.top}/withdraw"),
        client.get(f"{sheet.base}/countertop-runs"),
    ]

    assert [response.status_code for response in responses] == [404, 404, 404]
    _nothing_written(session)


def test_there_is_no_confirm_all() -> None:
    """Every endpoint that decides a run decides one countertop's, named in its path. Read off the
    router itself: `app.routes` holds one opaque entry per included router (see
    `tests/api/test_no_heavy_work.py`), and the tests above prove this router is wired."""
    deciding = [
        route
        for route in countertop_runs_api.router.routes
        if isinstance(route, APIRoute) and "POST" in route.methods
    ]

    assert len(deciding) == 2
    assert all("{countertop_item_id}" in route.path for route in deciding)


def test_the_demo_states_the_readers_own_tolerance_for_runs() -> None:
    """The demo's API states `GV_RUN_EDGE_TOLERANCE`, and states the number its worker gives the
    part suggester (`GV_READER_WITNESS_TOLERANCE`): one question, one number. Read from each block
    the way each process reads its environment, and parsed by `Settings` as the API will."""
    demo = (Path(__file__).resolve().parents[2] / "scripts" / "demo.sh").read_text(encoding="utf-8")
    api_end = demo.index("scripts/dev_server.py &")
    api_block = demo[:api_end]
    worker_block = demo[api_end : demo.index("scripts/drain_outbox.py --watch")]

    witness = re.findall(
        r"^GV_READER_WITNESS_TOLERANCE=(\S+) \\$", worker_block, flags=re.MULTILINE
    )
    runs = re.findall(r"^GV_RUN_EDGE_TOLERANCE=(\S+) \\$", api_block, flags=re.MULTILINE)

    assert runs == witness and len(runs) == 1
    stated = Settings(
        **{**_settings().model_dump(), "run_edge_tolerance": runs[0]}  # type: ignore[arg-type]
    )
    assert stated.run_edge_tolerance == Decimal(witness[0])
