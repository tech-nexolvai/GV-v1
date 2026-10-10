"""The countertop results and reports when the architect's drawings are their own file (#1161).

Each countertop not compared says the architect's file was not compared, and needs no decision of
its own: the one package-level line asks the reviewer (`tests/workflow/test_separate_architect_file.py`).
The reports print the same projection. Synthetic values only.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.api.visual_countertops import _countertop_results_for_revision
from app.db.session import session_factory
from app.models import ObservationCandidate, PackageRevision
from reports.spreadsheet import architect_line
from tests.api.test_visual_architect import _items
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_architect_row_evidence import (
    ARCH_RULE,
    _architect_drawing,
    _architect_value,
    _overall,
    _pairing,
    _run,
    _sealed_rows,
)
from tests.workflow.test_separate_architect_file import add_architect_file
from workflow.architect_row_plan import SEPARATE_ARCHITECT_FILE_ROW

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


def _revision(session: Session, package_id: UUID) -> PackageRevision:
    return session.query(PackageRevision).filter_by(package_id=package_id).one()


def test_every_row_not_compared_names_the_separate_file(session: Session, tmp_path: Path) -> None:
    package_id, anchors = _sealed_rows(session)
    add_architect_file(session, package_id, same_bytes_as=None)
    _run(session, package_id, tmp_path)

    items = _items(session, package_id)

    for anchor in anchors.values():
        block = items[str(anchor)]["architect"]
        assert block["finding_id"] is None
        assert block["outcome"] is None
        assert block["needs_decision"] is False
        assert block["not_compared_reason"] == SEPARATE_ARCHITECT_FILE_ROW
    report = _countertop_results_for_revision(session, package_id, _revision(session, package_id))
    assert {architect_line(item) for item in report.items} == {
        f"Matches the architect: not compared: {SEPARATE_ARCHITECT_FILE_ROW}"
    }


def test_a_compared_row_keeps_its_result_beside_a_row_naming_the_file(
    session: Session, tmp_path: Path
) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 7\"")
    add_architect_file(session, package_id, same_bytes_as=None)
    finding = _run(session, package_id, tmp_path, {anchors[0]: _pairing(_overall(overall))})[
        ARCH_RULE
    ][anchors[0]]

    items = _items(session, package_id)

    compared = items[str(anchors[0])]["architect"]
    assert compared["finding_id"] == str(finding.id)
    assert compared["outcome"] == "PASS"
    assert compared["not_compared_reason"] is None
    other = items[str(anchors[1])]["architect"]
    assert other["not_compared_reason"] == SEPARATE_ARCHITECT_FILE_ROW


def test_the_same_file_in_both_slots_keeps_the_usual_reason(
    session: Session, tmp_path: Path
) -> None:
    package_id, anchors = _sealed_rows(session)
    shop = session.get_one(ObservationCandidate, anchors[0]).document_version_id
    add_architect_file(session, package_id, same_bytes_as=shop)
    _run(session, package_id, tmp_path)

    items = _items(session, package_id)

    assert {
        items[str(anchor)]["architect"]["not_compared_reason"] for anchor in anchors.values()
    } == {"No architect dimension is paired with this row."}
