"""Third reads for would-be reds (Anant, 2026-10-28, "yes build it"), $0, made-up findings.

When the cheap second read is unsure or disagrees on a would-be red, a stronger model's third read
settles it. On a real upload every second read came back before third reads opened (after the
lead's answer); then the ten third reads went in page order and four would-be reds further on got
none, so their unsure second reads held them back.

- a would-be red's third read starts as soon as its second read comes back, before third reads
  open for the rest, and is not counted against the others' ten;
- the others' third reads wait for the lead's answer and get what is left of the ten;
- the third reads' money is capped inside the fixed price (`ThirdReadBudget`);
- a third read's result is kept only while the finding shows what was read, and kept on the
  finding's next version;
- "agrees" lifts the hold an unsure second read puts on a red; "unsure" or "disagrees" keeps it;
  a red still needs code's own proof (the pairing proved by code, two reading routes per number);
  the third read never makes one.
"""

from __future__ import annotations

import base64
import io
import threading
import types
from collections.abc import Callable
from typing import Any

import pytest
from PIL import Image

from app.review.assistant import chat_engine, proof, reading_routes
from app.review.assistant.chat_engine import THIRD_READ_RED_LIMIT, ChatEngine, could_go_red
from app.review.assistant.chat_read_review import (
    Budget,
    NoRoom,
    ThirdReadBudget,
    content_tokens,
)
from app.review.assistant.read_adjudication import (
    ADJUDICATION_CAP_MICROS,
    ADJUDICATION_LIMIT,
    ADJUDICATION_PROMPT_TOKENS,
    CENTRE_WORDS,
    question,
)
from tests.review.assistant.test_reading_routes import _Check, _Fit, _label, _marked, _wrong

BOX = {"page": 1, "x0": 100, "y0": 80, "x1": 160, "y1": 96}


def _finding(id_: str, status: str = "same", second: str = "unsure", **more: Any) -> dict[str, Any]:
    return {
        "id": id_,
        "item": f"Item {id_}",
        "status": status,
        "architect": '30"',
        "vendor": '31"',
        "arch_box": dict(BOX),
        "vendor_box": dict(BOX, page=2),
        "created_by": "helper:1",
        "checks": {"second_read": second},
        "second_read_values": {"architect": '30"', "vendor": None},
        **more,
    }


def _red(id_: str, second: str = "unsure") -> dict[str, Any]:
    return _finding(id_, "different", second, inches={"vendor": "31", "architect": "30"})


def _engine(monkeypatch: pytest.MonkeyPatch, *, open_: bool = False) -> Any:
    def job(engine: Any, finding: Any, budget: Any, update: Any) -> Callable[[], None]:
        return lambda: None

    monkeypatch.setattr(chat_engine, "third_read_job", job)
    engine = types.SimpleNamespace(
        _lock=threading.Lock(),
        _third_queued=set(),
        _third_reads_open=open_,
        started=[],
    )
    engine._third_read_budget = lambda: None
    engine._third_read_done = lambda key: None
    engine._submit_check = lambda phase, job: engine.started.append(phase)
    return engine


def _queue(engine: Any, *findings: dict[str, Any]) -> list[str]:
    before = {key[0] for key in engine._third_queued}
    for finding in findings:
        ChatEngine._queue_third_read(engine, finding)
    return sorted({key[0] for key in engine._third_queued} - before)


# ---- when a third read starts ----------------------------------------------------------------------


def test_a_would_be_red_gets_its_third_read_before_third_reads_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(monkeypatch)
    assert could_go_red(_red("F1")) and not could_go_red(_finding("F2"))
    assert _queue(engine, _red("F1"), _red("F3", "disagrees"), _finding("F2")) == ["F1", "F3"]
    assert engine.started == ["third_reads", "third_reads"]
    engine._third_reads_open = True  # the lead has answered: the rest may have theirs now
    assert _queue(engine, _finding("F2")) == ["F2"]


