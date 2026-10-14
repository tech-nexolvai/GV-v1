"""Each confirmed countertop's architect comparison over the API (#1273, Milan by item).

Verification for `GET …/countertop-items/architect` (`app/api/item_architect.py`): one entry per
live countertop, "not compared" with the stored reason, and it reads only. Every value is made up.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.api import visual_countertops
from app.db.session import session_factory
from app.main import API_PREFIX
from app.models import Finding, Package
from storage.local import LocalStore
from tests.api.test_visual_architect import _items
from tests.api.test_visual_ui import _client
from tests.workflow.test_architect_row_evidence import (
    _architect_drawing,
    _architect_value,
    _overall,
    _pairing,
    _run,
    _sealed_rows,
)
from tests.workflow.test_bala_items import _Revision
from tests.workflow.test_item_plan import _confirm_bala_countertop
from tests.workflow.test_milan_items import _countertop_on_row, _nothing_comparable
from tests.workflow.test_part_proposals_route import _upgrade
from tests.workflow.test_stages import _publish_rulebook
from workflow import milan_items
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


def test_each_confirmed_countertop_says_it_is_not_compared_and_why(
    session: Session, tmp_path: Path
) -> None:
    built = _Revision(session)
    run = built.run()
    anchor = built.row(run, page=0)
    built.row(run, page=1)  # never confirmed: not listed
    propose_items(session, built.revision.id)
    item = _confirm_bala_countertop(session, anchor)
    _nothing_comparable(session, built, anchor, "The architect prints only wall lengths here")
    _publish_rulebook(session)
    DatabaseStages(LocalStore(root=tmp_path, ticket_secret=b"synthetic-test-only")).run_checks(
        session, built.revision.id
    )
    session.flush()
    package = session.get_one(Package, built.revision.package_id)
    findings_before = session.scalar(select(func.count()).select_from(Finding))
    client = _client(session, package.project_id)

    response = client.get(
        f"{API_PREFIX}/projects/{package.project_id}/packages/{package.id}"
        "/countertop-items/architect"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revision_id"] == str(built.revision.id)
    assert body["items"] == [
        {
            "countertop": {
                "item_id": str(item),
                "page_number": 1,
                "number": 1,
                "label": "Countertop 1 on page 1",
            },
            "row_id": str(anchor),
            "state": "not_compared",
            "reason": "The architect prints only wall lengths here.",
            "finding_id": None,
            "outcome": None,
            "pairs": [],
            "pairing_source": "none",
            "pairing_judgments": None,
        }
    ]
    assert session.scalar(select(func.count()).select_from(Finding)) == findings_before

    elsewhere = client.get(
        f"{API_PREFIX}/projects/{package.project_id}/packages/{built.revision.id}"
        "/countertop-items/architect"
    )
    assert elsewhere.status_code == 404


def test_no_confirmed_countertop_lists_nothing(session: Session) -> None:
    built = _Revision(session)
    built.row(built.run(), page=0)
    propose_items(session, built.revision.id)
    session.flush()
    package = session.get_one(Package, built.revision.package_id)

    response = _client(session, package.project_id).get(
        f"{API_PREFIX}/projects/{package.project_id}/packages/{package.id}"
        "/countertop-items/architect"
    )

    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


def _endpoint(session: Session, package_id: UUID) -> dict[str, dict[str, Any]]:
    package = session.get_one(Package, package_id)
    response = _client(session, package.project_id).get(
        f"{API_PREFIX}/projects/{package.project_id}/packages/{package_id}"
        "/countertop-items/architect"
    )
    assert response.status_code == 200, response.text
    return {entry["row_id"]: entry for entry in response.json()["items"]}


@pytest.mark.parametrize(
    "case", ["recorded FAIL", "one judgment", "unusable value", "paired after the run"]
)
def test_the_countertop_line_never_contradicts_its_row_on_the_results(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Review of #1279: the endpoint says what the countertop results show for the same row: the
    same recorded result, or, with none, the same words."""
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(
        session, run, anchors[0], "3' - 6\"", unit="mm" if case == "unusable value" else "in"
    )
    pairing = _pairing(_overall(overall), source="code" if case == "one judgment" else "code+ais")
    _run(
        session,
        package_id,
        tmp_path,
        {} if case == "paired after the run" else {anchors[0]: pairing},
    )
    if case == "paired after the run":
        for module in (visual_countertops, milan_items):
            monkeypatch.setattr(
                module,
                "effective_architect_pairings",
                lambda _session, rows: {anchor: pairing for anchor in rows},
            )
    _countertop_on_row(session, anchors[0])

    block = _items(session, package_id)[str(anchors[0])]["architect"]
    line = _endpoint(session, package_id)[str(anchors[0])]

    assert line["finding_id"] == block["finding_id"]
    if block["finding_id"] is None:
        assert (line["state"], line["reason"]) == ("not_checked_yet", block["not_compared_reason"])
        assert line["pairs"] == []
    else:
        assert (line["outcome"], line["reason"]) == (block["outcome"], block["reason"])
        assert line["state"] == (
            "compared" if block["outcome"] in ("PASS", "FAIL") else "for_reviewer"
        )
        assert [pair["vendor_piece"] for pair in line["pairs"]] == [
            pair["vendor_piece"] for pair in block["compared"]
        ]
    expected = {
        "recorded FAIL": ("compared", "FAIL"),
        "one judgment": ("for_reviewer", "REVIEW_REQUIRED"),
        "unusable value": ("for_reviewer", "REVIEW_REQUIRED"),
        "paired after the run": ("not_checked_yet", None),
    }[case]
    assert (line["state"], line["outcome"]) == expected
