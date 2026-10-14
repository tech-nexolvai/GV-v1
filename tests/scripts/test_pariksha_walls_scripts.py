"""`scripts/pariksha_walls.py` and `scripts/pariksha_walls_key.py` (#1250).

Synthetic stored runs on the test database and an invented walls truth only. No model call, no
client value, no PDF (the key builder's PDF part is the product's own geometry code).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from eval.pariksha.keys import KeyFormatError
from eval.pariksha.walls import WALLS_AGENT, load_walls_key
from tests.eval.test_pariksha_stored import OPUS, PAGE_HASH, SONNET, add_answer
from tests.eval.test_pariksha_walls import BOX, add_wall_row, wall_reply

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import pariksha_walls
import pariksha_walls_key

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


def _run(session: Session, left: str, right: str) -> str:
    """One stored run whose two readers both answer `left`, `right` about one row."""
    from tests.workflow.test_slot_reader import _scaffold

    _revision, version, page, run = _scaffold(session)
    candidate = add_wall_row(
        session,
        version,
        page,
        run,
        ["wall-reader", "walls-held:walls-not-agreed", "wall-ink:left=False,right=False"],
        "walls: for the person",
    )
    for model in (OPUS, SONNET):
        add_answer(
            session,
            run.id,
            version.id,
            question_id="p0:walls",
            prompt_id="slot-walls-v3",
            model_id=model,
            raw=wall_reply(left, right),
            candidate_ids=[candidate],
        )
    session.commit()
    return str(run.id)


def _key(tmp_path: Path, version: str = "1") -> Path:
    path = tmp_path / f"walls-key-{version}.json"
    path.write_text(
        json.dumps(
            {
                "format": "pariksha-key/1",
                "agent": WALLS_AGENT,
                "name": "synthetic",
                "version": version,
                "cases": [
                    {
                        "family": "slot-walls",
                        "page_sha256": PAGE_HASH,
                        "question_id": "p0:walls",
                        "row_box": ",".join(str(v) for v in BOX),
                        "left": {"drawn": "wall", "site": "wall"},
                        "right": {"drawn": "open", "site": "open"},
                    }
                ],
            }
        )
    )
    return path


def _score(engine: Engine, key: Path, out: Path, run: str, code: str = "a1b2c3d") -> int:
    return pariksha_walls.main(
        [
            "score",
            "--db",
            engine.url.render_as_string(hide_password=False),
            "--key",
            str(key),
            "--run",
            run,
            "--code-version",
            code,
            "--out",
            str(out),
        ]
    )


def test_score_then_compare_accepts_better_and_rejects_a_new_wrong_end(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = _key(tmp_path)
    unsure = _run(session, "yes", "unsure")
    right = _run(session, "yes", "no")
    wrong = _run(session, "yes", "yes")
    cards = {name: tmp_path / f"{name}.json" for name in ("unsure", "right", "wrong")}

    assert _score(engine, key, cards["unsure"], unsure) == 0
    assert "pair_agreed_right: 1/2" in capsys.readouterr().out
    assert _score(engine, key, cards["right"], right) == 0
    assert _score(engine, key, cards["wrong"], wrong) == 0
    capsys.readouterr()

    assert pariksha_walls.main(["compare", str(cards["unsure"]), str(cards["right"])]) == 0
    assert "ACCEPT" in capsys.readouterr().out
    assert pariksha_walls.main(["compare", str(cards["unsure"]), str(cards["wrong"])]) == 1
    assert "REJECT (1 new wrong" in capsys.readouterr().out


def test_refusals_exit_2(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _run(session, "yes", "no")
    # Inside a git working tree: a scorecard holds client values.
    assert _score(engine, _key(tmp_path), REPO / "walls-card.json", run) == 2
    assert not (REPO / "walls-card.json").exists()
    # A code version that is no commit.
    assert _score(engine, _key(tmp_path), tmp_path / "x.json", run, "main") == 2
    # Two keys.
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    assert _score(engine, _key(tmp_path), first, run) == 0
    assert _score(engine, _key(tmp_path, "2"), second, run) == 0
    assert pariksha_walls.main(["compare", str(first), str(second)]) == 2
    # A key of another agent.
    other = tmp_path / "other.json"
    other.write_text(json.dumps({**json.loads(_key(tmp_path).read_text()), "agent": "rekha"}))
    assert _score(engine, other, tmp_path / "y.json", run) == 2
    assert "agent" in capsys.readouterr().err


# --- the key builder ----------------------------------------------------------------------------

TRUTH = {
    "sX|2|1": ["back_left_right", {"L": 1, "R": 1}],
    "sX|3|2": ["corner", {"L": 0, "R": 1}],
    "sY|9|1": ["island", {"L": 0, "R": 0}],
}
GEOMETRY = {(2, 1): ("a" * 64, (10, 20, 300, 400)), (3, 2): ("b" * 64, (50, 60, 700, 800))}


def test_the_truth_becomes_a_key_the_scorer_reads() -> None:
    rows = pariksha_walls_key.truth_rows(TRUTH, "sX")
    drawn = pariksha_walls_key.drawn_ends(
        [{"page": 3, "side": "right", "drawn": "key_unsure", "note": "check by eye"}]
    )

    document = pariksha_walls_key.walls_key_document(
        rows, drawn, GEOMETRY, name="synthetic", version="1", source="invented truth"
    )

    key = load_walls_key(document)
    first, second = key.cases
    assert first.question.question_id == "p1:walls"
    assert first.question.page_sha256 == "a" * 64
    assert first.row_box == (10, 20, 300, 400)
    assert (first.left.drawn, first.left.site, first.site_layout) == (
        "wall",
        "wall",
        "back_left_right",
    )
    # A corner is the back wall and the one end that has a wall.
    assert second.site_layout == "back_and_right"
    assert (second.left.drawn, second.left.site) == ("open", "open")
    # An unsettled drawn end is marked, never guessed; its site truth stays.
    assert (second.right.drawn, second.right.site) == ("key_unsure", "wall")
    assert second.right.note == "check by eye"


@pytest.mark.parametrize(
    "drawn",
    [
        [{"page": 3, "side": "middle", "drawn": "wall"}],
        [{"page": 3, "side": "right", "drawn": "maybe"}],
        [
            {"page": 3, "side": "right", "drawn": "wall"},
            {"page": 3, "side": "right", "drawn": "open"},
        ],
        {"page": 3},
    ],
)
def test_bad_drawn_ends_are_refused(drawn: object) -> None:
    with pytest.raises(KeyFormatError):
        pariksha_walls_key.drawn_ends(drawn)


def test_a_drawn_end_of_an_unkeyed_row_is_refused() -> None:
    rows = pariksha_walls_key.truth_rows(TRUTH, "sX")
    with pytest.raises(KeyFormatError, match="does not key"):
        pariksha_walls_key.walls_key_document(
            rows,
            {(7, "left"): ("wall", "")},
            GEOMETRY,
            name="synthetic",
            version="1",
            source="invented",
        )


def test_a_truth_with_no_row_of_the_set_is_refused() -> None:
    with pytest.raises(KeyFormatError, match="no row"):
        pariksha_walls_key.truth_rows(TRUTH, "sZ")
    with pytest.raises(KeyFormatError, match="set>"):
        pariksha_walls_key.truth_rows({"bad": ["island", {"L": 0, "R": 0}]}, "sX")
