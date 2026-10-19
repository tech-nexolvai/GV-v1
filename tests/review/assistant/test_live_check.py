"""The full check's live state for the live screen (#1710): every event and the one-call state.

- Every event is small, versioned (`v: 1`) and matches its contract shape (`ChatLive*Out`); the
  snapshot is a valid `ChatLiveStateOut` at every moment.
- Drawings: the whole work list at once (waiting, or skipped with code's reason, each with its area),
  then each start and finish; the comparing phase counts them.
- Honest findings (owner choice 1, 2026-10-19): nothing counts before the run decides it; only a
  proved Wrong is Wrong; candidates waiting for the second look are only counted; the same item
  counts once; a shown Wrong never disappears when a same-item Wrong stands for it.
- Stop: how many drawings are still finishing and how many never start, updated as they finish.
- The AI service's plain reason, once, when it stops every call.
- The roster: fewer events (pages, its phases), findings decided once at writing; nothing breaks.

Made-up findings and drawings only; no model call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.review.assistant.chat_contract import (
    ChatLiveDrawingOut,
    ChatLiveFindingOut,
    ChatLivePhaseOut,
    ChatLiveStateOut,
    ChatLiveStoppingOut,
    ChatLiveTallyOut,
)
from app.review.assistant.live_check import EVENTS, FILES_EXPECTED, LIVE_VERSION, LiveCheck
from tests.review.assistant.test_proof_drawing import _ai_only, _proved

_SHAPES: dict[str, tuple[str, Any]] = {
    "check_phase": ("phase", ChatLivePhaseOut),
    "check_drawing": ("drawing", ChatLiveDrawingOut),
    "check_finding": ("finding", ChatLiveFindingOut),
    "check_tally": ("tally", ChatLiveTallyOut),
    "check_stopping": ("stopping", ChatLiveStoppingOut),
}


class _Screen:
    """What a screen following the message receives."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event: str, data: dict[str, Any]) -> None:
        self.events.append((event, data))

    def named(self, name: str) -> list[dict[str, Any]]:
        return [data for event, data in self.events if event == name]

    def last(self, name: str) -> dict[str, Any]:
        return self.named(name)[-1]


def _clock() -> Any:
    moments = iter(
        datetime(2026, 10, 19, 9, 0, tzinfo=UTC) + timedelta(seconds=n) for n in range(10_000)
    )
    return lambda: next(moments)


def _live(**kwargs: Any) -> tuple[LiveCheck, _Screen]:
    screen = _Screen()
    live = LiveCheck(screen, clock=_clock(), **kwargs)
    live.start()
    return live, screen


def _items() -> list[Any]:
    return [
        SimpleNamespace(id="p2.v1", page=2, round=1, area="Area A"),
        SimpleNamespace(id="p3.v1", page=3, round=1, area="Area B"),
        SimpleNamespace(id="p4.v1", page=4, round=None, area=None),
    ]


def _valid(screen: _Screen) -> None:
    """Every event is versioned and is its contract shape."""
    for event, data in screen.events:
        assert event in EVENTS
        assert data["v"] == LIVE_VERSION
        if event == "check_drawings":
            for drawing in data["drawings"]:
                ChatLiveDrawingOut.model_validate(drawing)
        elif event == "check_service":
            assert isinstance(data["reason"], str) and data["reason"]
        else:
            name, shape = _SHAPES[event]
            shape.model_validate(data[name])


def _candidate(key: str, finding: dict[str, Any], outcome: str = "candidate") -> Any:
    return SimpleNamespace(key=key, outcome=outcome, finding=finding, reads={})


