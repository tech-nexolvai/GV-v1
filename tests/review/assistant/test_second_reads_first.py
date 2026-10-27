"""Second reads for would-be reds first (2026-10-27), $0, made-up findings.

Second reads are capped per message (`SECOND_READ_LIMIT`) and queued as the helpers report. On a
real upload a would-be red arrived late, after the cap was spent on items no red could come from,
so it went red without the read that, on another run of the same drawings, held it back as unsure.

- a would-be red (`could_go_red`): the AI's difference between two numbers, one on each side;
- while findings still arrive, only would-be reds are read; a late one still gets its read; the
  last queue gives what is left to the rest, differences first;
- the cap itself is unchanged.
"""

from __future__ import annotations

import threading
import types
from typing import Any

import pytest

from app.review.assistant import chat_engine
from app.review.assistant.chat_engine import (
    SECOND_READ_LIMIT,
    ChatEngine,
    could_go_red,
)


def _finding(id_: str, status: str = "check", **more: Any) -> dict[str, Any]:
    return {
        "id": id_,
        "item": f"Item {id_}",
        "status": status,
        "architect": '30"',
        "vendor": '31"',
        "arch_picture_id": f"a-{id_}",
        "vendor_picture_id": f"v-{id_}",
        "created_by": "helper:1",
        "checks": {"second_read": "not_run"},
        **more,
    }


def _red(id_: str) -> dict[str, Any]:
    return _finding(id_, "different", inches={"vendor": "31", "architect": "30"})


def _engine(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(chat_engine, "load_module", lambda name: object())
    engine = types.SimpleNamespace(_lock=threading.Lock(), _read_queued=set(), started=[])
    engine._second_read_one = lambda module, finding: None
    engine._submit_check = lambda phase, job: engine.started.append(phase)
    return engine


def _queue(engine: Any, findings: list[dict[str, Any]], *, final: bool = False) -> list[str]:
    before = {key[0] for key in engine._read_queued}
    ChatEngine._queue_second_reads(engine, findings, final=final)
    return sorted({key[0] for key in engine._read_queued} - before)


def test_a_would_be_red_is_a_difference_of_two_numbers_the_ai_reported() -> None:
    assert could_go_red(_red("F1"))
    assert not could_go_red(_finding("F1"))  # a please-check item
    assert not could_go_red({**_red("F1"), "status": "same"})
    assert not could_go_red({**_red("F1"), "inches": {"vendor": "31", "architect": None}})
    assert not could_go_red({**_red("F1"), "created_by": "ledger"})  # code's own, made later


def test_a_late_would_be_red_still_gets_its_read(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine(monkeypatch)
    early = [_finding(f"F{n}") for n in range(1, 41)]  # 40 items no red can come from
    reds = [_red(f"F{n}") for n in range(41, 51)]
    assert _queue(engine, [*early, *reds]) == sorted(f["id"] for f in reds)
    late = _red("F99")  # arrives after them all
    assert _queue(engine, [*early, *reds, late]) == ["F99"]
    # Once every finding is in, the reads left go to the rest; the cap holds.
    rest = _queue(engine, [*early, *reds, late], final=True)
    assert len(engine._read_queued) == SECOND_READ_LIMIT
    assert len(rest) == SECOND_READ_LIMIT - 11


def test_differences_come_before_the_rest_at_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine(monkeypatch)
    rest = [_finding(f"F{n}", "same") for n in range(1, 40)]
    differences = [_finding(f"F{n}", "different") for n in range(40, 45)]
    taken = _queue(engine, [*rest, *differences], final=True)
    assert all(f["id"] in taken for f in differences)


def test_would_be_reds_are_read_first_in_a_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine(monkeypatch)
    batch = [_finding(f"F{n}") for n in range(1, 40)] + [_red("F50"), _red("F51")]
    taken = _queue(engine, batch, final=True)
    assert "F50" in taken and "F51" in taken
    assert len(taken) == SECOND_READ_LIMIT


def test_the_cap_is_unchanged_for_would_be_reds_too(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine(monkeypatch)
    reds = [_red(f"F{n}") for n in range(1, 41)]
    assert len(_queue(engine, reds)) == SECOND_READ_LIMIT
    assert _queue(engine, [_red("F99")], final=True) == []
