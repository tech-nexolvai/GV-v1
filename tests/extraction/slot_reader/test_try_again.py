"""The try-again pieces on their own (#1309): keys, gates, records, the allowance, the tie-break.

Verification for `extraction/slot_reader/try_again.py` and the tie-break question in
`extraction/slot_reader/bedrock.py`. Invented values only; no network.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import TRY_AGAIN_GATE_NAMES
from extraction.slot_reader.anthropic import BatchSpendGuard, SpendCapExceeded
from extraction.slot_reader.bedrock import (
    ROW_PROMPT_ID,
    ROW_TIEBREAK_PROMPT,
    ROW_TIEBREAK_PROMPT_ID,
    CropJob,
    job_prompt_id,
    read_row_choice,
    replay_stored_answer,
)
from extraction.slot_reader.stop import ReadingStop
from extraction.slot_reader.try_again import (
    AKSHARA_CHANGES,
    MAX_TRIES,
    REKHA_CHANGES,
    TRY_AGAIN_GATES,
    AllowanceUsed,
    TryAgainGuard,
    TryRecord,
    is_try_key,
    parse_try_again_gates,
    try_again_allowance,
    try_key,
    try_records_in,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
PRICE = SimpleNamespace(input_per_1k_tokens=Decimal("0.01"), output_per_1k_tokens=Decimal("0.05"))


class Rates:
    def rate_for(self, _model: str) -> object:
        return PRICE


# --- keys: at most three tries, never a try of a try ---------------------------------------------


def test_a_try_key_is_the_first_key_and_its_try_number() -> None:
    assert [try_key("p3:slot1:0", n) for n in (1, 2, 3)] == [
        "p3:slot1:0:again1",
        "p3:slot1:0:again2",
        "p3:slot1:0:again3",
    ]
    assert is_try_key("p3:row-choice:again2") and not is_try_key("p3:row-choice")


@pytest.mark.parametrize("attempt", [0, 4, 5, True])
def test_a_fourth_try_cannot_be_built(attempt: Any) -> None:
    with pytest.raises(ValueError, match="1 to 3"):
        try_key("p3:slot1:0", attempt)


def test_a_try_is_never_tried_again() -> None:
    with pytest.raises(ValueError, match="already a try"):
        try_key("p3:slot1:0:again1", 2)


def test_three_tries_per_agent_in_the_settled_order() -> None:
    assert MAX_TRIES == 3
    assert REKHA_CHANGES == ("tie-break-disputed-lines", "other-numbered-lines", "each-line-alone")
    assert AKSHARA_CHANGES == ("closer-look-2x", "closer-look-3x-upright", "tighter-crop")


# --- the one setting -----------------------------------------------------------------------------


def test_the_setting_is_empty_by_default_and_names_only_known_gates() -> None:
    assert parse_try_again_gates("") == frozenset()
    assert parse_try_again_gates(" akshara:seal , rekha:two-readers-agree ") == {
        "akshara:seal",
        "rekha:two-readers-agree",
    }
    with pytest.raises(ValueError, match="unknown gates: rekha:typo"):
        parse_try_again_gates("rekha:typo")


def test_the_settings_list_of_gates_is_the_readers() -> None:
    """`app/config.py` keeps its own copy (it imports nothing of the reader): the same names."""
    assert TRY_AGAIN_GATE_NAMES == TRY_AGAIN_GATES


def test_the_setting_refuses_an_unknown_gate_at_start_up(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    from app.config import Settings

    monkeypatch.setenv("GV_DATABASE_URL", "postgresql+psycopg://gv:gv@localhost:5434/x")
    assert Settings(_env_file=None).try_again_gates == ""  # type: ignore[call-arg]
    monkeypatch.setenv("GV_TRY_AGAIN_GATES", "akshara:seal,rekha:nonsense")
    with pytest.raises(ValidationError, match="unknown gates"):
        Settings(_env_file=None)  # type: ignore[call-arg]


# --- the record ----------------------------------------------------------------------------------


def test_a_try_record_round_trips_through_its_flag() -> None:
    record = TryRecord("akshara", "seal", "readers-differ", "closer-look-3x-upright", "helped")

    assert record.flag == "try-again:akshara:seal:readers-differ:closer-look-3x-upright:helped"
    assert record.attempt == 2 and record.helped
    assert try_records_in(["slot:0", record.flag, "try-again:akshara:seal:x"]) == (record,)


@pytest.mark.parametrize(
    "parts",
    [
        ("nina", "seal", "x", "closer-look-2x", "helped"),
        ("akshara", "seal", "x", "tie-break-disputed-lines", "helped"),
        ("akshara", "seal", "x", "closer-look-2x", "maybe"),
        ("rekha", "Two Readers", "x", "each-line-alone", "helped"),
    ],
)
def test_a_try_record_holds_only_known_codes(parts: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        TryRecord(*parts)


# --- the allowance -------------------------------------------------------------------------------


def test_the_allowance_is_a_tenth_of_the_readings_cap() -> None:
    assert try_again_allowance(Decimal("4.50")) == Decimal("0.450")


def test_a_try_refused_for_the_allowance_leaves_the_readings_guard_as_it_was() -> None:
    shared = BatchSpendGuard(Decimal(10), Rates())
    guard = TryAgainGuard(Decimal("0.05"), Rates(), shared)

    with pytest.raises(AllowanceUsed):
        guard.reserve("reader-one", 2048, 400)  # 0.04096 + 0.02 > 0.05
    # The reading's own guard was never charged: it still takes its whole cap.
    shared.reserve("reader-one", 0, 1)
    assert isinstance(AllowanceUsed("x"), SpendCapExceeded)


def test_a_try_the_readings_cap_refuses_gives_back_its_allowance() -> None:
    shared = BatchSpendGuard(Decimal("0.01"), Rates())
    guard = TryAgainGuard(Decimal(1), Rates(), shared)

    with pytest.raises(AllowanceUsed, match="reading's AI cap"):
        guard.reserve("reader-one", 2048, 400)
    # Nothing is left reserved on the allowance: a smaller try still fits it whole.
    small = guard.reserve("reader-one", 0, 1)
    assert small is not None


def test_a_try_counts_in_both_the_allowance_and_the_readings_guard() -> None:
    shared = BatchSpendGuard(Decimal("0.07"), Rates())
    guard = TryAgainGuard(Decimal(1), Rates(), shared)
    reservation = guard.reserve("reader-one", 2048, 400)
    guard.settle(reservation, {"usage": {"inputTokens": 1000, "outputTokens": 1000}})
    # Settled at 0.01 + 0.05 = 0.06 on the shared guard: another 0.06 would pass its 0.07.
    with pytest.raises(SpendCapExceeded):
        shared.reserve("reader-one", 2048, 400)
    assert guard.exhausted is False


# --- the stop ------------------------------------------------------------------------------------


def test_a_try_stop_says_which_tries_were_not_sent() -> None:
    stop = ReadingStop()
    sent = SimpleNamespace(key="p1:slot0:0:again1", model_id="a", page_index=1)
    refused = SimpleNamespace(key="p1:slot1:0:again1", model_id="a", page_index=1)
    unasked = SimpleNamespace(key="p1:slot2:0:again1", model_id="a", page_index=1)
    stop.refused(refused, "budget")
    stop.not_asked(unasked)
    assert stop.missed(refused) and stop.missed(unasked) and not stop.missed(sent)


# --- the tie-break question ----------------------------------------------------------------------


class Client:
    def __init__(self, answer: dict[str, object]) -> None:
        self.answer = answer
        self.requests: list[dict[str, Any]] = []

    def converse(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "output": {"message": {"content": [{"text": json.dumps(self.answer)}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 1, "outputTokens": 1},
        }


def _ask(answer: dict[str, object], shown: tuple[int, ...] | None) -> tuple[Any, list[Any], Client]:
    client = Client(answer)
    attempts: list[Any] = []
    reply = read_row_choice(
        client,
        model_id="reader-one",
        page_png=PNG,
        page_index=2,
        candidate_count=6,
        max_tokens=50,
        record_attempt=attempts.append,
        shown_rows=shown,
    )
    return reply, attempts, client


def test_the_tie_break_is_its_own_question_with_the_row_questions_answer_shape() -> None:
    reply, attempts, client = _ask({"row": 4, "kind": "row", "also": [], "why": "x"}, (2, 4))

    assert (reply.row, reply.kind) == (4, "row")
    assert attempts[0].prompt_id == ROW_TIEBREAK_PROMPT_ID
    (text,) = [
        part["text"] for part in client.requests[0]["messages"][0]["content"] if "text" in part
    ]
    assert text == ROW_TIEBREAK_PROMPT
    assert "second, closer look" in text and text.endswith(
        '{"row": <number>, "kind": "<kind>", "also": [<numbers>], "why": "<one short sentence>"}'
    )
    job = CropJob(
        "p2:row-choice:again1", "reader-one", 2, PNG, row_question=True, shown_rows=(2, 4)
    )
    assert job_prompt_id(job, None) == ROW_TIEBREAK_PROMPT_ID
    assert job_prompt_id(
        CropJob("p2:row-choice", "reader-one", 2, PNG, row_question=True), None
    ) == (ROW_PROMPT_ID)


def test_a_box_the_tie_break_did_not_show_counts_as_unsure() -> None:
    reply, _attempts, _client = _ask({"row": 3, "kind": "row", "also": [5], "why": "x"}, (2, 4))

    assert (reply.row, reply.kind) == (0, "unsure")


def test_a_second_countertop_the_tie_break_did_not_show_is_dropped() -> None:
    reply, _attempts, _client = _ask({"row": 2, "kind": "row", "also": [4, 5], "why": "x"}, (2, 4))

    assert (reply.row, reply.also) == (2, (4,))


def test_a_stored_tie_break_answer_is_read_by_the_same_code() -> None:
    job = CropJob(
        "p2:row-choice:again1",
        "reader-one",
        2,
        PNG,
        row_question=True,
        candidate_count=6,
        shown_rows=(2, 4),
    )
    replayed = replay_stored_answer(
        job,
        json.dumps({"row": 5, "kind": "row", "also": [], "why": "x"}),
        reused_from="x",
        max_tokens=9,
    )
    assert replayed is not None
    answer, attempt = replayed
    assert (answer.row, answer.kind, attempt.prompt_id) == (0, "unsure", ROW_TIEBREAK_PROMPT_ID)


#: The tie-break question's exact wording, pinned: a change to it (or to the row question it
#: shares its rules with) needs a new prompt id, so a stored answer is never reused for another text.
TIE_BREAK_SHA256 = "528609935a8c4ae700b6fb7e1e790b3a75be896847f9549e22ce375b0541f1ce"


def test_the_tie_break_wording_is_pinned_and_keeps_every_v5_sentence() -> None:
    import hashlib

    from extraction.slot_reader.bedrock import ROW_PROMPT

    assert (
        ROW_PROMPT_ID == "slot-row-choice-v5" and ROW_TIEBREAK_PROMPT_ID == "slot-row-tiebreak-v1"
    )
    assert hashlib.sha256(ROW_TIEBREAK_PROMPT.encode()).hexdigest() == TIE_BREAK_SHA256
    from extraction.slot_reader.bedrock import ROW_TIEBREAK_FRAMING

    # Every sentence of v5 kept word for word (its colour list too); only the framing is added.
    assert ROW_TIEBREAK_PROMPT.replace(ROW_TIEBREAK_FRAMING, "", 1) == ROW_PROMPT
    assert ROW_TIEBREAK_PROMPT.index(ROW_TIEBREAK_FRAMING) == ROW_PROMPT.index("Task:")
    assert "its two outer ticks are the stone top's two ends" in ROW_TIEBREAK_PROMPT
    # v4's sentence, which ended at "under it.", is never asked again.
    assert "fillers under it. A box around" not in ROW_TIEBREAK_PROMPT
