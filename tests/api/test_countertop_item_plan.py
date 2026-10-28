"""The results name each row's confirmed countertop, and each countertop has a plan (#1245).

Verification for `countertop_item` on `GET …/countertop-results` and for
`GET …/countertop-items/plan` (`app/api/visual_countertops.py`, `workflow/item_plan.py`). Both read
only. Every value is made up.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.models import Package
from storage.local import LocalStore
from tests.app.countertop_results import countertop_results
from tests.workflow.test_bala_items import _Revision
from tests.workflow.test_item_plan import _confirm_bala_countertop
from tests.workflow.test_part_proposals_route import _upgrade
from tests.workflow.test_stages import _publish_rulebook
from workflow.bala_items import propose_items
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _two_rows_one_confirmed(
    session: Session, tmp_path: Path
) -> tuple[_Revision, tuple[UUID, UUID], UUID]:
    built = _Revision(session)
    run = built.run()
    confirmed = built.row(run, page=0)
    unconfirmed = built.row(run, page=1)
    propose_items(session, built.revision.id)
    item = _confirm_bala_countertop(session, confirmed)
    _publish_rulebook(session)
    DatabaseStages(LocalStore(root=tmp_path, ticket_secret=b"synthetic-test-only")).run_checks(
        session, built.revision.id
    )
    session.flush()
    return built, (confirmed, item), unconfirmed


def test_each_row_names_the_countertop_confirmed_from_it(session: Session, tmp_path: Path) -> None:
    built, (anchor, item), other = _two_rows_one_confirmed(session, tmp_path)
    package = session.get_one(Package, built.revision.package_id)

    response = countertop_results(session, package.project_id, package.id)

    assert response.status_code == 200, response.text
    rows = {row["row_id"]: row for row in response.json()["items"]}
    assert rows[str(anchor)]["countertop_item"] == {
        "item_id": str(item),
        "page_number": 1,
        "number": 1,
        "label": "Countertop 1 on page 1",
    }
    assert rows[str(other)]["countertop_item"] is None
