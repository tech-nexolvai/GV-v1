"""`scripts/pariksha_library.py`: register keys on the local list, and judge a change by all of them.

Synthetic stored runs on the test database, invented keys and an invented library in temporary
folders. No model call, no client value.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from eval.pariksha.keys import KEY_FORMAT
from eval.smriti.library import LIBRARY_SETTING, read_library
from tests.eval.test_pariksha_depth import depth_key
from tests.eval.test_pariksha_stored import PAGE_HASH, slot_box_of, synthetic_run

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import pariksha_library

pytest_plugins = ("tests.app.postgres_fixture",)

REPO = Path(__file__).resolve().parents[2]
UNREAD_PAGE = "e" * 64


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


def _label_key(folder: Path, name: str, page: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.json"
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
                        "page_sha256": page,
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


def _register(library: Path, key: Path, label: str = "invented, not human-confirmed") -> int:
    return pariksha_library.main(
        [
            "register",
            "--library",
            str(library),
            "--key",
            str(key),
            "--confirmed-by",
            "a made-up person",
            "--label",
            label,
        ]
    )


def _compare(engine: Engine, library: Path, before: Any, after: Any, *extra: str) -> int:
    url = engine.url.render_as_string(hide_password=False)
    return pariksha_library.main(
        [
            "compare",
            "--library",
            str(library),
            "--agent",
            "akshara",
            "--before-db",
            url,
            "--before-run",
            str(before.id),
            "--before-code",
            "a1b2c3d",
            "--after-db",
            url,
            "--after-run",
            str(after.id),
            "--after-code",
            "b2c3d4e",
            *extra,
        ]
    )


def test_register_writes_the_local_list_and_list_prints_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = tmp_path / "shelf" / "library.json"
    key = _label_key(tmp_path / "keys", "made-up labels", PAGE_HASH)

    assert _register(library, key) == 0
    assert _register(library, key) == 0  # the same key again changes nothing
    (entry,) = read_library(library).entries
    assert entry.path == str(key.resolve())
    capsys.readouterr()

    assert pariksha_library.main(["list", "--library", str(library)]) == 0
    printed = capsys.readouterr().out
    assert "akshara" in printed
    assert "made-up labels" in printed
    assert "invented, not human-confirmed" in printed


def test_register_refuses_a_library_inside_a_git_checkout_or_without_a_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _label_key(tmp_path / "keys", "made-up labels", PAGE_HASH)
    assert _register(REPO / "library.json", key) == 2
    assert "inside a git" in capsys.readouterr().err
    assert not (REPO / "library.json").exists()

    for command in (
        ["list"],
        [
            "compare",
            "--agent",
            "akshara",
            "--before-db",
            "x",
            "--before-code",
            "a1b2c3d",
            "--after-db",
            "y",
            "--after-code",
            "b2c3d4e",
        ],
    ):
        assert pariksha_library.main([*command, "--library", str(REPO / "library.json")]) == 2
        assert "inside a git" in capsys.readouterr().err

    monkeypatch.delenv(LIBRARY_SETTING, raising=False)
    assert pariksha_library.main(["list"]) == 2
    assert "no default" in capsys.readouterr().err

    monkeypatch.setenv(LIBRARY_SETTING, str(tmp_path / "from-setting.json"))
    assert (
        pariksha_library.main(
            ["register", "--key", str(key), "--confirmed-by", "someone", "--label", "a label"]
        )
        == 0
    )
    assert (tmp_path / "from-setting.json").exists()


def test_register_refuses_a_conflicting_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = tmp_path / "library.json"
    assert _register(library, _label_key(tmp_path / "a", "made-up labels", PAGE_HASH)) == 0
    clash = _label_key(tmp_path / "b", "another key", PAGE_HASH)
    document = json.loads(clash.read_text())
    document["cases"][0]["answer"] = {"texts": ['5/8"']}
    clash.write_text(json.dumps(document))

    assert _register(library, clash) == 2
    assert "different answer" in capsys.readouterr().err
    assert len(read_library(library).entries) == 1


def test_compare_judges_every_key_of_the_agent_and_names_the_unmeasured(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = tmp_path / "library.json"
    assert _register(library, _label_key(tmp_path / "keys", "read labels", PAGE_HASH)) == 0
    assert _register(library, _label_key(tmp_path / "keys", "unread labels", UNREAD_PAGE)) == 0
    depth = tmp_path / "keys" / "depths.json"
    depth.write_text(json.dumps(depth_key()))
    assert _register(library, depth, label="read by a made-up reader twice") == 0
    _version, old = synthetic_run(
        session, {"p0:slot0:0": ('3/4"', '3/4"'), "p0:slot1:0": ('1/2"', '3/4"')}
    )
    _version, fixed = synthetic_run(
        session, {"p0:slot0:0": ('3/4"', '3/4"'), "p0:slot1:0": ('1/2"', '1/2"')}
    )
    _version, broken = synthetic_run(
        session, {"p0:slot0:0": ('3/4"', '3/4"'), "p0:slot1:0": ('3/4"', '3/4"')}
    )
    capsys.readouterr()

    assert _compare(engine, library, old, fixed) == 0
    printed = capsys.readouterr().out
    assert "ACCEPT" in printed
    assert "1 of 3 keys measured" in printed
    assert printed.count("NOT MEASURED") >= 2
    assert UNREAD_PAGE[:12] in printed
    assert "answers" in printed  # why the depth key cannot be measured from a stored run

    assert _compare(engine, library, old, broken) == 1
    printed = capsys.readouterr().out
    assert "REJECT" in printed
    assert "read labels" in printed

    assert _compare(engine, library, fixed, fixed) == 4
    assert "NEUTRAL (nothing better, nothing worse" in capsys.readouterr().out


def test_compare_with_nothing_measured_exits_3_and_never_accepts(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = tmp_path / "library.json"
    assert _register(library, _label_key(tmp_path / "keys", "unread labels", UNREAD_PAGE)) == 0
    _version, old = synthetic_run(session, {"p0:slot0:0": ('3/4"', '3/4"')})
    _version, new = synthetic_run(session, {"p0:slot0:0": ('3/4"', '3/4"')})
    capsys.readouterr()

    assert _compare(engine, library, old, new) == 3
    printed = capsys.readouterr().out
    assert "ACCEPT" not in printed
    assert "NOT MEASURED" in printed


def test_compare_writes_the_scorecards_only_outside_git(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = tmp_path / "library.json"
    assert _register(library, _label_key(tmp_path / "keys", "read labels", PAGE_HASH)) == 0
    _version, old = synthetic_run(session, {"p0:slot0:0": ('3/4"', '3/4"')})
    _version, new = synthetic_run(session, {"p0:slot0:0": ('3/4"', '3/4"')})

    assert _compare(engine, library, old, new, "--cards", str(REPO / "cards")) == 2
    assert "inside a git" in capsys.readouterr().err
    assert not (REPO / "cards").exists()

    cards = tmp_path / "cards"
    assert _compare(engine, library, old, new, "--cards", str(cards)) == 4  # unchanged: neutral
    assert len(list(cards.glob("*.json"))) == 2


def test_the_script_runs_from_the_command_line(tmp_path: Path) -> None:
    library = tmp_path / "library.json"
    key = _label_key(tmp_path / "keys", "made-up labels", PAGE_HASH)
    done = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "pariksha_library.py"),
            "register",
            "--library",
            str(library),
            "--key",
            str(key),
            "--confirmed-by",
            "someone",
            "--label",
            "a label",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert read_library(library).entries