def test_the_work_list_and_each_drawing_arrive_as_they_change() -> None:
    live, screen = _live()
    first = screen.last("check_phase")["phase"]
    assert first["phase"] == "getting_ready" and first["started_at"] == "2026-10-19T09:00:00Z"
    live.work_list(_items(), {"p4.v1": "outside the product scope (only sinks)"})
    drawings = screen.last("check_drawings")["drawings"]
    assert [(d["id"], d["status"], d["area"]) for d in drawings] == [
        ("p2.v1", "waiting", "Area A"),
        ("p3.v1", "waiting", "Area B"),
        ("p4.v1", "skipped", None),
    ]
    assert drawings[2]["reason"] == "outside the product scope (only sinks)"
    live.compare_started()
    assert screen.last("check_phase")["phase"] | {"phase_started_at": None} == {
        "phase": "comparing",
        "step": None,
        "done": 0,
        "total": 2,
        "unit": "drawings",
        "started_at": "2026-10-19T09:00:00Z",
        "phase_started_at": None,
        "stopping": False,
    }
    live.drawing_started("p2.v1")
    live.drawing_started("p3.v1")
    live.drawing_finished("p2.v1", "answered")
    live.drawing_finished("p3.v1", "failed", "the model call failed: busy")
    changes = [(d["drawing"]["id"], d["drawing"]["status"]) for d in screen.named("check_drawing")]
    assert changes == [
        ("p2.v1", "comparing"),
        ("p3.v1", "comparing"),
        ("p2.v1", "answered"),
        ("p3.v1", "failed"),
    ]
    assert (
        screen.last("check_phase")["phase"]["done"],
        screen.last("check_phase")["phase"]["total"],
    ) == (2, 2)
    # A drawing skipped by code never starts; an unknown one is ignored.
    live.drawing_started("p4.v1")
    live.drawing_finished("p9.v1", "answered")
    assert len(screen.named("check_drawing")) == 4
    snapshot = ChatLiveStateOut.model_validate(live.snapshot())
    assert snapshot.counts.drawings_done == 2 and snapshot.counts.drawings_total == 2
    assert snapshot.drawings[1].reason == "the model call failed: busy"
    _valid(screen)


def test_nothing_counts_before_the_run_decides_it_and_only_proved_wrongs_are_wrong() -> None:
    live, screen = _live()
    live.work_list(_items(), {})
    # Raw `finding` events (code's findings before their code checks): not decided yet.
    live.observe("finding", {"index": 1, "finding": _proved(page=2)})
    assert screen.named("check_finding") == [] and live.snapshot()["tally"]["wrong"] == 0
    live.decide([_proved(page=2), _ai_only(page=3), _proved(id="F3", page=4, status="same")])
    rows = {d["finding"]["key"]: d["finding"] for d in screen.named("check_finding")}
    assert rows["F1"]["outcome"] == "wrong" and rows["F1"]["proved_by"] == "code"
    assert rows["F1"]["should_be"].startswith('37"') and rows["F1"]["area"] == "Area A"
    assert rows["F1"]["impact"]["score"] > 0 and rows["F1"]["product"] == "cabinet"
    # Read by an AI only: Not compared, with the reason and what would settle it.
    assert rows["F2"]["outcome"] == "not_compared"
    assert rows["F2"]["reason"] and rows["F2"]["needs"]
    assert rows["F3"]["outcome"] == "right" and rows["F3"]["impact"] is None
    assert screen.last("check_tally")["tally"] == {
        "wrong": 1,
        "right": 1,
        "not_compared": 1,
        "waiting_second_look": 0,
    }
    # A decided finding that changes is sent again with what it was.
    live.observe("finding", {"index": 2, "finding": _proved(id="F2", page=3)})
    again = screen.last("check_finding")["finding"]
    assert (again["key"], again["outcome"], again["previous"]) == ("F2", "wrong", "not_compared")
    assert screen.last("check_tally")["tally"]["wrong"] == 2
    _valid(screen)


def test_the_same_wrong_twice_counts_once_and_never_disappears() -> None:
    live, screen = _live()
    live.work_list(_items(), {})
    # Code's own pair and the compare call's item: the same page, values (spelt apart).
    live.decide([_proved(id="F1", page=2)])
    twin = _proved(
        id="F7",
        page=2,
        vendor="46 in",
        inches={"architect": "37.00", "vendor": "46.0", "difference": "9"},
    )
    live.decide([_proved(id="F1", page=2), twin])
    rows = {d["finding"]["key"]: d["finding"] for d in screen.named("check_finding")}
    assert rows["F7"]["same_as"] == "F1"
    assert live.snapshot()["tally"]["wrong"] == 1
    # The final list folds F1 into F7: the shown row stays Wrong and now stands for F7.
    live.observe("finding_dropped", {"index": 0, "finding_id": "F1", "reason": "the same item"})
    shown = [r for r in live.snapshot()["findings"] if r["outcome"] == "wrong" and not r["same_as"]]
    assert [(r["key"], r["id"]) for r in shown] == [("F1", "F7")]
    assert live.snapshot()["tally"]["wrong"] == 1 and live.withdrawn == 0
    # The final decision keeps it.
    live.decide([twin], final=True)
    final = [r for r in live.snapshot()["findings"] if r["outcome"] == "wrong"]
    assert [(r["key"], r["id"], r["final"]) for r in final] == [("F1", "F7", True)]
    assert live.withdrawn == 0
    _valid(screen)


