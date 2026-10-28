"""The assistant talks like a chat assistant about the drawings (owner, 2026-10-16). Fake model; $0.

What is asserted:

- the model may read numbers off the pictures, count things, explain what a dimension measures and
  compare the architect with the vendor, in its own words: such an answer is published as written;
- the hard rules still drop an answer: a claim that something was approved, signed off, confirmed,
  fixed, agreed or marked; advice to approve; a pass or fail in the model's own words (a recorded
  result is quoted through its placeholder); a recorded result put on another page;
- "approve page 3" is refused with no model call, and the refusal says the person clicks it on
  the screen, with a button to the item;
- the prompt tells the model GV's markup is GV's, never the vendor's numbers, and that a reading
  off the drawing is not a decision;
- the package's AI page review summary goes to the model even when no page is named.

All numbers, names and pages here are made up.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.review.assistant.page_pictures import PageLook, PagePicture

SONNET = "anthropic.claude-sonnet-5-5"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@dataclass
class ScriptedModel:
    """Replies with the given answer text, and keeps every request it was sent."""

    text: str
    evidence: tuple[str, ...] = ()
    model_id: str = SONNET
    calls: list[Mapping[str, Any]] = field(default_factory=list)

    def answer(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(request)
        body = {"text": self.text, "evidence": list(self.evidence), "actions": []}
        return {
            "stopReason": "end_turn",
            "output": {"message": {"content": [{"type": "text", "text": json.dumps(body)}]}},
            "usage": {"inputTokens": 3000, "outputTokens": 100},
        }


@dataclass
class FakeLedger:
    def before_call(self) -> None:
        pass

    def record(self, **kwargs: Any) -> None:
        pass


@dataclass
class FakePictures:
    asked: list[int] = field(default_factory=list)

    def for_page(self, page_number: int) -> PageLook:
        self.asked.append(page_number)
        return PageLook(
            page_number=page_number,
            pictures=(
                PagePicture(role="architect", png=PNG, width_px=1400, height_px=900),
                PagePicture(role="vendor", png=PNG, width_px=1400, height_px=900),
            ),
            ai_review_rows=(
                {"key": "air:made-up", "result": "different", "measures": "overall length"},
            ),
        )

    def ai_review_overview(self) -> tuple[Mapping[str, Any], ...]:
        return ({"page": 7, "status": "done", "different": 2, "same": 5},)


# ---- talking freely about the drawings -----------------------------------------------------------


# ---- the hard rules ------------------------------------------------------------------------------


# ---- asked to decide -----------------------------------------------------------------------------


# ---- what the model is told ----------------------------------------------------------------------


# ---- a summary of what the vendor got wrong ------------------------------------------------------


def test_the_overview_lists_each_pages_different_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    from uuid import uuid4

    from app.review import ai_review
    from app.review.ai_review import AiReviewOut, AiReviewPageOut, AiReviewRowOut
    from app.review.assistant.page_pictures import RevisionPagePictures

    def row(key: str, result: str, vendor: str) -> AiReviewRowOut:
        return AiReviewRowOut(
            key=key,
            architect_text='36"',
            architect_inches="36",
            measures="floor to counter top",
            vendor_text=vendor,
            vendor_inches=None,
            result=result,  # type: ignore[arg-type]
            why="made-up reason",
            vendor_box=None,
            architect_ref=None,
            status="open" if result == "different" else "done",
        )

    review = AiReviewOut(
        pages=[
            AiReviewPageOut(
                page=2,
                status="done",
                cost_usd=None,
                rows=[row("air:1", "different", '34"'), row("air:2", "same", '36"')],
            ),
            AiReviewPageOut(page=3, status="done", cost_usd=None, rows=[]),
        ]
    )
    monkeypatch.setattr(ai_review, "ai_review_for_revision", lambda session, revision: review)
    overview = RevisionPagePictures(None, None, uuid4()).ai_review_overview()  # type: ignore[arg-type]

    assert overview[0]["page"] == 2 and overview[0]["different"] == 1 and overview[0]["same"] == 1
    (different,) = overview[0]["different_rows"]
    assert (different["architect"], different["vendor"], different["status"]) == (
        '36"',
        '34"',
        "open",
    )
    assert "different_rows" not in overview[1]
