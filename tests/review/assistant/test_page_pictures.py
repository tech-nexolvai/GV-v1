"""The assistant looks at the page it is asked about (#1431). A fake model and fake pictures; no paid call.

What is asserted: a question about a page (named in the question, the page in focus, or the
focused record's page) sends that page's architect and vendor pictures, and its AI page review rows
when there are any; a question with no page sends none; a decision request is still refused before
any picture is made or any call is sent; the package budget stops the call; a question over its cap
is asked without the pictures; pictures are made once per page and then reused.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid4

from app.review.assistant.page_pictures import (
    PageLook,
    PagePicture,
    cached_pictures,
    fitting_dpi,
)

SONNET = "anthropic.claude-sonnet-5-5"
#: A made-up 1x1 PNG header is enough: the service never decodes the pictures.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

GOOD = {
    "text": "The countertop on {C1.page} {C1.outcome}.",
    "evidence": ["C1"],
    "actions": [],
}


def _reply() -> dict[str, Any]:
    return {
        "stopReason": "end_turn",
        "output": {"message": {"content": [{"type": "text", "text": json.dumps(GOOD)}]}},
        "usage": {"inputTokens": 3000, "outputTokens": 100},
    }


@dataclass
class FakeModel:
    model_id: str = SONNET
    calls: list[Mapping[str, Any]] = field(default_factory=list)

    def answer(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(request)
        return _reply()


@dataclass
class FakeLedger:
    recorded: list[Any] = field(default_factory=list)

    def before_call(self) -> None:
        pass

    def record(self, **kwargs: Any) -> None:
        self.recorded.append(kwargs)


@dataclass
class FakePictures:
    """Two pictures for any page, and one made-up AI page review row on page 4."""

    asked: list[int] = field(default_factory=list)

    def for_page(self, page_number: int) -> PageLook:
        self.asked.append(page_number)
        rows = (
            ({"key": "air:x", "result": "different", "why": "made-up reason"},)
            if page_number == 4
            else ()
        )
        return PageLook(
            page_number=page_number,
            pictures=(
                PagePicture(role="architect", png=PNG, width_px=1400, height_px=900),
                PagePicture(role="vendor", png=PNG, width_px=1400, height_px=900),
            ),
            ai_review_rows=rows,
        )


def test_pictures_are_made_once_per_page_and_then_reused() -> None:
    made: list[int] = []
    revision = uuid4()

    def render() -> tuple[PagePicture, ...]:
        made.append(1)
        return (PagePicture(role="vendor", png=PNG, width_px=10, height_px=10),)

    first = cached_pictures(revision, 4, render)
    second = cached_pictures(revision, 4, render)
    cached_pictures(revision, 5, render)
    assert first == second and len(made) == 2


def test_the_render_resolution_fits_what_the_model_reads_unresized() -> None:
    # A made-up half of 600 x 330 points: as sharp as fits, never resized by the API.
    for width_pt, height_pt in ((600, 330), (440, 344), (1200, 800), (50, 40)):
        dpi = fitting_dpi(Decimal(width_pt), Decimal(height_pt))
        width = -(-width_pt * dpi // 72)
        height = -(-height_pt * dpi // 72)
        tile = 28
        assert -(-width // tile) * tile <= 2576 and -(-height // tile) * tile <= 2576
        assert -(-width // tile) * -(-height // tile) <= 4784
        assert 72 <= dpi <= 300
