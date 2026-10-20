"""The shared reader of a model's JSON answer (#1811): a fenced or prose-led answer is read; a
broken, cut or doubled answer is refused, never guessed from a piece of it."""

from __future__ import annotations

import json
import logging

import pytest

from app.review.assistant.reply_json import excerpt, json_of_reply, log_unreadable

_ANSWER = {"tags": [{"side": "vendor", "item": "Base: door", "tag": "PL-1", "page": 2}]}


@pytest.mark.parametrize(
    "text",
    [
        json.dumps(_ANSWER),
        f"```json\n{json.dumps(_ANSWER, indent=1)}\n```",
        f"```\n{json.dumps(_ANSWER)}\n```",
        f"Here is the table I read.\n\n```json\n{json.dumps(_ANSWER)}\n```\nDone.",
        f"I read both drawings. {json.dumps(_ANSWER)}",
        f"```json\n{json.dumps(_ANSWER)}\n```\nSame again:\n```json\n{json.dumps(_ANSWER)}\n```",
    ],
)
def test_the_answer_is_read_out_of_its_wrapping(text: str) -> None:
    assert json_of_reply(text) == _ANSWER


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "no answer here",
        # Cut short: never a piece from inside it (the inner row alone is an object).
        '```json\n{"tags": [{"side": "vendor", "item": "Base", "tag": "PL-1"}',
        '{"tags": [{"side": "vendor", "item": "Base", "tag": "PL-1"}',
        # Broken: a trailing comma; the inner row is never taken instead.
        '{"tags": [{"side": "vendor", "item": "Base", "tag": "PL-1"}],}',
        # Two different answers: never choose between them.
        '```json\n{"pick": "D1"}\n```\nOn second thought:\n```json\n{"pick": "D2"}\n```',
        'First {"pick": "D1"} then {"pick": "D2"}',
        # A brace in the words before the answer and none after: not one object.
        "The label {A} is printed twice.",
    ],
)
def test_a_broken_cut_or_doubled_reply_is_refused(text: str) -> None:
    with pytest.raises(ValueError):
        json_of_reply(text)


def test_a_bare_list_or_value_is_returned_for_the_caller_to_refuse() -> None:
    # The shape check stays with each caller, as strict as before.
    assert json_of_reply("[1, 2]") == [1, 2]
    assert json_of_reply('"text"') == "text"


def test_an_unreadable_reply_is_logged_with_its_start(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    log_unreadable(logging.getLogger("t"), "simple blind look", "p1.D4", "Sure!\n\n" + "x" * 900)
    line = caplog.records[-1].getMessage()
    assert "simple blind look" in line and "p1.D4" in line and "chars=907" in line
    assert len(excerpt("y" * 900)) == 300 and excerpt("a\n  b") == "a b"
