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
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.main import API_PREFIX
from app.models import Finding, Package
from storage.local import LocalStore
from tests.api.test_visual_ui import _client
from tests.workflow.test_bala_items import _Revision
from tests.workflow.test_item_plan import _confirm_bala_countertop, _reread_rows
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

    response = _client(session, package.project_id).get(
        f"{API_PREFIX}/projects/{package.project_id}/packages/{package.id}/countertop-results"
    )

    assert response.status_code == 200, response.text
    rows = {row["row_id"]: row for row in response.json()["items"]}
    assert rows[str(anchor)]["countertop_item"] == {
        "item_id": str(item),
        "page_number": 1,
        "number": 1,
        "label": "Countertop 1 on page 1",
    }
    assert rows[str(other)]["countertop_item"] is None


def test_the_plan_lists_each_countertop_and_reads_only(session: Session, tmp_path: Path) -> None:
    built, (anchor, item), _other = _two_rows_one_confirmed(session, tmp_path)
    package = session.get_one(Package, built.revision.package_id)
    findings_before = session.scalar(select(func.count()).select_from(Finding))

    client = _client(session, package.project_id)
    response = client.get(
        f"{API_PREFIX}/projects/{package.project_id}/packages/{package.id}/countertop-items/plan"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revision_id"] == str(built.revision.id)
    (entry,) = body["items"]
    assert entry["countertop"]["item_id"] == str(item)
    assert entry["row_id"] == str(anchor)
    assert entry["note"] is None
    width = next(rule for rule in entry["rules"] if rule["rule_id"] == "CT-WIDTH-001")
    assert [finding["scope"] for finding in width["findings"]] == ["row"]
    assert session.scalar(select(func.count()).select_from(Finding)) == findings_before

    elsewhere = client.get(
        f"{API_PREFIX}/projects/{package.project_id}/packages/{built.revision.id}"
        "/countertop-items/plan"
    )
    assert elsewhere.status_code == 404


def _results_statements(session: Session, tmp_path: Path, pages: int, *, publish: bool) -> int:
    """How many statements the countertop results ask after a re-read of `pages` pages, each page's
    countertop confirmed on the first reading and skipped by Bala on the second."""
    built = _Revision(session, pages=pages)
    newest = _reread_rows(session, built, pages)
    if publish:
        _publish_rulebook(session)
    DatabaseStages(LocalStore(root=tmp_path, ticket_secret=b"synthetic-test-only")).run_checks(
        session, built.revision.id
    )
    session.flush()
    package = session.get_one(Package, built.revision.package_id)
    client = _client(session, package.project_id)
    url = f"{API_PREFIX}/projects/{package.project_id}/packages/{package.id}/countertop-results"
    client.get(url)  # warm: the first request of a client loads what every request shares
    session.expire_all()
    seen: list[object] = []

    def count(*_args: object) -> None:
        seen.append(None)

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", count)
    try:
        response = client.get(url)
    finally:
        event.remove(bind, "before_cursor_execute", count)
    assert response.status_code == 200, response.text
    named = {
        row["row_id"] for row in response.json()["items"] if row["countertop_item"] is not None
    }
    assert named == {str(anchor) for anchor in newest}
    return len(seen)


def test_after_a_re_read_the_results_ask_the_same_number_of_statements_for_any_number_of_rows(
    session: Session, tmp_path: Path
) -> None:
    """Review of #1247 (M3): following the confirmed countertops to a re-read's rows used to rebuild
    Bala's whole plan, one query per row (13 statements with 2 rows, 21 with 6). It now costs the
    same whatever the number of rows."""
    two = _results_statements(session, tmp_path, 2, publish=True)
    six = _results_statements(session, tmp_path, 6, publish=False)
    assert two == six