def test_only_an_unsure_or_disagreeing_second_read_gets_a_third(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(monkeypatch, open_=True)
    assert _queue(engine, _red("F1", "agrees"), _red("F2", "not_run")) == []
    assert _queue(engine, _red("F3", "unsure"), _red("F4", "disagrees")) == ["F3", "F4"]


def test_would_be_reds_are_not_counted_against_the_others_ten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(monkeypatch, open_=True)
    others = [_finding(f"F{n}") for n in range(1, 21)]
    assert len(_queue(engine, *others)) == ADJUDICATION_LIMIT
    # The ten are spent, but every would-be red still gets its third read.
    reds = [_red(f"F{n}") for n in range(50, 62)]
    assert len(_queue(engine, *reds)) == 12
    assert _queue(engine, _finding("F99")) == []


def test_the_reds_come_first_and_the_others_get_what_is_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(monkeypatch)
    reds = [_red(f"F{n}") for n in range(1, 8)]
    _queue(engine, *reds)  # as their second reads come back
    engine._third_reads_open = True
    others = [_finding(f"F{n}") for n in range(20, 40)]
    assert len(_queue(engine, *others)) == ADJUDICATION_LIMIT - len(reds)


def test_the_last_sweep_takes_the_would_be_reds_first() -> None:
    """`_post_checks` queues in `_reads_first` order: a would-be red late in the page order is
    ahead of every other item."""
    findings = [_finding(f"F{n}", "different", inches=None) for n in range(1, 12)]
    findings.append(_red("F40"))
    assert min(findings, key=chat_engine._reads_first)["id"] == "F40"


def test_one_third_read_per_reading_and_at_most_one_per_second_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(monkeypatch)
    red = _red("F1")
    assert _queue(engine, red, red) == ["F1"]
    assert len(engine._third_queued) == 1
    # The finding's values changed (its second read was made again): it may be read again.
    assert _queue(engine, {**red, "vendor": '32"'}) == []  # same id: already listed
    assert len(engine._third_queued) == 2
    reds = [_red(f"F{n}") for n in range(100, 100 + THIRD_READ_RED_LIMIT)]
    _queue(engine, *reds)
    assert len(engine._third_queued) == THIRD_READ_RED_LIMIT


# ---- the result on the finding ---------------------------------------------------------------------


def _book_engine(finding: dict[str, Any]) -> Any:
    shown: list[dict[str, Any]] = []
    live = {finding["id"]: dict(finding)}

    def replace(updated: dict[str, Any]) -> bool:
        live[updated["id"]] = updated
        return True

    book = types.SimpleNamespace(live=lambda: list(live.values()), replace=replace)
    engine = types.SimpleNamespace(
        finding_lock=threading.RLock(),
        book=book,
        _thirds={},
        _show_finding=shown.append,
    )
    engine.live = live
    return engine


def _adjudicated(outcome: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def change(current: dict[str, Any]) -> dict[str, Any]:
        current["checks"] = {**current["checks"], "adjudication": outcome}
        current["reads"] = [{"role": "third", "model": "sonnet"}]
        return current

    return change


def test_a_third_read_is_kept_only_while_the_finding_shows_what_was_read() -> None:
    red = _red("F1")
    engine = _book_engine(red)
    key = chat_engine._read_key(red)
    engine.live["F1"] = {**red, "vendor": '33"'}  # changed while it was read
    ChatEngine._third_read_done(engine, key)("F1", _adjudicated("agrees_after_adjudication"))
    assert "adjudication" not in engine.live["F1"]["checks"] and engine._thirds == {}
    engine.live["F1"] = dict(red)
    ChatEngine._third_read_done(engine, key)("F1", _adjudicated("agrees_after_adjudication"))
    assert engine.live["F1"]["checks"]["adjudication"] == "agrees_after_adjudication"
    assert engine._thirds["F1"][0] == key and engine._thirds["F1"][1] == (
        "agrees_after_adjudication"
    )


def test_the_next_version_of_the_finding_keeps_its_third_read() -> None:
    red = _red("F1")
    key = chat_engine._read_key(red)
    engine: Any = types.SimpleNamespace(
        _recorded_checks=lambda: None,
        _reads={"F1": (key, "unsure", {"architect": '30"', "vendor": None})},
        _thirds={"F1": (key, "agrees_after_adjudication", [{"role": "third"}])},
        book=types.SimpleNamespace(pdf_paths=dict),
    )
    fresh = {k: v for k, v in red.items() if k not in ("checks", "second_read_values")}
    checked = ChatEngine._checked(engine, fresh)
    assert checked["checks"]["second_read"] == "unsure"
    assert checked["checks"]["adjudication"] == "agrees_after_adjudication"
    assert checked["reads"] == [{"role": "third"}]
    # Changed values: neither read is kept.
    changed = ChatEngine._checked(engine, {**fresh, "vendor": '33"'})
    assert changed["checks"]["second_read"] == "not_run"
    assert changed["checks"]["adjudication"] is None


# ---- the money --------------------------------------------------------------------------------------


class _Engine:
    def __init__(self, room: int) -> None:
        self.room = room

    def _fits(self, estimate: int | None) -> bool:
        return (estimate or 0) <= self.room


def test_the_third_reads_money_is_capped_inside_the_fixed_price() -> None:
    inner = Budget(_Engine(room=10_000_000))
    budget = ThirdReadBudget(inner, cap=100)
    held = budget.hold(60)
    with pytest.raises(NoRoom):
        budget.hold(50)  # 60 running + 50 > 100
    budget.release(held)
    budget.charge(30)  # what the read really cost
    assert budget.hold(70) == 70
    with pytest.raises(NoRoom):
        budget.hold(1)
    assert inner.held == 70  # held in the checks' shared budget too


def test_the_fixed_price_still_stops_a_third_read() -> None:
    budget = ThirdReadBudget(Budget(_Engine(room=10)), cap=ADJUDICATION_CAP_MICROS)
    with pytest.raises(NoRoom):
        budget.hold(11)
    assert budget.running == 0


def _png(width: int, height: int) -> str:
    out = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(out, format="PNG")
    return base64.b64encode(out.getvalue()).decode("ascii")


def test_a_read_is_held_at_what_it_really_sends() -> None:
    source = {"type": "base64", "media_type": "image/png", "data": _png(240, 250)}
    content: list[dict[str, Any]] = [
        {"type": "image", "source": source},
        {"type": "text", "text": "What value is printed?"},
    ]
    tokens = content_tokens(content, "system", ADJUDICATION_PROMPT_TOKENS)
    assert 500 < tokens < 1_000  # 240 x 250 / 750 = 80, plus the text and the overhead
    unknown: list[dict[str, Any]] = [{"type": "image", "source": {"type": "url", "url": "x"}}]
    assert content_tokens(unknown, "", ADJUDICATION_PROMPT_TOKENS) == ADJUDICATION_PROMPT_TOKENS


def test_the_question_says_the_items_value_is_at_the_centre() -> None:
    shown = [("architect", False), ("vendor", False)]
    assert CENTRE_WORDS in question("Sink base", shown, centred=True)
    assert CENTRE_WORDS not in question("Sink base", shown)  # the resolver's read is unchanged


# ---- what a third read can and cannot do to a red --------------------------------------------------


def _proved(adjudication: str | None, second: str = "unsure", **more: Any) -> dict[str, Any]:
    """A Wrong code proved in every way, its pairing and both numbers' two routes included, whose
    second read was `second` and third read `adjudication`."""
    fit = _Fit({"D1": _Check(True, 23.0), "D9": _Check(True, 24.0)})
    finding = _wrong(
        _label("D1", '23"', "23", "vendor"), _label("D9", '24"', "24", "architect"), **more
    )
    finding["checks"] = {**finding["checks"], "second_read": second, "adjudication": adjudication}
    return _marked(finding, fit)


def test_agrees_lifts_the_hold_of_an_unsure_second_read() -> None:
    assert proof.proof_group(_proved(None)) == "likely_problem"
    finding = _proved("agrees_after_adjudication")
    assert reading_routes.held(finding) is None
    assert proof.proof_group(finding) == "clouded"


def test_agrees_lifts_the_hold_of_a_disagreeing_second_read() -> None:
    assert proof.proof_group(_proved("unsure", "disagrees")) != "clouded"
    assert proof.proof_group(_proved("agrees_after_adjudication", "disagrees")) == "clouded"


@pytest.mark.parametrize("outcome", ["unsure", "disagrees"])
def test_unsure_or_disagrees_keeps_the_hold(outcome: str) -> None:
    finding = _proved(outcome)
    assert reading_routes.held(finding) is not None
    assert proof.proof_group(finding) != "clouded"


def test_agrees_never_makes_a_red_without_codes_pairing_proof() -> None:
    finding = _proved("agrees_after_adjudication", paired_by=None, critic=None)
    assert proof.needs_pairing(finding) and not proof.pairing_proved(finding)
    assert proof.proof_group(finding) != "clouded"


def test_agrees_never_makes_a_red_on_a_number_with_one_reading_route() -> None:
    finding = _wrong(_label("D1", '23"', "23", "vendor"), _label("D9", '24"', "24", "architect"))
    finding["checks"] = {
        **finding["checks"],
        "second_read": "unsure",
        "adjudication": "agrees_after_adjudication",
    }
    _marked(finding, None)  # no drawn length: the PDF's text is each number's only route
    assert reading_routes.verdict(finding).kind == "missing"
    assert proof.proof_group(finding) == "likely_problem"


def test_agrees_never_makes_a_red_without_a_code_witness() -> None:
    finding = _proved("agrees_after_adjudication", witnesses=[])
    finding["checks"] = {**finding["checks"], "printed_vendor": None, "printed_architect": None}
    assert not proof.code_proved(finding)
    assert proof.proof_group(finding) != "clouded"
