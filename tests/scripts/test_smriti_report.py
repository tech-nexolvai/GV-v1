"""`scripts/smriti_report.py`: the learner report and the person-confirmed vendor report (#1272).

An empty or invented test database only; invented names. No model call.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import Engine

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import smriti_report

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


def _url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def test_an_empty_database_reports_not_measured_and_an_empty_vendor_report(
    engine: Engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eval.smriti.actors import REVIEWERS_ENV

    monkeypatch.delenv(REVIEWERS_ENV, raising=False)
    out = tmp_path / "report.txt"
    assert smriti_report.main(["--db", _url(engine), "--vendors", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "NOT MEASURED" in printed
    assert "no package names a vendor" in printed
    assert out.read_text(encoding="utf-8").startswith("Smriti learner report")
    # Written once, never overwritten.
    assert smriti_report.main(["--db", _url(engine), "--out", str(out)]) == smriti_report.REFUSED


def test_a_report_file_inside_the_repository_is_refused(engine: Engine) -> None:
    inside = REPO / "smriti-report-must-not-exist.txt"
    assert smriti_report.main(["--db", _url(engine), "--out", str(inside)]) == smriti_report.REFUSED
    assert not inside.exists()


# --- review of #1276: wording matches the code (#1281) --------------------------------------------


def test_the_docstring_says_no_decision_counts_without_a_reviewer_list() -> None:
    doc = " ".join((smriti_report.__doc__ or "").split())
    assert 'every name counts as "other"' not in doc
    assert "without it, no decision is counted at all" in doc
    assert "the FAILs a reviewer on the list confirmed or signed" in doc


def test_the_vendor_header_says_only_a_reviewer_on_the_list_counts(
    engine: Engine,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eval.smriti.actors import REVIEWERS_ENV

    monkeypatch.delenv(REVIEWERS_ENV, raising=False)
    assert smriti_report.main(["--db", _url(engine), "--vendors"]) == 0
    printed = capsys.readouterr().out
    assert "only FAILs a reviewer on the list confirmed or signed count" in printed
    assert "only FAILs a person confirmed or signed count" not in printed