def test_candidates_wait_for_the_second_look_then_become_wrong_or_not_compared() -> None:
    live, screen = _live()
    live.work_list(_items(), {})
    live.verifying(2)
    agreed = _proved(page=2, pairing={"advocate": {"pick": "D1", "verdict": "same_thing"}})
    agreed.pop("id")
    refused = _proved(page=3, vendor='12"', inches={"architect": "37", "vendor": "12"})
    refused.pop("id")
    live.verified("p2.v1", [_candidate("p2.v1:p2.D1", agreed)])
    live.verified(
        "p3.v1",
        [
            _candidate("p3.v1:p3.D4", refused),
            _candidate("p3.v1:p3.D5", _ai_only(page=3), "not_compared"),
            _candidate("p3.v1:p3.D6", _proved(page=3), "candidate"),
        ],
        scoped=lambda finding: finding.get("vendor") != '46"' or finding.get("page") != 3,
    )
    phase = screen.last("check_phase")["phase"]
    assert (phase["phase"], phase["step"], phase["done"], phase["total"]) == (
        "double_checking",
        "verifying",
        2,
        2,
    )
    # Another product's candidate is never counted; the Not compared item is decided at once.
    assert screen.last("check_tally")["tally"] == {
        "wrong": 0,
        "right": 0,
        "not_compared": 1,
        "waiting_second_look": 2,
    }
    live.second_looks(2)
    live.second_look(_candidate("p2.v1:p2.D1", agreed, "red"))
    wrong = screen.last("check_finding")["finding"]
    assert (wrong["key"], wrong["outcome"], wrong["proved_by"]) == (
        "p2.v1:p2.D1",
        "wrong",
        "checked_twice",
    )
    assert wrong["id"] is None
    not_agreed = _ai_only(page=3)
    live.second_look(_candidate("p3.v1:p3.D4", not_agreed, "not_compared"))
    assert screen.last("check_tally")["tally"] == {
        "wrong": 1,
        "right": 0,
        "not_compared": 2,
        "waiting_second_look": 0,
    }
    phase = screen.last("check_phase")["phase"]
    assert (phase["step"], phase["done"], phase["total"], phase["unit"]) == (
        "second_looks",
        2,
        2,
        "looks",
    )
    # Recorded: the finding takes over its candidate's row (same key, now with its id).
    recorded = {**agreed, "id": "F9", "simple": {"key": "p2.v1:p2.D1"}}
    live.decide([recorded])
    row = next(r for r in live.snapshot()["findings"] if r["key"] == "p2.v1:p2.D1")
    assert row["id"] == "F9" and row["outcome"] == "wrong"
    assert live.snapshot()["tally"]["wrong"] == 1
    _valid(screen)


def test_a_look_that_agreed_but_is_not_proved_yet_waits_for_its_record() -> None:
    live, screen = _live()
    live.work_list(_items(), {})
    unproved = _ai_only(page=2)
    unproved.pop("id")
    live.verified("p2.v1", [_candidate("p2.v1:p2.D1", unproved)])
    live.second_looks(1)
    live.second_look(_candidate("p2.v1:p2.D1", unproved, "red"))
    # Never shown red on the look's word alone: it waits for its recorded finding.
    assert [d for d in screen.named("check_finding") if d["finding"]["outcome"] == "wrong"] == []
    live.decide([_proved(id="F4", page=2, simple={"key": "p2.v1:p2.D1"})])
    assert live.snapshot()["tally"]["wrong"] == 1
    _valid(screen)


def test_files_count_and_the_final_list_removes_what_was_left_out() -> None:
    live, screen = _live()
    live.work_list(_items(), {})
    live.decide([_proved(page=2), _ai_only(page=3)])
    live.making_files([_proved(page=2)])
    phase = screen.last("check_phase")["phase"]
    assert (phase["phase"], phase["done"], phase["total"], phase["unit"]) == (
        "making_files",
        0,
        FILES_EXPECTED,
        "files",
    )
    left = next(
        d["finding"]
        for d in screen.named("check_finding")
        if d["finding"]["key"] == "F2" and d["finding"]["removed"]
    )
    assert left["outcome"] == "not_compared"
    for n in range(6):
        live.observe("file", {"file": {"id": str(n), "name": f"file {n}.pdf"}})
    phase = screen.last("check_phase")["phase"]
    assert (phase["done"], phase["total"]) == (6, 6)
    live.finish(None)
    assert screen.events[-1][0] == "check_phase"
    assert screen.last("check_phase")["phase"]["phase"] == "done"
    snapshot = ChatLiveStateOut.model_validate(live.snapshot())
    assert snapshot.counts.files_made == 6 and snapshot.tally.wrong == 1
    assert all(row.final for row in snapshot.findings)
    # Nothing is sent after the end.
    count = len(screen.events)
    live.observe("finding", {"finding": _proved(status="same")})
    live.drawing_finished("p2.v1", "failed", "late")
    assert len(screen.events) == count + 1  # the drawing tile only (the screen may still show it)
    _valid(screen)


