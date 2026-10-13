"""`scripts/run_check.py` prints the run check, saves it as JSON, and reads only (#1220).

Synthetic values only.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

import scripts.run_check as run_check_script
from alembic import command
from app.db.session import session_factory
from app.models.verdicts import CheckRun, Finding
from tests.app.postgres_fixture import alembic_config
from tests.eval.test_run_check import _rows

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


def _url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def test_the_script_prints_the_report_and_saves_it_as_json(
    session: Session,
    postgres_engine: Engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    revision_id, _anchors = _rows(session, tmp_path, row_pages=(0,))
    session.commit()
    before = session.execute(select(func.count()).select_from(Finding)).scalar_one()
    runs = session.execute(select(func.count()).select_from(CheckRun)).scalar_one()
    out = tmp_path / "report.json"

    code = run_check_script.main(
        [str(revision_id), "--json", str(out), "--database-url", _url(postgres_engine)]
    )

    assert code == 0
    printed = capsys.readouterr().out
    assert "Suggested action: re-run Rekha" in printed
    assert "silent-page" in printed
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["package_revision_id"] == str(revision_id)
    assert saved["action"] == "re-run Rekha"
    assert [item["code"] for item in saved["items"]] == ["silent-page"]
    assert saved["not_computable"][0]["check"] == "wording"
    session.expire_all()
    assert session.execute(select(func.count()).select_from(Finding)).scalar_one() == before
    assert session.execute(select(func.count()).select_from(CheckRun)).scalar_one() == runs


def test_a_clean_revision_prints_ready(
    session: Session,
    postgres_engine: Engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    revision_id, _anchors = _rows(session, tmp_path)
    session.commit()

    assert run_check_script.main([str(revision_id), "--database-url", _url(postgres_engine)]) == 0
    printed = capsys.readouterr().out
    assert "Suggested action: ready" in printed
    assert "Every check that could be computed held." in printed


def test_an_unknown_revision_exits_with_two(
    session: Session, postgres_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    session.commit()
    assert run_check_script.main([str(uuid4()), "--database-url", _url(postgres_engine)]) == 2
    assert "no package revision" in capsys.readouterr().err
