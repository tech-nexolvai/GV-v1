"""A stored answer always opens, and knows the code that made it (final plan, changes 7 and 11).

- **Validate on write.** Before an answer is stored its blocks are read back through the same model
  that serves them (`ChatMessageOut`, as `message_out` builds it). A field the reader refuses (#1934:
  a finding's `settled.was = "same"`; #1940: a label's `route = "ocr"`) is logged by where it is, and
  the answer is stored anyway, marked with `stored_problems`: a paid answer is never lost.
- **Read tolerantly.** A stored field the reader refuses is left out of what is shown (then its
  parent when it was required, then its block), logged; never a 500 for the whole chat.
- **Commit stamp.** Every stored answer's trust and accuracy blocks carry `code_version`: the
  running code's commit, from `GV_CODE_VERSION`, else `git rev-parse`, else nothing (never fails).

Made-up values only.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.models import AssistantChatMessage
from app.review.assistant import chat_store, code_version
from app.review.assistant.chat_store import message_out, stored_problems

VERSION = "abc12345"


def _answer(blocks: list[dict[str, Any]], mode: str | None = "full_check") -> AssistantChatMessage:
    return AssistantChatMessage(
        chat_id=uuid4(),
        seq=2,
        role="assistant",
        created_by="reviewer-one",
        text="Done.",
        blocks=blocks,
        api_content=[],
        mode=mode,
    )


def _accuracy(level: str = "green") -> dict[str, Any]:
    return {
        "type": "accuracy",
        "accuracy": {
            "score": None,
            "level": level,
            "claims": 0,
            "breakdown": [],
            "measured": "nothing to check",
        },
    }


def _helpers(stopped: str) -> dict[str, Any]:
    """A step of helpers, one of them stopped as `stopped` (the reader takes four words)."""
    return {
        "type": "step",
        "step": {
            "id": "s1",
            "tool": "Agent",
            "label": "Ran 1 agent · Pages 3-4",
            "ok": False,
            "agents": [
                {
                    "id": "h1",
                    "description": "Pages 3-4",
                    "model": "claude-sonnet-5-5",
                    "ok": False,
                    "report": "Not started.",
                    "stopped": stopped,
                }
            ],
        },
    }


# ---- the commit stamp -----------------------------------------------------------------------------


def test_the_code_version_comes_from_the_environment_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(code_version, "_from_git", lambda repo: "ffffffff")

    assert code_version.read_code_version("ABC12345DEF") == "abc12345"
    assert code_version.read_code_version("not a hash") == "ffffffff"
    assert code_version.read_code_version(None) == "ffffffff"


def test_the_code_version_is_none_without_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)

    assert code_version.read_code_version(None) is None


def test_the_code_version_never_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def broken(repo: Path) -> str:
        raise OSError("disk")

    assert code_version.read_code_version(None, repo=tmp_path) is None  # not a checkout
    monkeypatch.setattr(code_version, "_from_git", broken)
    assert code_version.read_code_version(None) is None


def test_this_checkout_has_a_code_version() -> None:
    found = code_version.read_code_version(None)
    assert found is None or (len(found) == 8 and int(found, 16) >= 0)


def test_trust_and_accuracy_blocks_are_stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_store, "code_version", lambda: VERSION)
    blocks = [{"type": "text", "text": "Hi"}, _accuracy(), {"type": "trust", "trust": {"a": 1}}]

    stamped = chat_store._stamped(blocks)

    assert stamped is not None
    assert stamped[0] == {"type": "text", "text": "Hi"}
    assert stamped[1]["accuracy"]["code_version"] == VERSION
    assert stamped[2]["trust"] == {"a": 1, "code_version": VERSION}
    assert "code_version" not in blocks[1]["accuracy"]  # the writer's own blocks are untouched
    monkeypatch.setattr(chat_store, "code_version", lambda: None)
    assert chat_store._stamped(blocks) == blocks


def test_the_stamp_is_served_with_the_accuracy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_store, "code_version", lambda: VERSION)
    message = _answer(chat_store._stamped([_accuracy()]) or [])

    served = message_out(message, [])

    assert served.accuracy is not None and served.accuracy.code_version == VERSION
    assert served.blocks is not None
    assert served.blocks[0].accuracy.code_version == VERSION  # type: ignore[union-attr]


# ---- validate on write ------------------------------------------------------------------------------


def test_a_clean_answer_has_no_stored_problems() -> None:
    assert stored_problems(_answer([_accuracy(), _helpers("error")])) == []


def test_a_field_the_reader_refuses_is_named_on_write_and_the_answer_marked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    message = _answer([_helpers("credit"), _accuracy()])

    assert stored_problems(message) == ["blocks.0.step.step.agents.0.stopped: literal_error"]
    with caplog.at_level(logging.ERROR, logger="app.review.assistant.chat_store"):
        chat_store._check_on_write(message)

    assert "blocks.0.step.step.agents.0.stopped" in caplog.text
    assert "credit" not in caplog.text  # where and what, never the stored value
    blocks = message.blocks or []
    # Stored anyway (nothing dropped), and marked where tests and the logs can see it.
    assert blocks[0]["step"]["agents"][0]["stopped"] == "credit"
    assert blocks[1]["accuracy"]["stored_problems"] == [
        "blocks.0.step.step.agents.0.stopped: literal_error"
    ]


def test_with_no_accuracy_block_the_trust_block_is_marked() -> None:
    message = _answer([_helpers("credit"), {"type": "text", "text": "Done."}], mode="chat")
    chat_store._check_on_write(message)
    assert all("stored_problems" not in str(b) for b in message.blocks or [])  # nowhere to mark

    trust = {"type": "trust", "trust": {"counts": {}}}
    message = _answer([_helpers("credit"), trust], mode="chat")
    chat_store._check_on_write(message)
    assert (message.blocks or [])[1]["trust"]["stored_problems"]


def test_a_broken_check_never_stops_the_answer_being_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(message: AssistantChatMessage) -> list[str]:
        raise RuntimeError("bug")

    monkeypatch.setattr(chat_store, "stored_problems", broken)
    message = _answer([_accuracy()])
    chat_store._check_on_write(message)  # no raise
    assert message.blocks == [_accuracy()]


# ---- read tolerantly --------------------------------------------------------------------------------


def test_a_refused_field_is_left_out_and_the_rest_is_shown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    message = _answer([_helpers("credit"), {"type": "text", "text": "Done."}, _accuracy()])

    with caplog.at_level(logging.ERROR, logger="app.review.assistant.chat_store"):
        served = message_out(message, [])

    assert served.blocks is not None and len(served.blocks) == 3
    agent = served.blocks[0].step.agents[0]  # type: ignore[union-attr,index]
    assert agent.stopped is None and agent.report == "Not started."
    assert "shown without: blocks.0.step.step.agents.0.stopped" in caplog.text
    # The stored row is never changed by reading it.
    assert (message.blocks or [])[0]["step"]["agents"][0]["stopped"] == "credit"


def test_a_refused_required_field_leaves_out_its_parent_then_its_block() -> None:
    message = _answer([{"type": "text", "text": "Done."}, _accuracy(level="purple")])

    served = message_out(message, [])

    assert served.blocks is not None
    assert [block.type for block in served.blocks] == ["text"]
    assert served.accuracy is not None  # scored again from the findings (none): still shown


def test_an_unknown_block_kind_is_left_out() -> None:
    message = _answer([{"type": "hologram", "x": 1}, {"type": "text", "text": "Done."}])

    served = message_out(message, [])

    assert served.blocks is not None and [b.type for b in served.blocks] == ["text"]


def test_the_check_report_names_the_code_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.review.assistant import chat_engine
    from tests.review.assistant.test_full_check_roster import _clean_ink, _Roster, _roster

    _clean_ink(monkeypatch)
    monkeypatch.setattr(chat_engine, "code_version", lambda: VERSION)
    covers: list[dict[str, Any]] = []
    made = chat_engine.make_report_file

    def report(workspace: Any, summary: Any, *args: Any, **kwargs: Any) -> Any:
        covers.append(dict(summary))
        return made(workspace, summary, *args, **kwargs)

    monkeypatch.setattr(chat_engine, "make_report_file", report)

    _roster(tmp_path, _Roster()).run()

    assert covers and covers[0]["Code version"] == VERSION
