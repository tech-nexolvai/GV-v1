"""`scripts/pariksha_countertops.py`: the countertop-level scorecard from a stored run (#1321).

A synthetic stored run on the test database, an invented key and box map in temporary folders. No
model call, no client value.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from tests.eval.test_pariksha_countertops import (
    _add_finding,
    _add_row_question,
    _row_candidate,
    _width_rule,
)
from tests.scripts.test_pariksha_row_boxes import LINE, PICTURE, box_map, rows_key

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import pariksha_countertops

pytest_plugins = ("tests.app.postgres_fixture",)

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def engine(postgres_engine: Engine) -> Engine:
    from alembic import command
    from tests.app.postgres_fixture import alembic_config

    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    return postgres_engine


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    from app.db.session import session_factory

    opened = session_factory(engine)()
    try:
        yield opened
    finally:
        opened.close()


def test_the_script_prints_and_writes_the_countertop_scorecard(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.workflow.test_slot_reader import _scaffold

    revision, version, page, run = _scaffold(session)
    row = _row_candidate(session, version, page, run, ["slot-reader", "row-choice:2"])
    _add_finding(
        session, revision, _width_rule(session), row, "FAIL", datetime(2026, 1, 1, tzinfo=UTC)
    )
    _add_row_question(session, run, version, PICTURE)
    session.commit()
    key = rows_key(tmp_path / "keys", {"row_at": LINE})
    out = tmp_path / "cards" / "countertops.json"
    command = [
        "--db",
        engine.url.render_as_string(hide_password=False),
        "--key",
        str(key),
        "--row-boxes",
        str(box_map(tmp_path)),
        "--out",
    ]

    assert pariksha_countertops.main([*command, str(out)]) == 0

    printed = capsys.readouterr().out
    assert "decided automatically: 1 of 1" in printed
    # The database's password is never printed.
    assert engine.url.password and f":{engine.url.password}@" not in printed
    written = json.loads(out.read_text())
    # The right line, but the FAIL's numbers have no key: counted apart, never "right".
    assert written["totals"]["automatic_right_line_numbers_not_keyed"] == 1
    assert written["totals"]["automatic_right"] == 0
    assert written["lines"][0]["line"] == "right" and written["lines"][0]["right"] is None
    assert "NOT checked" in printed
    inside = REPO / "countertops-should-not-exist.json"
    assert pariksha_countertops.main([*command, str(inside)]) == 2
    assert not inside.exists()
