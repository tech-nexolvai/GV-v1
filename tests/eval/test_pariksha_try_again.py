"""Pariksha's scorer with `--with-try-again` (#1309): a held case is judged on its last try.

Verification for `eval/pariksha/score.py`, `eval/pariksha/compare.py` and
`scripts/pariksha_scorecard.py`. Every stored answer is invented; no model call.
"""

from __future__ import annotations

from typing import Any

import pytest

from eval.pariksha.compare import compare_scorecards
from eval.pariksha.keys import KEY_FORMAT, AnswerKey, load_key
from eval.pariksha.score import AGREED_RIGHT, AGREED_WRONG, HELD, SPLIT, score_akshara, score_rekha
from eval.pariksha.stored import QuestionIdentity, StoredQuestion
from tests.eval.test_pariksha_score import (
    OPUS,
    PAGE,
    ROW_PICTURE,
    SONNET,
    _label_key,
    _ok,
    _outcomes,
    _question,
    _row,
    _run,
    _span,
)


def _try(question_id: str, *attempts: Any, family: str = "claude-slot-span") -> StoredQuestion:
    """A try's question: the first question's id plus `:again<n>`, about the same span."""
    first = question_id.split(":again", 1)[0]
    if family == "slot-row-tiebreak":
        packet: dict[str, Any] = {
            "question_id": question_id,
            "page_index": 1,
            "prompt_id": "slot-row-tiebreak-v1",
            "source_page_sha256": PAGE,
            "packet_sha256": "t" * 8,
            "candidate_count": 3,
            "shown_rows": [1, 2],
            "images": {"numbered_vendor_view": {"sha256": "4" * 64, "storage_key": None}},
        }
        return StoredQuestion(
            identity=QuestionIdentity(family=family, page_sha256=PAGE, question_id=question_id),
            prompt_id="slot-row-tiebreak-v1",
            packet=packet,
            slot_box=None,
            attempts=attempts,
        )
    base = _question(first, prompt_id="claude-slot-span-v5")
    return StoredQuestion(
        identity=QuestionIdentity(family=family, page_sha256=PAGE, question_id=question_id),
        prompt_id="claude-slot-span-v5",
        packet={**base.packet, "question_id": question_id, "packet_sha256": "t" * 8},
        slot_box=base.slot_box,
        attempts=attempts,
    )


def _row_key(cases: dict[str, dict[str, Any]]) -> AnswerKey:
    key = load_key(
        {
            "format": KEY_FORMAT,
            "agent": "rekha",
            "name": "synthetic",
            "version": "1",
            "cases": [
                {
                    "family": "slot-row-choice",
                    "page_sha256": PAGE,
                    "question_id": question_id,
                    "picture_sha256": ROW_PICTURE,
                    "answer": answer,
                }
                for question_id, answer in cases.items()
            ],
        }
    )
    assert isinstance(key, AnswerKey)
    return key


