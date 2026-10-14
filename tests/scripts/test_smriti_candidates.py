"""`scripts/smriti_candidates.py`: export candidates, list them, confirm some into a local key (#1232).

Synthetic decisions on the test database and invented confirmations only. No model call.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from eval.pariksha.keys import AnswerKey, read_key
from eval.smriti.confirm import CONFIRMATIONS_FORMAT
from tests.eval.test_pariksha_stored import PAGE_HASH, slot_box_of, synthetic_run
from tests.eval.test_smriti_export import build_world

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import pariksha_scorecard
import smriti_candidates

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


@pytest.fixture(autouse=True)
def _known_reviewer(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local real-reviewer list (#1267), naming the invented reviewer of `build_world`."""
    from eval.smriti.actors import REVIEWERS_ENV, REVIEWERS_FORMAT
    from tests.eval.test_smriti_export import REVIEWER

    people = tmp_path_factory.mktemp("local") / "people.json"
    people.write_text(json.dumps({"format": REVIEWERS_FORMAT, "reviewers": [REVIEWER]}))
    monkeypatch.setenv(REVIEWERS_ENV, str(people))


def _url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def test_export_list_and_confirm_write_a_local_key_and_nothing_else(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.models.evidence import NoCountertopAnswer

    world = build_world(session)
    candidates = tmp_path / "candidates.json"
    assert smriti_candidates.main(["export", "--db", _url(engine), "--out", str(candidates)]) == 0
    printed = capsys.readouterr().out
    assert "rekha" in printed and "akshara" in printed
    assert "needs a person's confirmation" in printed

    assert smriti_candidates.main(["list", str(candidates), "--agent", "rekha"]) == 0
    # The counts come first, then only Rekha's candidates that can become key cases.
    listed = capsys.readouterr().out.split("can become key cases", 1)[1]
    assert f"- no_countertop_answers:{world.no_countertop_reader}" in listed
    assert "- architect_pairing_records" not in listed
    assert "- slot_row_review_decisions" not in listed

    # Exporting wrote no key: a candidate is only a candidate.
    assert sorted(path.name for path in tmp_path.iterdir()) == ["candidates.json"]

    confirmations = tmp_path / "confirmations.json"
    confirmations.write_text(
        json.dumps(
            {
                "format": CONFIRMATIONS_FORMAT,
                "confirmed_by": "a person",
                "key": {"agent": "rekha", "name": "invented", "version": "1"},
                "confirm": [{"candidate": f"no_countertop_answers:{world.no_countertop_reader}"}],
            }
        )
    )
    key_path = tmp_path / "key.json"
    assert (
        smriti_candidates.main(
            [
                "confirm",
                str(candidates),
                "--confirmations",
                str(confirmations),
                "--out",
                str(key_path),
            ]
        )
        == 0
    )
    key = read_key(key_path)
    assert isinstance(key, AnswerKey)
    assert [case.question.question_id for case in key.cases] == ["p0:row-choice"]
    # The database was only read.
    assert session.scalar(select(func.count()).select_from(NoCountertopAnswer)) == 4
    # Written once: a second confirm to the same file is refused.
    assert (
        smriti_candidates.main(
            [
                "confirm",
                str(candidates),
                "--confirmations",
                str(confirmations),
                "--out",
                str(key_path),
            ]
        )
        == smriti_candidates.REFUSED
    )


@pytest.mark.parametrize("command", ["export", "confirm"])
def test_an_output_inside_a_git_working_tree_is_refused(
    tmp_path: Path, command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    inside = REPO / "smriti-should-never-exist.json"
    arguments = (
        ["export", "--db", "postgresql+psycopg://nobody@localhost:1/none", "--out", str(inside)]
        if command == "export"
        else [
            "confirm",
            str(tmp_path / "candidates.json"),
            "--confirmations",
            str(tmp_path / "confirmations.json"),
            "--out",
            str(inside),
        ]
    )
    assert smriti_candidates.main(arguments) == smriti_candidates.REFUSED
    assert "git working tree" in capsys.readouterr().err
    assert not inside.exists()


def test_a_confirmation_without_a_person_refuses_and_writes_nothing(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    build_world(session)
    candidates = tmp_path / "candidates.json"
    assert smriti_candidates.main(["export", "--db", _url(engine), "--out", str(candidates)]) == 0
    confirmations = tmp_path / "confirmations.json"
    confirmations.write_text(
        json.dumps(
            {
                "format": CONFIRMATIONS_FORMAT,
                "confirmed_by": "a person",
                "key": {"agent": "rekha", "name": "invented", "version": "1"},
                "confirm": [],
            }
        )
    )
    key_path = tmp_path / "key.json"
    assert (
        smriti_candidates.main(
            [
                "confirm",
                str(candidates),
                "--confirmations",
                str(confirmations),
                "--out",
                str(key_path),
            ]
        )
        == smriti_candidates.REFUSED
    )
    assert "nothing" in capsys.readouterr().err
    assert not key_path.exists()


def test_the_pariksha_report_shows_the_agents_reviewer_corrections(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    build_world(session)
    candidates = tmp_path / "candidates.json"
    assert smriti_candidates.main(["export", "--db", _url(engine), "--out", str(candidates)]) == 0
    capsys.readouterr()

    _version, run = synthetic_run(session, {"p0:slot0:0": ('3/4"', '3/4"')})
    key = tmp_path / "akshara-key.json"
    key.write_text(
        json.dumps(
            {
                "format": "pariksha-key/1",
                "agent": "akshara",
                "name": "invented",
                "version": "1",
                "cases": [
                    {
                        "family": "claude-slot-span",
                        "page_sha256": PAGE_HASH,
                        "question_id": "p0:slot0:0",
                        "slot_box": slot_box_of("p0:slot0:0"),
                        "answer": {"texts": ['3/4"']},
                    }
                ],
            }
        )
    )
    assert (
        pariksha_scorecard.main(
            [
                "score",
                "--agent",
                "akshara",
                "--db",
                _url(engine),
                "--key",
                str(key),
                "--run",
                str(run.id),
                "--code-version",
                "a1b2c3d",
                "--out",
                str(tmp_path / "card.json"),
                "--smriti",
                str(candidates),
            ]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "reviewer decisions on akshara: 3" in printed
