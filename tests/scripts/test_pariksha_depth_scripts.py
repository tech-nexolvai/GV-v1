"""`scripts/pariksha_scorecard.py` for the depth and view-kind agents (#1257).

Invented keys and invented reader answers only (from `tests/eval/test_pariksha_depth.py`): no
database, no model call, no client value.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from eval.pariksha.depth import DEPTH_AGENT, KEY_PROVENANCE, VIEW_AGENT
from tests.eval.test_pariksha_depth import (
    UNSURE,
    answers,
    depth_key,
    entry,
    good_depth_answers,
    value,
    view_key,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import pariksha_kinds
import pariksha_scorecard
import pariksha_walls


def _write(path: Path, document: dict) -> Path:
    path.write_text(json.dumps(document))
    return path


def _score(tmp_path: Path, agent: str, key: dict, given: dict, name: str) -> list[str]:
    return [
        "score",
        "--agent",
        agent,
        "--key",
        str(_write(tmp_path / f"{name}-key.json", key)),
        "--answers",
        str(_write(tmp_path / f"{name}-answers.json", given)),
        "--code-version",
        "abc1234",
        "--out",
        str(tmp_path / f"{name}.json"),
    ]


def test_score_prints_the_key_provenance_and_counts_then_compare_rejects_a_new_wrong(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        pariksha_scorecard.main(
            _score(tmp_path, DEPTH_AGENT, depth_key(), good_depth_answers(), "before")
        )
        == 0
    )
    out = capsys.readouterr().out
    assert KEY_PROVENANCE in out
    assert "claude-double-read: 6" in out
    assert "pair_agreed_wrong: 0/6" in out
    seeded = good_depth_answers(width=(value('18"'), value('18"')))
    assert pariksha_scorecard.main(_score(tmp_path, DEPTH_AGENT, depth_key(), seeded, "after")) == 0
    capsys.readouterr()

    before, after = str(tmp_path / "before.json"), str(tmp_path / "after.json")
    assert pariksha_scorecard.main(["compare", before, after]) == 1
    out = capsys.readouterr().out
    assert "1 new wrong" in out
    assert KEY_PROVENANCE in out
    assert pariksha_scorecard.main(["compare", after, before]) == 0


def test_a_view_card_and_a_depth_card_are_never_compared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        pariksha_scorecard.main(
            _score(tmp_path, DEPTH_AGENT, depth_key(), good_depth_answers(), "depth")
        )
        == 0
    )
    view_answers = answers(
        VIEW_AGENT, entry("p3:view@10,20,300,400", "view", {"kind": "plan"}, UNSURE)
    )
    assert (
        pariksha_scorecard.main(_score(tmp_path, VIEW_AGENT, view_key(), view_answers, "view")) == 0
    )
    capsys.readouterr()
    depth, view = str(tmp_path / "depth.json"), str(tmp_path / "view.json")

    for main in (pariksha_scorecard.main, pariksha_walls.main, pariksha_kinds.main):
        assert main(["compare", depth, view]) == 2
        assert main(["compare", view, depth]) == 2
    # The walls and kinds compares refuse a depth card even on its own.
    assert pariksha_walls.main(["compare", depth, depth]) == 2
    assert pariksha_kinds.main(["compare", view, view]) == 2
    capsys.readouterr()


def test_refusals_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # A view key scored as depth.
    assert (
        pariksha_scorecard.main(
            _score(tmp_path, DEPTH_AGENT, view_key(), good_depth_answers(), "wrong-key")
        )
        == 2
    )
    assert "not a key for akshara-depth" in capsys.readouterr().err
    # Answers for the other agent.
    view_answers = answers(VIEW_AGENT, entry("p3:view@10,20,300,400", "view", UNSURE, UNSURE))
    assert (
        pariksha_scorecard.main(
            _score(tmp_path, DEPTH_AGENT, depth_key(), view_answers, "wrong-answers")
        )
        == 2
    )
    # No answers file for a depth agent; a database for one (reading stored runs waits for part 3).
    no_answers = _score(tmp_path, DEPTH_AGENT, depth_key(), good_depth_answers(), "none")
    at = no_answers.index("--answers")
    del no_answers[at : at + 2]
    assert pariksha_scorecard.main(no_answers) == 2
    assert "--answers" in capsys.readouterr().err
    with_db = _score(tmp_path, DEPTH_AGENT, depth_key(), good_depth_answers(), "db")
    assert pariksha_scorecard.main([*with_db, "--db", "somewhere"]) == 2
    assert "--answers" in capsys.readouterr().err
    # An answers file for an agent that reads stored runs.
    akshara = _score(tmp_path, DEPTH_AGENT, depth_key(), good_depth_answers(), "akshara")
    akshara[akshara.index(DEPTH_AGENT)] = "akshara"
    assert pariksha_scorecard.main([*akshara, "--db", "somewhere"]) == 2
    assert "--answers" in capsys.readouterr().err
    # A scorecard inside a git working tree.
    inside = _score(tmp_path, DEPTH_AGENT, depth_key(), good_depth_answers(), "inside")
    inside[-1] = str(Path(__file__).resolve().parent / "never-written.json")
    assert pariksha_scorecard.main(inside) == 2
    assert not (Path(__file__).resolve().parent / "never-written.json").exists()


def test_an_answers_file_missing_a_reader_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    given = good_depth_answers()
    given["answers"][0]["readers"] = given["answers"][0]["readers"][1:]
    assert pariksha_scorecard.main(_score(tmp_path, DEPTH_AGENT, depth_key(), given, "one")) == 2
    assert "both readers" in capsys.readouterr().err
    assert not (tmp_path / "one.json").exists()
