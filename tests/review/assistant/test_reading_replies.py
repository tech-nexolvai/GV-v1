"""A reading's reply with words around its JSON (2026-10-28): the one asked-for object is read out
of it, two or more are a read of nothing, and the reply's text is kept on the finding.

On a real upload the third reader (Sonnet, through OpenRouter, where the reply's shape was only
asked for in the prompt) wrote a sentence around its JSON on 30 of 48 calls, and every one was
recorded as "read nothing, not sure". The replies below are shaped like those: the reader explains
which picture it read, then gives the object (or gives it first, or in a fence). $0: the model is a
fake."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.review.assistant import chat_contract
from app.review.assistant.read_adjudication import SideCrops, adjudicate, third_read
from app.review.assistant.second_read import (
    REPLY_KEPT_CHARS,
    SecondRead,
    compare_read,
    kept_reply,
    parse_reply,
    second_read,
)

F10 = json.dumps({"architect": '9"', "vendor": "254 [10]", "sure": True})
F56 = json.dumps({"architect": "6' - 0\"", "vendor": '73 1/2"', "sure": True})

WORDY_BEFORE = (
    "Looking at image 1 (the architect's drawing), the value at the centre is the dimension "
    'between the two dashed outlet centre lines, which reads 9". Image 2 is the same crop turned. '
    "In image 3, the vendor's shop drawing, the dimension at the centre is stacked: 254 over [10], "
    "millimetres over inches.\n\n" + F10
)
WORDY_AFTER = (
    F10 + "\n\nThe architect's value is printed between the dashed centre lines; the vendor's "
    "value is stacked with the millimetres above the inches in brackets."
)
WORDY_FENCED = (
    "The turned copies (images 2 and 4) make the sideways text upright.\n\n```json\n"
    + F56
    + "\n```\n\nBoth values are clear in the turned copies."
)
WORDY_BRACES = (
    "The label {6' - 0\"} sits beside the dimension line, see {image 2}.\n" + F56 + "\nDone."
)


@pytest.mark.parametrize(
    ("reply", "architect", "vendor"),
    [
        (WORDY_BEFORE, '9"', "254 [10]"),
        (WORDY_AFTER, '9"', "254 [10]"),
        (WORDY_FENCED, "6' - 0\"", '73 1/2"'),
        (WORDY_BRACES, "6' - 0\"", '73 1/2"'),
        ('Here: {"architect": "37", "vendor": "46", "sure": true}', "37", "46"),
    ],
)
def test_the_one_asked_for_object_is_read_out_of_words(
    reply: str, architect: str, vendor: str
) -> None:
    read = parse_reply(reply)
    assert (read.architect, read.vendor, read.sure) == (architect, vendor, True)


def test_a_wordy_reply_keeps_its_not_sure() -> None:
    reply = "I can only just make it out.\n" + json.dumps(
        {"architect": None, "vendor": '73 1/2"', "sure": False}
    )
    read = parse_reply(reply)
    assert (read.architect, read.vendor, read.sure) == (None, '73 1/2"', False)


@pytest.mark.parametrize(
    "reply",
    [
        # A draft and a final answer that differ: code never picks one.
        "First reading: "
        + json.dumps({"architect": '9"', "vendor": "254", "sure": False})
        + "\nOn a closer look at the stacked value: "
        + F10,
        # The same object twice is still two objects.
        F10 + "\n" + F10,
        # One object per drawing.
        'Architect: {"architect": "9\\"", "sure": true} Vendor: {"vendor": "254 [10]", "sure": true}',
        # A part of the asked-for shape beside it.
        F10 + ' (the vendor value alone: {"vendor": "[10]"})',
    ],
)
def test_two_or_more_candidate_objects_are_a_read_of_nothing(reply: str) -> None:
    read = parse_reply(reply)
    assert (read.architect, read.vendor, read.sure, read.result) == (None, None, False, "unsure")


@pytest.mark.parametrize(
    "reply",
    [
        # Not the asked-for shape, with words around it.
        "Answer: " + json.dumps({"architect": '9"', "vendor": "254 [10]"}),
        "Answer: " + json.dumps({"architect": '9"', "vendor": "254 [10]", "sure": "yes"}),
        "Answer: " + json.dumps({"architect": 9, "vendor": "254 [10]", "sure": True}),
        "Answer: " + json.dumps({"architect": '9"', "vendor": "254", "sure": True, "why": "x"}),
        # The object nested in another one.
        "Result: " + json.dumps({"answer": json.loads(F10)}),
        # A list, all JSON.
        json.dumps([json.loads(F10)]),
        # Broken JSON.
        'The values are {"architect": "9\\"", "vendor": "254 [10]", "sure": true',
        # Words only.
        "I cannot read the value at the centre of either picture.",
    ],
)
def test_anything_that_is_not_the_asked_for_json_stays_nothing(reply: str) -> None:
    read = parse_reply(reply)
    assert (read.architect, read.vendor, read.sure) == (None, None, False)


def test_a_value_read_out_of_words_is_compared_as_strictly_as_ever() -> None:
    finding = {"architect": '9"', "vendor": "254 [10]"}
    sides = ("architect", "vendor")
    assert compare_read(_asked(parse_reply(WORDY_BEFORE), sides), finding).result == "agrees"
    # Only the millimetres read: a different number, never an agreement.
    partial = parse_reply("Read: " + json.dumps({"architect": '9"', "vendor": "254", "sure": True}))
    assert compare_read(_asked(partial, sides), finding).result == "disagrees"
    # Not sure, with words around it: never an agreement.
    unsure = parse_reply(
        "Hmm. " + json.dumps({"architect": '9"', "vendor": "254 [10]", "sure": False})
    )
    assert compare_read(_asked(unsure, sides), finding).result == "unsure"


def _asked(read: SecondRead, sides: tuple[str, ...]) -> SecondRead:
    return SecondRead(read.architect, read.vendor, sure=read.sure, asked=sides)


class FakeModel:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def __call__(self, content: list[dict[str, Any]]) -> str:
        return self.reply


def test_the_second_reading_keeps_its_reply_text() -> None:
    read = second_read(
        {"architect": b"\x89PNG a", "vendor": b"\x89PNG v"}, "Spacing", FakeModel(WORDY_BEFORE)
    )
    assert read.vendor == "254 [10]" and read.reply == WORDY_BEFORE.strip()
    nothing = second_read({"vendor": b"\x89PNG v"}, "Spacing", FakeModel("I cannot read it."))
    assert nothing.vendor is None and nothing.reply == "I cannot read it."
    # The reply is never part of what a reading is (a reading equals another with other words).
    assert read == SecondRead(
        architect='9"', vendor="254 [10]", sure=True, asked=("architect", "vendor")
    )


def test_a_long_reply_is_cut() -> None:
    long = "x" * (REPLY_KEPT_CHARS + 500)
    kept = kept_reply(long)
    assert kept is not None and len(kept) == REPLY_KEPT_CHARS and kept.endswith("…")
    assert kept_reply("  short  ") == "short"
    assert kept_reply("") is None and kept_reply(None) is None


def test_the_third_reading_and_both_replies_are_kept_on_the_finding() -> None:
    finding = {
        "id": "F10",
        "item": "Spacing between the two microwave outlets",
        "architect": '9"',
        "vendor": "254 [10]",
        "status": "different",
        "checks": {"second_read": "unsure"},
        "second_read_values": {"architect": None, "vendor": None, "reply": "Not sure: {}"},
    }
    crops = {
        "architect": SideCrops(upright=b"\x89PNG a"),
        "vendor": SideCrops(upright=b"\x89PNG v"),
    }
    third = third_read(crops, finding["item"], FakeModel(WORDY_AFTER))
    assert third.vendor == "254 [10]" and third.reply == WORDY_AFTER.strip()
    result = adjudicate(
        finding, crops, FakeModel(WORDY_AFTER), model="sonnet", second_model="haiku"
    )
    second, last = result.reads[1], result.reads[2]
    assert second["reply"] == "Not sure: {}" and last["reply"] == WORDY_AFTER.strip()
    assert (last["value_arch"], last["value_vendor"], last["sure"]) == ('9"', "254 [10]", True)
    # The finding's contract keeps both replies (they reach the stored answer).
    for raw in result.reads:
        out = chat_contract.ChatFindingReadOut.model_validate(raw)
        assert out.reply == raw.get("reply")
    values = chat_contract.ChatSecondReadValuesOut.model_validate(finding["second_read_values"])
    assert values.reply == "Not sure: {}"


# ---- the format enforced on the readings' calls ---------------------------------------------------


def test_the_readings_format_is_enforced_on_openrouter_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.review.assistant import chat_request
    from app.review.assistant.second_read import READ_SCHEMA

    monkeypatch.setattr(chat_request, "_openrouter_routing", lambda: {"only": ["x"]})
    common: dict[str, Any] = {
        "model": chat_request.BY_SHORT["haiku"],
        "effort": "low",
        "thinking": {"type": "disabled"},
        "system": "Read the values.",
        "tools": (),
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Go"}]}],
        "final": False,
        "max_tokens": 100,
        "output_format": READ_SCHEMA,
    }
    shape = {"type": "json_schema", "schema": json.loads(json.dumps(READ_SCHEMA))}
    enforced = chat_request.call_params(provider="openrouter", enforce_format=True, **common)
    assert enforced["output_config"] == {"effort": "low", "format": shape}
    # The prompt still says the shape, and the route still honours every parameter it is sent.
    assert enforced["system"][0]["text"].endswith(chat_request.schema_note(READ_SCHEMA))
    assert enforced["extra_body"] == {"provider": {"only": ["x"]}}
    # Every other call with a shape is unchanged (`test_extra_models`: byte for byte).
    plain = chat_request.call_params(provider="openrouter", **common)
    assert plain["output_config"] == {"effort": "low"}
    anthropic = chat_request.call_params(provider="anthropic", enforce_format=True, **common)
    assert anthropic == chat_request.call_params(provider="anthropic", **common)
    # No shape, nothing to enforce.
    bare = {**common, "output_format": None}
    assert chat_request.call_params(provider="openrouter", enforce_format=True, **bare)[
        "output_config"
    ] == {"effort": "low"}


class _Reply:
    stop_reason = "end_turn"

    def __init__(self, text: str) -> None:
        self._text = text
        self.usage: dict[str, int] = {}

    def text(self) -> str:
        return self._text


def test_both_readings_ask_for_the_format_to_be_enforced() -> None:
    import threading
    import types

    from app.review.assistant import chat_read_review
    from app.review.assistant.chat_engine import ChatEngine

    sent: list[dict[str, Any]] = []

    def call(**kwargs: Any) -> _Reply:
        sent.append(kwargs)
        return _Reply(F10)

    engine: Any = types.SimpleNamespace(
        _call=call,
        _price=lambda model, prompt, out: 1,
        _fits=lambda amount: True,
        _hold=lambda share, amount: True,
        _release=lambda share, amount: None,
        _cost=lambda model, usage: 0,
        _lock=threading.Lock(),
        check_prompt_id="review-assistant-chat-check-test",
    )
    second = ChatEngine._second_read_call.__get__(engine)
    assert second([{"type": "text", "text": "Go"}], None) == F10
    third = chat_read_review._model_call(
        engine,
        chat_read_review.Budget(engine),
        short="sonnet",
        effort="low",
        system="S",
        max_tokens=10,
        prompt_tokens=10,
        prompt_id="p",
        output_format={"type": "object"},
        enforce_format=True,
    )
    assert third([{"type": "text", "text": "Go"}]) == F10
    critic = chat_read_review._model_call(
        engine,
        chat_read_review.Budget(engine),
        short="sonnet",
        effort="low",
        system="S",
        max_tokens=10,
        prompt_tokens=10,
        prompt_id="p",
        output_format={"type": "object"},
    )
    critic([{"type": "text", "text": "Go"}])
    assert [call.get("enforce_format", False) for call in sent] == [True, True, False]
    assert sent[0]["output_format"] is not None


def test_the_engine_keeps_the_second_readings_reply_on_the_finding() -> None:
    import threading
    import types

    from app.review.assistant import second_read as module
    from app.review.assistant.chat_engine import ChatEngine

    finding = {
        "id": "F10",
        "item": "Spacing between the two microwave outlets",
        "status": "different",
        "architect": '9"',
        "vendor": "254 [10]",
        "checks": {"second_read": "not_run"},
    }
    stored: dict[str, dict[str, Any]] = {"F10": dict(finding)}

    class Book:
        def live(self) -> list[dict[str, Any]]:
            return [dict(f) for f in stored.values()]

        def replace(self, updated: dict[str, Any]) -> bool:
            stored[updated["id"]] = dict(updated)
            return True

    engine: Any = types.SimpleNamespace(
        _lock=threading.Lock(),
        finding_lock=threading.RLock(),
        book=Book(),
        _reads={},
        _show_finding=lambda f: None,
        _queue_third_read=lambda f: None,
    )
    read = module.second_read(
        {"architect": b"\x89PNG a", "vendor": b"\x89PNG v"},
        finding["item"],
        FakeModel(WORDY_BEFORE),
        centred=True,
    )
    ChatEngine._apply_second_read.__get__(engine)(module, finding, read)
    after = stored["F10"]
    assert after["checks"]["second_read"] == "agrees"
    assert after["second_read_values"] == {
        "architect": '9"',
        "vendor": "254 [10]",
        "reply": WORDY_BEFORE.strip(),
    }
