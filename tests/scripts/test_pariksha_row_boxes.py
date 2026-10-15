"""Geometry row keys from the command line (#1321): convert a key, score and judge with a box map.

`scripts/pariksha_row_boxes.py convert`, and the `--row-boxes` option of
`scripts/pariksha_scorecard.py score` and `scripts/pariksha_library.py compare`. Synthetic stored
runs on the test database, invented keys, pictures and lines in temporary folders. No model call.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from eval.pariksha.keys import KEY_FORMAT, AnswerKey, read_key
from eval.pariksha.row_geometry import ROW_BOXES_FORMAT

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import pariksha_library
import pariksha_row_boxes
import pariksha_scorecard

pytest_plugins = ("tests.app.postgres_fixture",)

REPO = Path(__file__).resolve().parents[2]
PAGE = "1" * 64  # the scaffold's page hash
PICTURE = "2" * 64  # the stored test packets' picture
LINE = {"y": 412.5, "x0": 100, "x1": 400}
CABINETS = {"y": 430.25, "x0": 110, "x1": 390}
OPUS = "anthropic.claude-opus-5-5"
SONNET = "anthropic.claude-sonnet-5-5"


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


def write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def box_map(folder: Path, boxes: list[dict[str, Any]] | None = None) -> Path:
    return write_json(
        folder / "boxes.json",
        {
            "format": ROW_BOXES_FORMAT,
            "pictures": [
                {
                    "picture_sha256": PICTURE,
                    "page_sha256": PAGE,
                    "boxes": boxes or [CABINETS, LINE],
                    "how": "invented for a test",
                }
            ],
        },
    )


def rows_key(folder: Path, answer: dict[str, Any], name: str = "rows") -> Path:
    data: dict[str, Any] = {
        "format": KEY_FORMAT,
        "agent": "rekha",
        "name": name,
        "version": "1",
        "cases": [
            {
                "family": "slot-row-choice",
                "page_sha256": PAGE,
                "question_id": "p0:row-choice",
                "answer": answer,
            }
        ],
    }
    if "row_at" in answer:
        data["row_tolerance_pt"] = 1
    else:
        data["cases"][0]["picture_sha256"] = PICTURE
    return write_json(folder / f"{name}.json", data)


def geometry_run(session: Session, first: int, second: int) -> Any:
    """One stored run: both readers answer the row question on the invented picture."""
    from tests.eval.test_pariksha_stored import add_answer, row_answer
    from tests.workflow.test_slot_reader import _scaffold

    _revision, version, _page, run = _scaffold(session)
    candidates = [str(uuid4()), str(uuid4())]
    for model, box in ((OPUS, first), (SONNET, second)):
        add_answer(
            session,
            run.id,
            version.id,
            question_id="p0:row-choice",
            prompt_id="slot-row-choice-v5",
            model_id=model,
            raw=row_answer(box),
            candidate_ids=candidates,
        )
    session.commit()
    return run


def test_convert_writes_a_geometry_key_that_names_the_line_not_the_box(tmp_path: Path) -> None:
    key = rows_key(tmp_path / "keys", {"box": 2})
    boxes = box_map(tmp_path)
    out = tmp_path / "keys" / "rows-geometry.json"

    assert (
        pariksha_row_boxes.main(
            [
                "convert",
                "--key",
                str(key),
                "--row-boxes",
                str(boxes),
                "--tolerance-pt",
                "1",
                "--name",
                "rows-geometry",
                "--version",
                "2",
                "--out",
                str(out),
            ]
        )
        == 0
    )

    converted = read_key(out)
    assert isinstance(converted, AnswerKey)
    assert converted.cases[0].truth.at is not None  # type: ignore[union-attr]
    assert converted.cases[0].picture_sha256 is None
    # Written once: the same output again is refused, and so is a path inside a git checkout.
    again = ["convert", "--key", str(key), "--row-boxes", str(boxes), "--tolerance-pt", "1"]
    assert (
        pariksha_row_boxes.main([*again, "--name", "g", "--version", "2", "--out", str(out)]) == 2
    )
    inside = REPO / "rows-geometry-should-not-exist.json"
    assert (
        pariksha_row_boxes.main([*again, "--name", "g", "--version", "2", "--out", str(inside)])
        == 2
    )
    assert not inside.exists()


def test_convert_refuses_a_box_it_cannot_place(tmp_path: Path) -> None:
    key = rows_key(tmp_path / "keys", {"box": 3})

    assert (
        pariksha_row_boxes.main(
            [
                "convert",
                "--key",
                str(key),
                "--row-boxes",
                str(box_map(tmp_path)),
                "--tolerance-pt",
                "1",
                "--name",
                "g",
                "--version",
                "2",
                "--out",
                str(tmp_path / "out.json"),
            ]
        )
        == 2
    )
    assert not (tmp_path / "out.json").exists()


def _score(engine: Engine, key: Path, out: Path, run: Any, *extra: str) -> int:
    return pariksha_scorecard.main(
        [
            "score",
            "--agent",
            "rekha",
            "--db",
            engine.url.render_as_string(hide_password=False),
            "--key",
            str(key),
            "--code-version",
            "a1b2c3d",
            "--run",
            str(run.id),
            "--out",
            str(out),
            *extra,
        ]
    )


def test_the_scorecard_matches_the_readers_box_by_geometry_with_a_box_map(
    engine: Engine, session: Session, tmp_path: Path
) -> None:
    key = rows_key(tmp_path / "keys", {"row_at": LINE})
    run = geometry_run(session, 2, 2)

    assert (
        _score(engine, key, tmp_path / "with.json", run, "--row-boxes", str(box_map(tmp_path))) == 0
    )
    assert _score(engine, key, tmp_path / "without.json", run) == 0

    with_map = json.loads((tmp_path / "with.json").read_text())
    without = json.loads((tmp_path / "without.json").read_text())
    assert [case["outcome"] for case in with_map["cases"]] == ["agreed_right"]
    assert [case["outcome"] for case in without["cases"]] == ["not_asked"]


def test_the_library_judges_a_geometry_key_with_the_box_map(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = tmp_path / "library.json"
    key = rows_key(tmp_path / "keys", {"row_at": LINE})
    assert (
        pariksha_library.main(
            [
                "register",
                "--library",
                str(library),
                "--key",
                str(key),
                "--confirmed-by",
                "a made-up person",
                "--label",
                "invented",
            ]
        )
        == 0
    )
    before, after = geometry_run(session, 1, 1), geometry_run(session, 2, 2)
    url = engine.url.render_as_string(hide_password=False)
    judge = [
        "compare",
        "--library",
        str(library),
        "--agent",
        "rekha",
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
    ]
    capsys.readouterr()

    assert pariksha_library.main([*judge, "--row-boxes", str(box_map(tmp_path))]) == 0
    assert "ACCEPT" in capsys.readouterr().out
    # Without the box map no geometry case is asked: never a pass.
    assert pariksha_library.main(judge) == 3