def test_a_held_label_is_judged_on_its_last_try_only_with_the_option() -> None:
    key = _label_key(
        {
            "p1:slot0:0": {"texts": ['12"']},
            "p1:slot1:0": {"texts": ['24"']},
            "p1:slot2:0": {"texts": ['12"']},
        }
    )
    run = _run(
        # Held, then settled right on try 2 (try 1 did not help).
        _question("p1:slot0:0", _ok(OPUS, _span('12"')), _ok(SONNET, _span('13"'))),
        _try("p1:slot0:0:again1", _ok(OPUS, _span('12"')), _ok(SONNET, _span('21"'))),
        _try("p1:slot0:0:again2", _ok(OPUS, _span('12"')), _ok(SONNET, _span('12"'))),
        # Held, then settled WRONG on its try: a new wrong answer.
        _question("p1:slot1:0", _ok(OPUS, _span('24"')), _ok(SONNET, _span('25"'))),
        _try("p1:slot1:0:again1", _ok(OPUS, _span('42"')), _ok(SONNET, _span('42"'))),
        # Agreed right in the first round: a try is never looked at.
        _question("p1:slot2:0", _ok(OPUS, _span('12"')), _ok(SONNET, _span('12"'))),
    )

    before = score_akshara(key, run, code_version="abc1234")
    after = score_akshara(key, run, code_version="abc1234", with_try_again=True)

    assert _outcomes(before) == {
        "p1:slot0:0": HELD,
        "p1:slot1:0": HELD,
        "p1:slot2:0": AGREED_RIGHT,
    }
    assert "try_again_tried" not in before.metrics
    assert before.metrics["unkeyed_questions"].numerator == 0, "a try is not a question of its own"
    assert _outcomes(after) == {
        "p1:slot0:0": AGREED_RIGHT,
        "p1:slot1:0": AGREED_WRONG,
        "p1:slot2:0": AGREED_RIGHT,
    }
    assert after.metrics["try_again_tried"].numerator == 2
    assert after.metrics["try_again_helped"].numerator == 1
    assert after.metrics["try_again_new_wrong"].numerator == 1
    assert after.metrics["unkeyed_questions"].numerator == 1  # try 1 of slot 0, never decisive
    assert after.extractor_versions["try_again"]
    case = next(case for case in after.cases if case.case_id.endswith("p1:slot0:0"))
    assert "p1:slot0:0:again2" in case.note
    assert case.cost_micros == 4000, "both rounds' answers are counted"

    comparison = compare_scorecards(before, after)
    assert [change.case_id.split("/", 1)[1] for change in comparison.fixed] == ["p1:slot0:0"]
    assert [change.case_id.split("/", 1)[1] for change in comparison.new_wrong] == ["p1:slot1:0"]
    assert not comparison.accepted and "1 new wrong" in comparison.verdict


def test_a_split_line_is_judged_on_its_tie_break() -> None:
    key = _row_key({"p1:row-choice": {"box": 2}})
    run = _run(
        _question(
            "p1:row-choice",
            _ok(OPUS, _row(1)),
            _ok(SONNET, _row(2)),
            family="slot-row-choice",
            prompt_id="slot-row-choice-v4",
        ),
        _try(
            "p1:row-choice:again1",
            _ok(OPUS, _row(2)),
            _ok(SONNET, _row(2)),
            family="slot-row-tiebreak",
        ),
    )

    assert _outcomes(score_rekha(key, run, code_version="abc1234")) == {"p1:row-choice": SPLIT}
    after = score_rekha(key, run, code_version="abc1234", with_try_again=True)
    assert _outcomes(after) == {"p1:row-choice": AGREED_RIGHT}
    compared = compare_scorecards(score_rekha(key, run, code_version="abc1234"), after)
    assert compared.accepted and compared.verdict.startswith("ACCEPT")


def test_a_tie_break_answer_naming_a_line_it_did_not_show_is_unsure() -> None:
    key = _row_key({"p1:row-choice": {"box": 3}})
    run = _run(
        _question(
            "p1:row-choice",
            _ok(OPUS, _row(1)),
            _ok(SONNET, _row(2)),
            family="slot-row-choice",
            prompt_id="slot-row-choice-v4",
        ),
        _try(
            "p1:row-choice:again1",
            _ok(OPUS, _row(3)),
            _ok(SONNET, _row(3)),
            family="slot-row-tiebreak",
        ),
    )

    after = score_rekha(key, run, code_version="abc1234", with_try_again=True)
    assert _outcomes(after) == {"p1:row-choice": SPLIT}
    assert {reader.answer for reader in after.cases[0].readers} == {"(unsure)"}


def test_the_script_takes_the_option_only_for_the_two_agents() -> None:
    from scripts.pariksha_scorecard import REFUSED, build_parser, main

    args = build_parser().parse_args(
        [
            "score",
            "--agent",
            "akshara",
            "--db",
            "x",
            "--key",
            "k.json",
            "--code-version",
            "abc1234",
            "--out",
            "o.json",
            "--with-try-again",
        ]
    )
    assert args.with_try_again is True
    assert (
        main(
            [
                "score",
                "--agent",
                "vastu",
                "--db",
                "x",
                "--key",
                "k.json",
                "--code-version",
                "abc1234",
                "--out",
                "o.json",
                "--with-try-again",
            ]
        )
        == REFUSED
    )


