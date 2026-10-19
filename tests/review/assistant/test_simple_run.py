"""The simple full check's run (`simple_check/run.py`) and its wiring: the options the API hands
over, the work-items block, merging onto GV Review's recorded row, the Not-compared rows that are
not findings, the drawings in the progress line, and the message's `pilot`. Made-up values only;
the run end to end is checked against the fake Anthropic server ($0, local)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from app.review.assistant import not_compared
from app.review.assistant.chat_contract import (
    AssistantModelsOut,
    ChatBlock,
    ChatMessageIn,
    ChatProgressOut,
)
from app.review.assistant.chat_progress import FullCheckProgress, progress_numbers, with_work
from app.review.assistant.simple_check.run import SimpleOptions, _row, _seed_for, work_item_rows
from app.review.assistant.simple_check.schema import CompareOutcome
from app.review.assistant.simple_check.verify import Verified


def _item(item_id: str, page: int, rnd: int = 1) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(id=item_id, page=page, round=rnd)


def test_options_from_the_apis_plain_mapping() -> None:
    options = SimpleOptions.of(
        {
            "compare": {"model": "claude-sonnet-5-5", "effort": "high", "parallel": 4},
            "blind_model": "claude-opus-5-5",
            "blind_effort": "xhigh",
            "pilot": 4,
        }
    )
    assert options.compare["parallel"] == 4
    assert options.blind.model == "claude-opus-5-5" and options.blind.effort == "xhigh"
    assert options.pilot == 4
    assert SimpleOptions.of(None).pilot is None
    assert SimpleOptions.of({"pilot": 0}).pilot is None


def test_every_work_item_ends_answered_failed_or_skipped() -> None:
    items = [_item("p3.v1", 3), _item("p3.v2", 3, 2), _item("p4.v1", 4), _item("p5.v2", 5, 2)]
    outcomes = {
        "p3.v1": CompareOutcome(
            work_item="p3.v1",
            status="ok",
            asked_model="claude-sonnet-5-5",
            effort="high",
            model="claude-sonnet-5-5",
            cost_usd=0.12,
        ),
        "p3.v2": CompareOutcome(
            work_item="p3.v2",
            status="failed",
            reason="the model call failed: 529",
            asked_model="claude-sonnet-5-5",
            effort="high",
        ),
    }
    rows = work_item_rows(items, outcomes, {"p5.v2": "a pilot run compares only the first 3"})
    assert [(r["id"], r["status"]) for r in rows] == [
        ("p3.v1", "answered"),
        ("p3.v2", "failed"),
        ("p4.v1", "failed"),
        ("p5.v2", "skipped"),
    ]
    assert rows[0]["reason"] is None and rows[0]["cost_usd"] == "0.1200"
    assert rows[1]["reason"] == "the model call failed: 529"
    assert rows[2]["reason"] == "not compared (the run ended first)"
    assert all(r["status"] == "answered" or r["reason"] for r in rows)
    block = TypeAdapter(ChatBlock).validate_python({"type": "work_items", "work_items": rows})
    assert block.type == "work_items"


def test_a_verified_item_merges_onto_gv_reviews_recorded_row() -> None:
    seed = {
        "id": "F1",
        "created_by": "gv_review",
        "source": "gv_review",
        "page": 3,
        "vendor": '45 1/4"',
        "architect": "3'-6\"",
        "vendor_box": None,
    }
    fields = {"page": 3, "status": "different", "vendor": '45 1/4"', "architect": "3' - 6\""}
    assert _seed_for(fields, [seed]) is seed
    assert _seed_for({**fields, "page": 4}, [seed]) is None
    assert _seed_for({**fields, "vendor": '44"'}, [seed]) is None
    assert _seed_for({**fields, "status": "same"}, [seed]) is None
    spotted = {**seed, "vendor_box": {"page": 3, "x0": 1, "y0": 1, "x1": 2, "y1": 2}}
    assert _seed_for(fields, [spotted]) is None


def test_rows_that_are_not_findings_reach_the_not_compared_list() -> None:
    label = {"id": "D7", "page": 23, "text": "305 [12]"}
    listed = Verified(
        "p23.v1:p23.D7",
        "pair",
        "listed",
        "architect gives no size",
        "nothing_to_compare",
        vendor=label,
    )
    room = Verified(
        "p36.v1:item2", "missing_on_vendor", "listed", "room context, not millwork", "not_proved"
    )
    rows = not_compared.gap_rows([_row(listed), _row(room, "the answer's item limit was reached")])
    by_reason = {row.reason: row for row in rows}
    assert set(by_reason) == {"nothing_to_compare", "not_proved"}
    assert by_reason["nothing_to_compare"].page == 23
    assert "label D7" in by_reason["nothing_to_compare"].item
    assert "item limit" in by_reason["not_proved"].item


def test_the_progress_line_counts_drawings() -> None:
    numbers = progress_numbers([], None, 60, "checking", 100.0)
    assert "drawings_total" not in numbers
    shown = with_work(numbers, (12, 36), 120.0)
    assert shown["drawings_done"] == 12 and shown["drawings_total"] == 36
    assert shown["eta_seconds"] == 240
    ChatProgressOut.model_validate(shown)
    published: list[dict[str, Any]] = []
    progress = FullCheckProgress(lambda: ([], None, 60), published.append, clock=lambda: 10.0)
    progress.set_work(3, 39)
    assert published[-1]["drawings_done"] == 3 and published[-1]["drawings_total"] == 39


def test_pilot_goes_with_a_full_check_only() -> None:
    base = {"model": "claude-opus-5-5", "effort": "high", "thinking": True}
    assert ChatMessageIn.model_validate({**base, "mode": "full_check", "pilot": 4}).pilot == 4
    with pytest.raises(ValidationError):
        ChatMessageIn.model_validate({**base, "text": "hi", "pilot": 4})
    with pytest.raises(ValidationError):
        ChatMessageIn.model_validate({**base, "mode": "full_check", "pilot": 0})
    with pytest.raises(ValidationError):
        ChatMessageIn.model_validate({**base, "mode": "full_check", "pilot": 201})


def test_the_models_answer_names_the_full_check_mode() -> None:
    out = AssistantModelsOut(
        available=True,
        provider="anthropic",
        models=[],
        message_cap_usd="15.00",
        package_cap_usd="70.00",
        package_spent_usd="0.00",
        full_check_mode="simple",
    )
    assert out.model_dump()["full_check_mode"] == "simple"
    assert (
        AssistantModelsOut(
            available=True,
            provider="anthropic",
            models=[],
            message_cap_usd="1",
            package_cap_usd="1",
            package_spent_usd="0",
        ).full_check_mode
        == "roster"
    )


def test_a_run_may_name_its_own_compare_model_and_effort() -> None:
    from types import SimpleNamespace

    from app.api.assistant_chats import _simple_options

    settings = SimpleNamespace(
        assistant_full_check_mode="simple",
        assistant_simple_compare_model="claude-sonnet-5-5",
        assistant_simple_compare_effort="high",
        assistant_simple_hard_model="claude-opus-5-5",
        assistant_simple_hard_effort="high",
        assistant_simple_parallel=8,
        assistant_simple_max_tokens=64000,
        assistant_simple_retries=3,
        assistant_simple_blind_model="claude-opus-5-5",
        assistant_simple_blind_effort="high",
    )
    base = {"model": "claude-opus-5-5", "effort": "high", "thinking": True, "mode": "full_check"}
    plain = _simple_options(settings, ChatMessageIn.model_validate(base))  # type: ignore[arg-type]
    assert plain is not None and plain["compare"]["model"] == "claude-sonnet-5-5"
    assert plain["compare"]["hard_model"] == "claude-opus-5-5" and plain["pilot"] is None
    body = ChatMessageIn.model_validate(
        {**base, "pilot": 4, "compare_model": "claude-sonnet-5-5", "compare_effort": "medium"}
    )
    picked = _simple_options(settings, body)  # type: ignore[arg-type]
    assert picked is not None and picked["pilot"] == 4
    assert picked["compare"]["model"] == picked["compare"]["hard_model"] == "claude-sonnet-5-5"
    assert picked["compare"]["effort"] == picked["compare"]["hard_effort"] == "medium"
    roster = SimpleNamespace(**{**vars(settings), "assistant_full_check_mode": "roster"})
    assert _simple_options(roster, body) is None  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ChatMessageIn.model_validate(
            {
                "model": "claude-opus-5-5",
                "effort": "high",
                "thinking": True,
                "text": "hi",
                "compare_model": "claude-sonnet-5-5",
            }
        )


def test_a_replay_names_a_saved_run_of_the_chat() -> None:
    base = {"model": "claude-opus-5-5", "effort": "high", "thinking": True, "mode": "full_check"}
    assert ChatMessageIn.model_validate({**base, "replay": "20261019-101500-ab12cd34"}).replay
    for bad in ("../other-chat", "a/b", ""):
        with pytest.raises(ValidationError):
            ChatMessageIn.model_validate({**base, "replay": bad})
    with pytest.raises(ValidationError):
        ChatMessageIn.model_validate(
            {**{k: v for k, v in base.items() if k != "mode"}, "text": "hi", "replay": "run-1"}
        )
    assert SimpleOptions.of({"replay": "run-1"}).replay == "run-1"
