"""The depth reader's measuring script (#1265): it refuses to pay without a cap, never writes into
a repository, reuses stored answers instead of paying twice, and writes Pariksha's answers so that a
value code held is a hold. No model, no network; made-up values."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.depth_read as script
from eval.pariksha.depth import load_depth_answers
from extraction.depth.output import ViewKindAnswer
from extraction.depth.prompts import (
    VIEW_KIND_PROMPT,
    VIEW_KIND_PROMPT_ID,
    VIEW_KIND_SCHEMA,
    DepthQuestion,
)
from extraction.depth.read import CLAUDE_PAIR, CT007, CT010, Asked, ReaderSays, ValueReading
from extraction.form_reader.bedrock import AttemptUsage
from tests.extraction.slot_reader.test_bedrock import PNG

OPUS, SONNET = CLAUDE_PAIR
SHA = "b" * 64


def test_a_paid_run_needs_a_cap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out"
    code = script.main(["--pdf", "x.pdf", "--out", str(out), "--allow-paid"])
    assert code == script.REFUSED
    assert "cap" in capsys.readouterr().err


def test_a_cap_without_allow_paid_is_refused(tmp_path: Path) -> None:
    code = script.main(["--pdf", "x.pdf", "--out", str(tmp_path / "o"), "--cap-usd", "4"])
    assert code == script.REFUSED


def test_the_output_never_goes_into_the_repository() -> None:
    code = script.main(["--pdf", "x.pdf", "--out", str(script.REPO / "out-here")])
    assert code == script.REFUSED


def _value(usable: bool, texts: tuple[str, str], *, field: str = CT010) -> ValueReading:
    edge = "cabinet_face" if field == CT007 else None
    readers = tuple(
        ReaderSays(model, "value", text, start_edge=edge)
        for model, text in zip(CLAUDE_PAIR, texts, strict=True)
    )
    return ValueReading(
        field,
        3,
        readers,
        usable,
        None,
        texts[0],
        "made-up reason: code holds it" if not usable else "ok",
        start_edge=edge,
    )


def test_two_ais_agreeing_where_code_held_is_a_hold_in_the_final_answers() -> None:
    ai, final = script.value_entries(_value(False, ('24"', '24"')))
    assert [entry["value"] for entry in ai] == ['24"', '24"']
    assert [entry["held"] for entry in final] == ["made-up reason: code holds it"] * 2


def test_a_usable_value_and_a_disagreement_are_written_as_they_are() -> None:
    _ai, final = script.value_entries(_value(True, ('4"', '4"'), field=CT007))
    assert final[0] == {"model_id": OPUS, "value": '4"', "start_edge": "cabinet_face"}
    ai, final = script.value_entries(_value(False, ('24"', '25"')))
    assert ai == final


def test_answers_files_load_in_the_scorer() -> None:
    _ai, final = script.value_entries(_value(False, ('24"', '24"')))
    document = {
        "format": script.ANSWERS_FORMAT,
        "agent": script.DEPTH_AGENT,
        "run": "made up",
        "prompt_ids": [],
        "answers": [
            {"page_sha256": SHA, "question_id": "p1:plan:CT010:x", "field": CT010, "readers": final}
        ],
    }
    loaded = load_depth_answers(document)
    assert {reading.says for reading in loaded.answers[(SHA, "p1:plan:CT010:x", CT010)]} == {"held"}


def _asked(key: str) -> Asked:
    from extraction.depth.read import PreparedPage, question_packet

    question = DepthQuestion(VIEW_KIND_PROMPT_ID, (PNG,), VIEW_KIND_PROMPT, VIEW_KIND_SCHEMA)
    page = PreparedPage(0, SHA, "doc", None, lambda box: (0, 0, 0, 0), (), None)  # type: ignore[arg-type]
    return Asked(key, OPUS, 0, question, question_packet(page, key, question, "high"))


def test_no_paid_call_is_sent_without_allow_paid(tmp_path: Path) -> None:
    ledger = script.Ledger(tmp_path / "calls.jsonl", None)
    ask = script.make_asker(ledger, allow_paid=False, paid=None, max_tokens=100)
    answers = ask([_asked("v1:kind")])
    (answer,) = answers.values()
    assert "not asked" in answer.reason
    assert ask.not_sent == [1]


def test_a_stored_answer_is_reused_and_costs_nothing(tmp_path: Path) -> None:
    asked = _asked("v1:kind")
    ledger_path = tmp_path / "calls.jsonl"
    first = script.Ledger(ledger_path, None)
    first.record(
        AttemptUsage(
            OPUS,
            VIEW_KIND_PROMPT_ID,
            VIEW_KIND_PROMPT_ID,
            100,
            10,
            5,
            False,
            raw_response_text=json.dumps({"kind": "plan", "why": "made up"}),
            question_packet=asked.packet,
        )
    )
    ledger = script.Ledger(ledger_path, None)
    ask = script.make_asker(ledger, allow_paid=False, paid=None, max_tokens=100)
    answers = ask([asked])
    assert answers[("v1:kind", OPUS)] == ViewKindAnswer(OPUS, "plan", "made up")
    assert ledger.reused == 1 and ledger.calls == 0 and ask.not_sent == [0]