def test_stop_says_how_many_drawings_still_finish_and_how_many_never_start() -> None:
    live, screen = _live()
    live.work_list(_items(), {"p4.v1": "a pilot run compares only the first 2 drawings"})
    live.drawing_started("p2.v1")
    live.stopping()
    assert screen.last("check_stopping")["stopping"] == {"in_flight": 1, "not_started": 1}
    assert screen.last("check_phase")["phase"]["stopping"] is True
    live.drawing_finished("p3.v1", "failed", "the run was stopped before this drawing was compared")
    # A drawing let go without starting: counted, no event of its own.
    assert len(screen.named("check_stopping")) == 1
    assert live.snapshot()["stopping"] == {"in_flight": 1, "not_started": 0}
    live.drawing_finished("p2.v1", "answered")
    assert screen.last("check_stopping")["stopping"] == {"in_flight": 0, "not_started": 0}
    live.stopping()  # pressed again: nothing new
    assert len(screen.named("check_stopping")) == 2
    live.finish("cancelled")
    done = screen.last("check_phase")["phase"]
    assert (done["phase"], done["step"]) == ("done", "stopped")
    assert ChatLiveStateOut.model_validate(live.snapshot()).stopping is not None
    _valid(screen)


def test_the_ai_services_reason_is_sent_once_when_it_stops_every_call() -> None:
    reason: dict[str, str | None] = {"now": None}
    live, screen = _live(service=lambda: reason["now"])
    live.work_list(_items(), {})
    live.drawing_finished("p2.v1", "failed", "the model call failed: busy")
    assert screen.named("check_service") == []
    reason["now"] = "The AI service refused this server's key, so Claude did not answer."
    live.drawing_finished("p3.v1", "failed", "the model call failed: refused")
    live.finish(None)
    assert [d["reason"] for d in screen.named("check_service")] == [reason["now"]]
    assert live.snapshot()["service"] == reason["now"]
    _valid(screen)


def test_the_roster_sends_fewer_events_and_decides_its_findings_once_at_writing() -> None:
    live, screen = _live(mode="roster")
    progress = {"phase": "reading", "pages_done": 3, "pages_total": 60}
    live.observe("progress", {"index": 0, "progress": progress})
    live.observe("finding", {"index": 1, "finding": _proved()})
    phase = screen.last("check_phase")["phase"]
    assert (phase["phase"], phase["done"], phase["total"], phase["unit"]) == (
        "comparing",
        3,
        60,
        "pages",
    )
    assert screen.named("check_finding") == []
    live.observe("progress", {"index": 0, "progress": {**progress, "phase": "checking"}})
    assert screen.last("check_phase")["phase"]["phase"] == "double_checking"
    live.observe("progress", {"index": 0, "progress": {**progress, "phase": "writing"}})
    assert screen.last("check_phase")["phase"]["phase"] == "making_files"
    assert [d["finding"]["outcome"] for d in screen.named("check_finding")] == ["wrong"]
    snapshot = ChatLiveStateOut.model_validate(live.snapshot())
    assert (
        snapshot.mode == "roster"
        and snapshot.drawings == []
        and snapshot.counts.drawings_total is None
    )
    _valid(screen)


def test_a_screen_that_fails_never_stops_the_check() -> None:
    def gone(_event: str, _data: dict[str, Any]) -> None:
        raise ConnectionError("the screen went away")

    live = LiveCheck(gone)
    live.start()
    live.work_list(_items(), {})
    live.decide([_proved()])
    assert live.snapshot()["tally"]["wrong"] == 1


@pytest.mark.parametrize("event", ["text_delta", "coverage", "usage", "check_tally"])
def test_other_events_are_not_followed(event: str) -> None:
    live, screen = _live()
    before = len(screen.events)
    live.observe(event, {"index": 0, "text": "x"})
    assert len(screen.events) == before
