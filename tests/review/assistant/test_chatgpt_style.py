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

from app.review.assistant.contract import AnswerEvent, AssistantRequest, Draft
from app.review.assistant.guard import GuardRejected, check
from app.review.assistant.intent import REFUSAL_TEXT
from app.review.assistant.model import SYSTEM_PROMPT
from app.review.assistant.page_pictures import PageLook, PagePicture
from app.review.assistant.service import AssistantRuntime, stream_answer
from tests.review.assistant.synthetic import snapshot

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


def _ask(question: str, model: ScriptedModel, **body: Any) -> AnswerEvent:
    events = list(
        stream_answer(
            AssistantRequest.model_validate_json(json.dumps({"question": question, **body})),
            load_snapshot=snapshot,
            runtime=AssistantRuntime(model=model, max_history_turns=6),
            ledger=FakeLedger(),
            pictures=FakePictures(),
        )
    )
    answer = events[-1][1]
    assert isinstance(answer, AnswerEvent)
    return answer


# ---- talking freely about the drawings -----------------------------------------------------------

FREE_ANSWERS = (
    (
        'On page 2 the vendor shows the counter height as 34 1/2" and the architect shows 36", so '
        "they differ by 1 1/2 inches."
    ),
    "The AI review found 2 differences on page 7: the overall length and the sink centre line.",
    "I count three drawers on the left and two doors under the sink.",
    'The 96" dimension measures the whole countertop from the left wall to the right end.',
    "Yes, the architect's elevation is drawn at a different scale from the vendor's plan.",
)


@pytest.mark.parametrize("text", FREE_ANSWERS)
def test_numbers_counts_and_comparisons_in_the_models_words_are_published(text: str) -> None:
    model = ScriptedModel(text)
    answer = _ask("What does the vendor show on page 2?", model)

    assert answer.mode == "llm"
    assert answer.text == text
    assert answer.looked_at_page == 2


def test_a_recorded_result_is_quoted_through_its_placeholder() -> None:
    model = ScriptedModel(
        'The vendor prints 84 1/2" here. The countertop on {C1.page} {C1.outcome}.', ("C1",)
    )
    answer = _ask("What is different on page 4?", model)

    assert answer.mode == "llm"
    assert answer.text.startswith('The vendor prints 84 1/2" here.')
    assert "[[0]]" in answer.text, "the recorded result is cited"


# ---- the hard rules ------------------------------------------------------------------------------

DROPPED = (
    ("I approved page 4.", "action-claim"),
    ("I have marked the countertop on page 4 as checked.", "action-claim"),
    ("I've signed off the package.", "action-claim"),
    ("Done, page 4 is now confirmed.", "action-claim"),
    ("Page 4 has been approved.", "action-claim"),
    ("I agreed with the difference on page 7.", "action-claim"),
    ("I fixed the number on page 2.", "action-claim"),
    ("You can safely approve page 4.", "advice-to-decide"),
    ("It is safe to sign off now.", "advice-to-decide"),
    ("I recommend approving page 7.", "advice-to-decide"),
    ("The countertop on page 4 passes.", "outcome-in-own-words"),
    ("Page 7 failed the overall check.", "outcome-in-own-words"),
    ("That one is a PASS.", "outcome-in-own-words"),
)


@pytest.mark.parametrize(("text", "code"), DROPPED)
def test_the_hard_rules_drop_an_answer(text: str, code: str) -> None:
    with pytest.raises(GuardRejected, match=code):
        check(Draft(text=text), snapshot())


@pytest.mark.parametrize(("text", "code"), DROPPED[:3])
def test_a_dropped_answer_falls_back_to_the_records(text: str, code: str) -> None:
    answer = _ask("What is on page 4?", ScriptedModel(text))
    assert answer.mode == "records_only"
    assert text not in answer.text


def test_a_recorded_result_said_of_another_page_is_dropped() -> None:
    # C1 is the countertop on page 4; its outcome may not be put on page 7.
    with pytest.raises(GuardRejected, match="fact-on-another-page"):
        check(Draft(text="The countertop on page 7 {C1.outcome}."), snapshot())
    check(Draft(text="The countertop on page 4 {C1.outcome}."), snapshot())


def test_markers_braces_and_links_are_still_refused() -> None:
    for text in ("See [[0]] here.", "A {made up} brace.", "See https://example.test/x."):
        with pytest.raises(GuardRejected):
            check(Draft(text=text), snapshot())


# ---- asked to decide -----------------------------------------------------------------------------


def test_approve_page_3_is_refused_and_points_to_the_screen() -> None:
    model = ScriptedModel("I approved page 3.")
    answer = _ask("approve page 3", model)

    assert model.calls == []
    assert (answer.mode, answer.text) == ("refused", REFUSAL_TEXT)
    assert "on the screen" in REFUSAL_TEXT
    assert answer.actions, "the refusal points to the item to click"


# ---- what the model is told ----------------------------------------------------------------------


def test_the_prompt_lets_the_model_read_and_count_and_keeps_the_hard_rules() -> None:
    prompt = SYSTEM_PROMPT.casefold()
    assert "never read a number off a picture" not in prompt
    assert "no digits" not in prompt
    assert "gv's markup" in prompt and "never the vendor's" in prompt
    assert "{c1.outcome}" in prompt, "a recorded result is quoted as recorded"
    for word in ("approve", "sign off", "confirm", "fix", "agree", "mark"):
        assert word in prompt


def test_the_ai_review_summary_goes_to_the_model_without_a_page() -> None:
    model = ScriptedModel("The AI review found 2 differences on page 7.")
    answer = _ask("What did the AI review find?", model)

    assert answer.mode == "llm"
    (request,) = model.calls
    texts = "\n".join(
        block["text"] for block in request["messages"][0]["content"] if "text" in block
    )
    assert "AI PAGE REVIEW summary" in texts and '"different": 2' in texts


# ---- a summary of what the vendor got wrong ------------------------------------------------------


def test_what_the_vendor_got_wrong_is_answered_from_the_ai_review_rows() -> None:
    text = (
        "Here is what the AI review found different from the architect:\n"
        '- Page 7: the overall length, architect 120", vendor 118 1/2".\n'
        '- Page 7: the sink centre, architect 30", vendor 31".'
    )
    model = ScriptedModel(text)
    answer = _ask("Summarise what the vendor got wrong", model)

    assert (answer.mode, answer.text) == ("llm", text)
    (request,) = model.calls
    assert "AI PAGE REVIEW summary" in json.dumps(request["messages"][0]["content"][:6])


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
