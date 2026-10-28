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
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.api.dependencies import get_artifact_store, get_session
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models import (
    CountertopRun,
    DrawingView,
    Package,
    PackageRevision,
    ViewRole,
)
from storage.local import LocalStore
from tests.api.test_v1_loop import _settings
from tests.workflow.test_part_proposals_route import _extract, _upgrade
from tests.workflow.test_stages import _publish_rulebook
from workflow.countertop_runs import live_run_rows
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


@pytest.fixture
def sheet(session: Session, store: LocalStore) -> Sheet:
    return Sheet(session, store)


# -- the suggestion --------------------------------------------------------------------------------


# -- deciding --------------------------------------------------------------------------------------


# -- what is refused -------------------------------------------------------------------------------


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