@pytest.mark.parametrize("name", ["try_again_tried", "try_again_helped", "try_again_new_wrong"])
def test_the_try_counts_have_no_direction_of_their_own(name: str) -> None:
    from eval.pariksha.compare import metric_direction

    assert metric_direction(name) is None


# --- reading a stored run with its tries ----------------------------------------------------------

pytest_plugins = ("tests.app.postgres_fixture",)


@pytest.fixture
def session(postgres_engine: Any) -> Any:
    from alembic import command
    from app.db.session import session_factory
    from tests.app.postgres_fixture import alembic_config

    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def test_a_stored_runs_tries_are_read_only_with_the_option(session: Any) -> None:
    from eval.pariksha.stored import load_stored_run
    from tests.eval.test_pariksha_stored import OPUS as STORED_OPUS
    from tests.eval.test_pariksha_stored import add_answer, row_answer, span_answer, synthetic_run

    version, run = synthetic_run(session, {"p0:slot0:0": ('3/4"', '5/8"')})
    candidate = next(
        question for question in load_stored_run(session, agent="akshara", database="x").questions
    ).packet["candidate_ids"]
    session.rollback()  # the load made its transaction read-only
    add_answer(
        session,
        run.id,
        version.id,
        question_id="p0:slot0:0:again1",
        prompt_id="claude-slot-span-v5",
        model_id=STORED_OPUS,
        raw=span_answer('3/4"'),
        candidate_ids=list(candidate),
    )
    add_answer(
        session,
        run.id,
        version.id,
        question_id="p0:row-choice:again1",
        prompt_id="slot-row-tiebreak-v1",
        model_id=STORED_OPUS,
        raw=row_answer(1),
        candidate_ids=["a", "b"],
    )
    session.commit()

    plain = load_stored_run(session, agent="akshara", database="x")
    session.rollback()
    assert [q.identity.question_id for q in plain.questions] == ["p0:slot0:0"]
    tried = load_stored_run(session, agent="akshara", database="x", with_try_again=True)
    session.rollback()
    assert sorted(q.identity.question_id for q in tried.questions) == [
        "p0:slot0:0",
        "p0:slot0:0:again1",
    ]
    again = next(q for q in tried.questions if q.identity.question_id.endswith(":again1"))
    assert again.slot_box == next(q for q in tried.questions if q is not again).slot_box

    rows = load_stored_run(session, agent="rekha", database="x")
    session.rollback()
    assert [q.identity.question_id for q in rows.questions] == ["p0:row-choice"]
    rows = load_stored_run(session, agent="rekha", database="x", with_try_again=True)
    assert sorted((q.identity.family, q.identity.question_id) for q in rows.questions) == [
        ("slot-row-choice", "p0:row-choice"),
        ("slot-row-tiebreak", "p0:row-choice:again1"),
    ]


def test_a_line_rekhas_gate_stopped_is_judged_on_its_try_too() -> None:
    """Both readers agreed on line 1 and Rekha's gate stopped it (the scorer cannot see the gate),
    so the run tried again: the try's line is what the product went on, right or newly wrong."""
    key = _row_key({"p1:row-choice": {"box": 2}})

    def run(try_box: int) -> Any:
        return _run(
            _question(
                "p1:row-choice",
                _ok(OPUS, _row(1)),
                _ok(SONNET, _row(1)),
                family="slot-row-choice",
                prompt_id="slot-row-choice-v5",
            ),
            _try(
                "p1:row-choice:again2",
                _ok(OPUS, _row(try_box)),
                _ok(SONNET, _row(try_box)),
                family="slot-row-tiebreak",
            ),
        )

    assert _outcomes(score_rekha(key, run(1), code_version="abc1234")) == {
        "p1:row-choice": AGREED_WRONG
    }
    right = score_rekha(key, run(2), code_version="abc1234", with_try_again=True)
    assert _outcomes(right) == {"p1:row-choice": AGREED_RIGHT}
    wrong = score_rekha(key, run(1), code_version="abc1234", with_try_again=True)
    assert _outcomes(wrong) == {"p1:row-choice": AGREED_WRONG}
    assert wrong.metrics["try_again_new_wrong"].numerator == 1
    assert "again2" in wrong.cases[0].note
