"""The simple full check's blind second look (`simple_check/blind.py`): the question never carries
the first answer, the form keeps to the structured-output limits, and every way the look can fail
leaves the item Not compared. A fake engine and made-up values; no model call, no PDF."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import jsonschema
import pytest

from app.review.assistant.simple_check.blind import (
    BLIND_SCHEMA,
    BlindSettings,
    answer_of,
    look,
    look_all,
    question,
)
from app.review.assistant.simple_check.verify import BlindAnswer, Item, Verified, verify_answer
from tests.review.assistant.test_simple_verify import (
    ROUND_2,
    _pack,
    _pair,
    _reader,
    _unlabelled,
)

SECRET_WORDS = "overall counter width"


def _candidate(kind: str = "pair") -> tuple[Verified, Item]:
    pack = _pack()
    if kind == "pair":
        result = {
            "work_item": ROUND_2.id,
            "pairs": [_pair("p2.D1", "p1.D1", measures=SECRET_WORDS)],
            "other_items": [],
            "unaccounted_labels": [],
        }
        found = verify_answer(result, ROUND_2, pack, ink=lambda label: (100, 0.0))
    elif kind == "values":
        read = _reader({(2, '46"'): "match", (1, "3' - 1\""): "match"})
        result = {
            "work_item": ROUND_2.id,
            "pairs": [],
            "other_items": [_unlabelled(what=SECRET_WORDS)],
            "unaccounted_labels": [],
        }
        found = verify_answer(result, ROUND_2, pack, read=read)
    else:
        entry = {
            "kind": "missing_on_vendor",
            "what": f"outlet in the backsplash, {SECRET_WORDS}",
            "vendor": None,
            "architect": "1' - 8\"",
            "vendor_box": None,
            "architect_box": [295, 95, 335, 115],
            "architect_page": 1,
            "verdict": "different",
            "millwork": True,
        }
        result = {"work_item": ROUND_2.id, "pairs": [], "other_items": [entry]}
        found = verify_answer(result, ROUND_2, pack)
    verified = found.items[0]
    assert verified.outcome == "candidate", verified.reason
    return verified, ROUND_2


@dataclass
class _Reply:
    payload: Any
    stop_reason: str = "end_turn"
    usage: dict[str, Any] | None = None

    def text(self) -> str:
        return self.payload if isinstance(self.payload, str) else json.dumps(self.payload)


@dataclass
class _Engine:
    """What `blind.look` uses of the chat engine: the price, the guard, zoom and the call."""

    replies: list[Any]
    fits: bool = True
    calls: list[dict[str, Any]] = field(default_factory=list)
    crops: list[dict[str, Any]] = field(default_factory=list)
    cancel: threading.Event = field(default_factory=threading.Event)
    provider: Any = field(default_factory=lambda: SimpleNamespace(name="anthropic"))
    broken_crops: bool = False

    def __post_init__(self) -> None:
        self.tools = SimpleNamespace(zoom=self._zoom)

    def _price(self, model: Any, prompt: int, out: int) -> int:
        return 50_000

    def _fits(self, estimate: int | None) -> bool:
        return self.fits

    def _zoom(self, raw: Mapping[str, Any], step: Any, by: str) -> Any:
        self.crops.append(dict(raw))
        if self.broken_crops:
            return SimpleNamespace(content=[{"type": "text", "text": "not drawn"}])
        image = {"type": "image", "source": {"type": "gv_picture", "picture_id": "x"}}
        content = [{"type": "text", "text": "Crop 1"}, dict(image)]
        if raw.get("marks") or raw["crops"][0].get("guides"):
            content += [dict(image), {"type": "text", "text": "D1 3' - 6\" (architect, h)"}]
        return SimpleNamespace(content=content)

    def _call(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _agreeing(**changes: Any) -> dict[str, Any]:
    base = {
        "pick": "D1",
        "verdict": "same_thing",
        "vendor_from": "wall_face (left)",
        "vendor_to": "wall_face (right)",
        "architect_from": "wall_face (left)",
        "architect_to": "wall_face (right)",
        "millwork": "unsure",
        "note": "",
    }
    return {**base, **changes}


def _texts(call: Mapping[str, Any]) -> str:
    content = call["messages"][0]["content"]
    return " ".join(block.get("text", "") for block in content if block.get("type") == "text")


# ---- the form ---------------------------------------------------------------------------------


def test_the_form_keeps_to_the_structured_output_limits() -> None:
    jsonschema.Draft202012Validator.check_schema(BLIND_SCHEMA)
    assert BLIND_SCHEMA["additionalProperties"] is False
    assert sorted(BLIND_SCHEMA["required"]) == sorted(BLIND_SCHEMA["properties"])
    jsonschema.validate(_agreeing(), BLIND_SCHEMA)


def test_answer_of_reads_the_form() -> None:
    found = answer_of(_agreeing(pick="none", millwork="no", verdict="Absent"))
    assert found is not None
    assert found.pick is None and found.millwork is False and found.verdict == "absent"
    assert answer_of("not a form") is None


# ---- the question never carries the first answer ----------------------------------------------


@pytest.mark.parametrize("kind", ["pair", "values", "absence"])
def test_the_question_never_carries_the_first_answer(kind: str) -> None:
    verified, item = _candidate(kind)
    engine = _Engine([_Reply(_agreeing())])
    look(engine, verified, item, BlindSettings())  # type: ignore[arg-type]
    words = _texts(engine.calls[0])
    assert SECRET_WORDS not in words
    for name in ("vendor_from", "vendor_to", "architect_from", "architect_to"):
        said = verified.ends.get(name)
        assert not said or said not in words
    if kind == "pair":
        # The vendor label is named; the architect label the compare call picked never is.
        assert "p2.D1" in words and "p1.D1" not in words
        assert engine.crops[0]["crops"][0]["guides"] == ["D1"]
        assert engine.crops[1]["marks"] is True
    assert engine.calls[0]["output_format"] == BLIND_SCHEMA
    assert engine.calls[0]["model"].id == "claude-opus-5-5"
    assert engine.calls[0]["effort"] == "high"
    assert engine.calls[0]["tools"] == ()
    assert engine.calls[0]["prompt_id"].startswith("review-assistant-chat-simple-blind")


def test_each_kind_of_question_is_its_own() -> None:
    assert "pick the numbered architect dimension that measures the same thing" in question(
        "pair", "p2.D1", None
    )
    assert "close view" in question("values", None, None)
    assert "shown when the vendor's drawing shows" in question("absence", None, "p1.D4")


# ---- outcomes -------------------------------------------------------------------------------------


def test_an_agreeing_look_makes_the_candidate_red() -> None:
    verified, item = _candidate()
    engine = _Engine([_Reply(_agreeing())])
    done = look_all(engine, [(verified, item)], BlindSettings())  # type: ignore[arg-type]
    assert done[0].outcome == "red"
    assert done[0].finding["pairing"]["advocate"]["model"] == "claude-opus-5-5"


@pytest.mark.parametrize(
    ("engine_changes", "reply", "why"),
    [
        ({"fits": False}, None, "runaway guard"),
        ({"broken_crops": True}, None, "pictures could not be drawn"),
        ({}, RuntimeError("down"), "the call failed"),
        ({}, _Reply(_agreeing(), stop_reason="refusal"), "declined"),
        ({}, _Reply(_agreeing(), stop_reason="max_tokens"), "cut short"),
        ({}, _Reply("{not json"), "not the form"),
        ({}, _Reply(_agreeing(pick="D3")), "chose p1.D3"),
        ({}, _Reply(_agreeing(verdict="different_points")), "different points"),
    ],
)
def test_every_failed_or_disagreeing_look_leaves_the_item_not_compared(
    engine_changes: dict[str, Any], reply: Any, why: str
) -> None:
    verified, item = _candidate()
    engine = _Engine([reply] if reply is not None else [], **engine_changes)
    done = look_all(engine, [(verified, item)], BlindSettings())  # type: ignore[arg-type]
    assert done[0].outcome == "not_compared"
    assert why in done[0].reason
    assert done[0].finding["status"] == "check"


def test_a_stopped_check_makes_no_call() -> None:
    verified, item = _candidate()
    engine = _Engine([])
    engine.cancel.set()
    answer, why = look(engine, verified, item, BlindSettings())  # type: ignore[arg-type]
    assert answer is None and why == "the check was stopped" and engine.calls == []


def test_the_model_setting_is_used() -> None:
    verified, item = _candidate()
    engine = _Engine([_Reply(_agreeing())])
    look(engine, verified, item, BlindSettings("claude-sonnet-5-5", "medium"))  # type: ignore[arg-type]
    assert engine.calls[0]["model"].id == "claude-sonnet-5-5"
    assert engine.calls[0]["effort"] == "medium"


def test_an_absence_look_agreeing_makes_it_red() -> None:
    verified, item = _candidate("absence")
    reply = _Reply(
        _agreeing(
            pick="none",
            verdict="absent",
            millwork="yes",
            architect_from="centre_line (outlet)",
            architect_to="wall_face (right)",
        )
    )
    engine = _Engine([reply])
    done = look_all(engine, [(verified, item)], BlindSettings())  # type: ignore[arg-type]
    assert done[0].outcome == "red"
    # The architect's drawing with its dimension marked, then the vendor's numbered.
    assert engine.crops[0]["crops"][0]["guides"] == ["D4"] and engine.crops[1]["marks"] is True


def test_the_look_always_thinks_and_saves_its_thinking_tokens() -> None:
    """#1775, C4: the gate of every red is a judgement: adaptive thinking on any model, and the
    thinking tokens saved with the look (a red whose look did not think can be found)."""
    verified, item = _candidate()
    usage = {"output_tokens": 900, "output_tokens_details": {"thinking_tokens": 640}}
    engine = _Engine([_Reply(_agreeing(), usage=usage)])
    answer, _why = look(engine, verified, item, BlindSettings("claude-sonnet-5-5", "medium"))  # type: ignore[arg-type]
    assert engine.calls[0]["thinking"]["type"] == "adaptive"
    assert answer is not None and answer.thinking_tokens == 640
    saved = answer.as_dict()
    assert saved["thinking_tokens"] == 640
    assert BlindAnswer.from_dict(saved).thinking_tokens == 640
    assert BlindAnswer.from_dict({"pick": "D1", "verdict": "same_thing"}).thinking_tokens is None


@dataclass
class _CappedEngine(_Engine):
    """A price cap that fits two looks: each call is charged only when it ends."""

    cap: int = 120_000
    spent: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def _fits(self, estimate: int | None) -> bool:
        return self.spent + (estimate or 0) <= self.cap

    def _call(self, **kwargs: Any) -> Any:
        with self.lock:
            self.calls.append(kwargs)
        time.sleep(0.05)  # the other workers ask while this one runs
        with self.lock:
            self.spent += 50_000
        return _Reply(_agreeing())


def test_looks_started_together_hold_their_price_and_never_pass_the_cap() -> None:
    """Six workers: each look holds its price while it runs (#1775, C4: thinking makes a call's
    worst case bigger), so looks started together cannot pass the cap between them."""
    verified, item = _candidate()
    engine = _CappedEngine([])
    done = look_all(engine, [(verified, item)] * 6, BlindSettings())  # type: ignore[arg-type]
    assert len(engine.calls) == 2 and engine.spent <= engine.cap
    refused = [d for d in done if d.outcome == "not_compared"]
    assert len(refused) == 4 and all("runaway guard" in d.reason for d in refused)


# ---- a reply wrapped in a code block or after a sentence (#1811) ------------------------------


@pytest.mark.parametrize(
    "wrap",
    [
        lambda body: f"```json\n{body}\n```",
        lambda body: f"I compared the two dimensions.\n\n```json\n{body}\n```",
        lambda body: f"Here is my answer: {body}",
    ],
)
def test_a_wrapped_agreeing_reply_is_read(wrap: Any) -> None:
    verified, item = _candidate()
    engine = _Engine([_Reply(wrap(json.dumps(_agreeing())))])
    done = look_all(engine, [(verified, item)], BlindSettings())  # type: ignore[arg-type]
    assert done[0].outcome == "red"


@pytest.mark.parametrize(
    "wrap",
    [
        lambda body: f"```json\n{body}\n```",
        lambda body: f"Looking closer, the ends differ. {body}",
    ],
)
def test_a_wrapped_disagreeing_reply_never_goes_red(wrap: Any) -> None:
    verified, item = _candidate()
    reply = _Reply(wrap(json.dumps(_agreeing(verdict="different_points"))))
    done = look_all(engine := _Engine([reply]), [(verified, item)], BlindSettings())  # type: ignore[arg-type]
    assert engine.calls and done[0].outcome == "not_compared"
    assert "different points" in done[0].reason


def test_two_answers_in_one_reply_are_not_the_form_and_the_reply_is_kept(
    caplog: pytest.LogCaptureFixture,
) -> None:
    verified, item = _candidate()
    text = (
        f"```json\n{json.dumps(_agreeing())}\n```\nWait, correction:\n"
        f"```json\n{json.dumps(_agreeing(verdict='different_points'))}\n```"
    )
    with caplog.at_level("WARNING"):
        done = look_all(_Engine([_Reply(text)]), [(verified, item)], BlindSettings())  # type: ignore[arg-type]
    assert done[0].outcome == "not_compared" and "not the form" in done[0].reason
    advocate = done[0].finding["pairing"]["advocate"]
    assert advocate["failed"] == "the reply was not the form" and advocate["reply"] == text
    assert any("simple blind look" in r.getMessage() for r in caplog.records)
