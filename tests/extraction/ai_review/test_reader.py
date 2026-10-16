"""The AI page review's one call per sheet, with a mocked AI (decision 2026-10-16).

Verification for `extraction/ai_review/reader.py`: a good answer is parsed against its strict
shape; an unparseable one is re-asked once and then reported, never guessed; a budget refusal asks
nothing; a stored answer is reused without a call. No paid call: the client is a stub.
"""

from __future__ import annotations

import io
import json
from decimal import Decimal
from fractions import Fraction
from typing import Any

from PIL import Image

from extraction.ai_review.checks import ArchitectListed
from extraction.ai_review.reader import (
    AI_REVIEW_PROMPT,
    Half,
    ListedDimension,
    PageQuestion,
    ai_review_schema,
    ask_page,
)
from extraction.geometry.rows import Box
from extraction.slot_reader.anthropic import SpendCapExceeded
from extraction.slot_reader.claude_output import _conforms


def _png(size: tuple[int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (255, 255, 255)).save(out, format="PNG")
    return out.getvalue()


def _question() -> PageQuestion:
    box = Box(Decimal(0), Decimal(0), Decimal(100), Decimal(100))
    half = Half(_png((200, 150)), 144, (0, 0, 200, 150), box)
    listed = ListedDimension(ArchitectListed("A1", "2'-7\"", Fraction(31)), box)
    return PageQuestion(0, "0" * 64, half, half, (listed,), AI_REVIEW_PROMPT + "A1: ...")


ANSWER = {
    "vendor_dimensions": [
        {"printed": "787 [31]", "from_to": "floor to top", "direction": "vertical", "where": "left"}
    ],
    "rows": [
        {
            "architect_id": "A1",
            "architect_text": "2'-7\"",
            "measures": "floor to counter top",
            "vendor_text": "787 [31]",
            "result": "SAME",
            "why": "same span",
            "vendor_box": [10, 10, 40, 20],
        }
    ],
}


def _reply(text: str, stop: str = "end_turn") -> dict[str, Any]:
    return {
        "stopReason": stop,
        "output": {"message": {"content": [{"text": text}]}},
        "usage": {"inputTokens": 1000, "outputTokens": 500},
    }


class _Client:
    def __init__(self, *replies: dict[str, Any]) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def converse(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        return self.replies.pop(0)


def test_answer_shape_is_strict() -> None:
    assert _conforms(ANSWER, ai_review_schema())
    assert not _conforms({**ANSWER, "extra": 1}, ai_review_schema())


def test_one_call_two_pictures_parsed_strictly() -> None:
    client = _Client(_reply(json.dumps(ANSWER)))
    read = ask_page(
        _question(), client=client, document_version_id="v", spend_guard=None, stored=None
    )
    assert read.reason is None and read.answer == ANSWER
    (request,) = client.requests
    images = [c for c in request["messages"][0]["content"] if "image" in c]
    assert len(images) == 2
    assert request["modelId"] == "anthropic.claude-opus-5-5"
    assert [a.malformed for a in read.attempts] == [False]


def test_unparseable_answer_is_asked_again_once_then_reported() -> None:
    client = _Client(_reply("not json"), _reply('{"rows": 1}'))
    read = ask_page(
        _question(), client=client, document_version_id="v", spend_guard=None, stored=None
    )
    assert read.answer is None and read.reason and "malformed" in read.reason
    assert len(client.requests) == 2 and [a.malformed for a in read.attempts] == [True, True]


def test_budget_refusal_asks_nothing() -> None:
    class Guard:
        def reserve(self, *_args: object) -> None:
            raise SpendCapExceeded("cap")

    client = _Client()
    read = ask_page(
        _question(), client=client, document_version_id="v", spend_guard=Guard(), stored=None
    )
    assert read.answer is None and read.reason == "the package's AI budget was reached"
    assert client.requests == []


def test_stored_answer_is_reused_without_a_call() -> None:
    client = _Client()
    read = ask_page(
        _question(),
        client=client,
        document_version_id="v",
        spend_guard=None,
        stored=lambda _packet: json.dumps(ANSWER),
    )
    assert read.answer == ANSWER and client.requests == []
    assert read.attempts[0].reused_from == "stored"


def test_failed_call_is_reported_not_raised() -> None:
    class Broken:
        def converse(self, **_request: Any) -> dict[str, Any]:
            raise TimeoutError("slow")

    read = ask_page(
        _question(), client=Broken(), document_version_id="v", spend_guard=None, stored=None
    )
    assert read.answer is None and read.reason == "the AI call failed (TimeoutError)"
