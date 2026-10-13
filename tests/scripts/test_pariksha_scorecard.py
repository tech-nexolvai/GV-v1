"""`scripts/pariksha_scorecard.py`: score a stored run into a stored scorecard, and compare two (#1219).

Synthetic stored runs on the test database and synthetic keys only. No model call.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from eval.pariksha.keys import KEY_FORMAT
from tests.eval.test_pariksha_stored import PAGE_HASH, slot_box_of, synthetic_run

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import pariksha_scorecard

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


def _key(tmp_path: Path, name: str = "synthetic") -> Path:
    path = tmp_path / f"key-{name}.json"
    path.write_text(
        json.dumps(
            {
                "format": KEY_FORMAT,
                "agent": "akshara",
                "name": name,
                "version": "1",
                "cases": [
                    {
                        "family": "claude-slot-span",
                        "page_sha256": PAGE_HASH,
                        "question_id": question_id,
                        "slot_box": slot_box_of(question_id),
                        "answer": {"texts": [text]},
                    }
                    for question_id, text in (("p0:slot0:0", '3/4"'), ("p0:slot1:0", '1/2"'))
                ],
            }
        )
    )
    return path


def _score(engine: Engine, key: Path, out: Path, code: str, run: object | None = None) -> int:
    return pariksha_scorecard.main(
        [
            "score",
            "--agent",
            "akshara",
            "--db",
            engine.url.render_as_string(hide_password=False),
            "--key",
            str(key),
            "--code-version",
            code,
            "--out",
            str(out),
            *([] if run is None else ["--run", str(run)]),
        ]
    )


def test_score_writes_a_scorecard_and_compare_rejects_a_new_wrong_answer(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = _key(tmp_path)
    _version, old_run = synthetic_run(
        session, {"p0:slot0:0": ('3/4"', '3/4"'), "p0:slot1:0": ('1/2"', '3/4"')}
    )
    # Usually each stored run lives in its own database; two runs in one stand in for that.
    _version, new_run = synthetic_run(
        session, {"p0:slot0:0": ('3/4"', '3/4"'), "p0:slot1:0": ('3/4"', '3/4"')}
    )
    before, after = tmp_path / "before.json", tmp_path / "after.json"

    assert _score(engine, key, before, "a1b2c3d", old_run.id) == 0
    printed = capsys.readouterr().out
    assert "pair_agreed_right: 1/2" in printed
    card = json.loads(before.read_text())
    assert card["agent"] == "akshara"
    assert card["code_version"] == "a1b2c3d"
    assert card["source"]["extraction_run_id"] == str(old_run.id)

    assert _score(engine, key, after, "b2c3d4e", new_run.id) == 0
    capsys.readouterr()

    assert pariksha_scorecard.main(["compare", str(before), str(after)]) == 1
    assert "REJECT (1 new wrong:" in capsys.readouterr().out

    assert pariksha_scorecard.main(["compare", str(before), str(before)]) == 1
    assert "REJECT (not better" in capsys.readouterr().out


def test_compare_exits_zero_on_accept(tmp_path: Path, engine: Engine, session: Session) -> None:
    key = _key(tmp_path)
    _version, old_run = synthetic_run(
        session, {"p0:slot0:0": ('3/4"', '3/4"'), "p0:slot1:0": ('1/2"', '3/4"')}
    )
    _version, new_run = synthetic_run(
        session, {"p0:slot0:0": ('3/4"', '3/4"'), "p0:slot1:0": ('1/2"', '1/2"')}
    )
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    assert _score(engine, key, before, "a1b2c3d", old_run.id) == 0
    assert _score(engine, key, after, "b2c3d4e", new_run.id) == 0

    assert pariksha_scorecard.main(["compare", str(before), str(after)]) == 0


def test_a_scorecard_is_never_written_inside_a_git_working_tree(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A scorecard holds what the readers read, which is client data: it stays out of git, this
    repository's or any other's."""
    synthetic_run(session, {"p0:slot0:0": ('3/4"', '3/4"')})
    assert _score(engine, _key(tmp_path), REPO / "pariksha-card.json", "a1b2c3d") == 2
    assert not (REPO / "pariksha-card.json").exists()
    other = tmp_path / "other-repo"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    assert _score(engine, _key(tmp_path), other / "cards" / "card.json", "a1b2c3d") == 2
    assert not (other / "cards" / "card.json").exists()
    assert "git working tree" in capsys.readouterr().err


def test_refusals_exit_2(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """0 is ACCEPT and 1 is REJECT; anything the tool refuses to judge is 2."""
    _version, run = synthetic_run(session, {"p0:slot0:0": ('3/4"', '3/4"')})
    # A code version that is no commit.
    assert _score(engine, _key(tmp_path), tmp_path / "x.json", "main", run.id) == 2
    # Two scorecards of different keys.
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    assert _score(engine, _key(tmp_path), first, "a1b2c3d", run.id) == 0
    assert _score(engine, _key(tmp_path, "other"), second, "a1b2c3d", run.id) == 0
    assert pariksha_scorecard.main(["compare", str(first), str(second)]) == 2
    # A scorecard edited after it was written.
    stored = json.loads(first.read_text())
    stored["cases"][0]["outcome"] = "agreed_wrong"
    first.write_text(json.dumps(stored))
    assert pariksha_scorecard.main(["compare", str(first), str(first)]) == 2
    assert "changed after it was written" in capsys.readouterr().err


def test_an_unrecorded_code_version_is_flagged_in_the_output(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _version, run = synthetic_run(session, {"p0:slot0:0": ('3/4"', '3/4"')})

    assert _score(engine, _key(tmp_path), tmp_path / "card.json", "unrecorded", run.id) == 0

    assert "NOT ATTRIBUTABLE" in capsys.readouterr().out
