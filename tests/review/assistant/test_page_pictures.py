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
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

from app.review.assistant.contract import AnswerEvent, AssistantRequest
from app.review.assistant.intent import REFUSAL_TEXT
from app.review.assistant.page_pictures import (
    PageLook,
    PagePicture,
    cached_pictures,
    fitting_dpi,
)
from app.review.assistant.service import (
    AssistantBudget,
    AssistantRuntime,
    _estimate_micros,
    stream_answer,
)
from app.runs.rates import ModelRate, ModelRates
from tests.review.assistant.synthetic import ROW_FAIL, snapshot

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


def _rates(input_per_1k: str = "0.002", output_per_1k: str = "0.01") -> ModelRates:
    return ModelRates(
        source="made-up test prices",
        retrieved=date(2026, 10, 16),
        currency="USD",
        rates={SONNET: ModelRate(Decimal(input_per_1k), Decimal(output_per_1k))},
    )


def _budget(*, spent: int = 0, package: str = "2.00", question: str = "0.25") -> AssistantBudget:
    return AssistantBudget(
        package_cap_micros=int(Decimal(package) * 1_000_000),
        question_cap_micros=int(Decimal(question) * 1_000_000),
        spent_micros=lambda: spent,
        rates=_rates(),
    )


def _run(
    question: str,
    model: FakeModel,
    pictures: FakePictures | None = None,
    budget: AssistantBudget | None = None,
    **body: Any,
) -> list[tuple[str, Any]]:
    return list(
        stream_answer(
            AssistantRequest.model_validate_json(json.dumps({"question": question, **body})),
            load_snapshot=snapshot,
            runtime=AssistantRuntime(model=model, max_history_turns=6),
            ledger=FakeLedger(),
            pictures=pictures,
            budget=budget,
        )
    )


def _images(request: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    content = request["messages"][0]["content"]
    return [block for block in content if "image" in block]


def _texts(request: Mapping[str, Any]) -> str:
    return "\n".join(
        block["text"] for block in request["messages"][0]["content"] if "text" in block
    )


def test_a_question_naming_a_page_sends_its_two_pictures_and_ai_review_rows() -> None:
    model, pictures = FakeModel(), FakePictures()
    events = _run("What does the vendor show on page 4?", model, pictures)

    assert pictures.asked == [4]
    (request,) = model.calls
    images = _images(request)
    assert [image["image"]["format"] for image in images] == ["png", "png"]
    assert all(image["image"]["source"]["bytes"] == PNG for image in images)
    texts = _texts(request)
    assert "architect" in texts.casefold() and "vendor" in texts.casefold()
    assert "air:x" in texts, "the page's AI page review rows go with the pictures"
    answer = events[-1][1]
    assert isinstance(answer, AnswerEvent)
    assert (answer.mode, answer.looked_at_page) == ("llm", 4)


def test_the_page_in_focus_and_the_focused_records_page_are_looked_at() -> None:
    model, pictures = FakeModel(), FakePictures()
    _run("What is drawn here?", model, pictures, focus={"page_number": 7})
    _run("What is drawn here?", model, pictures, focus={"record_id": str(ROW_FAIL)})
    assert pictures.asked == [7, 4]
    assert all(len(_images(request)) == 2 for request in model.calls)


def test_a_question_with_no_page_sends_no_pictures() -> None:
    model, pictures = FakeModel(), FakePictures()
    events = _run("How does the overall check work?", model, pictures)

    assert pictures.asked == []
    (request,) = model.calls
    assert _images(request) == []
    assert events[-1][1].looked_at_page is None


def test_approve_page_3_is_still_refused_with_no_picture_and_no_call() -> None:
    model, pictures = FakeModel(), FakePictures()
    events = _run("approve page 3", model, pictures, budget=_budget())

    answer = events[-1][1]
    assert (answer.mode, answer.text) == ("refused", REFUSAL_TEXT)
    assert answer.looked_at_page is None
    assert model.calls == [] and pictures.asked == []


def test_the_package_budget_stops_the_call() -> None:
    model, pictures = FakeModel(), FakePictures()
    events = _run(
        "What does the vendor show on page 4?",
        model,
        pictures,
        budget=_budget(spent=1_990_000, package="2.00"),
    )

    assert model.calls == []
    answer = events[-1][1]
    assert isinstance(answer, AnswerEvent)
    assert answer.mode != "llm" and answer.looked_at_page is None


def test_a_question_over_its_cap_is_asked_without_the_pictures() -> None:
    # The cap sits between the question's estimate without the pictures and with them.
    rates = _rates()
    probe, pictures = FakeModel(), FakePictures()
    _run("What does the vendor show on page 4?", probe, pictures)
    (with_pictures,) = probe.calls
    look = pictures.for_page(4)
    full = _estimate_micros(with_pictures, look, SONNET, rates)
    records_only = _estimate_micros(with_pictures, None, SONNET, rates)
    assert full is not None and records_only is not None
    picture_cost = full - records_only
    assert picture_cost > 0
    budget = AssistantBudget(
        package_cap_micros=2_000_000,
        question_cap_micros=full - picture_cost // 2,
        spent_micros=lambda: 0,
        rates=rates,
    )
    model = FakeModel()
    events = _run("What does the vendor show on page 4?", model, FakePictures(), budget=budget)

    (request,) = model.calls
    assert _images(request) == []
    assert "air:x" in _texts(request), "the AI page review rows still go"
    assert events[-1][1].looked_at_page is None


def test_with_a_budget_many_questions_are_asked_until_it_is_used() -> None:
    model, pictures = FakeModel(), FakePictures()
    for _ in range(3):
        assert _run("page 4?", model, pictures, budget=_budget())[-1][1].mode == "llm"
    assert len(model.calls) == 3


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
