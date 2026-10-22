"""#1829: the same drawings give the same blind picks, so the same items go red on every run.

- A blind pick that does not fit while other picks run waits for them; it is refused only when
  nothing is running and it still does not fit (the price is really used up). Before, it was
  dropped on the spot, so which items got their pick depended on thread timing.
- A red candidate whose pick was not made says "Not verified within the fixed price", never the
  geometry's reason, so the escalation tries it again.
- One pick per label pair in a run; a decided pick is kept in the set pack and reused at $0 on
  the same drawings, never when what the pick is shown changed.

Every drawing value here is made up.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.review.assistant import full_check_roster, pick_cache
from app.review.assistant.full_check_advocate import NOT_VERIFIED, STRONG_PICK, Check, decide

_ESTIMATE = 100

_VENDOR = {"id": "D4", "page": 2, "file": "shop.pdf", "text": '41 1/8"', "box": [10, 10, 30, 20]}
_ARCHITECT = {
    "id": "D7",
    "page": 1,
    "file": "shop.pdf",
    "text": "7' - 3\"",
    "box": [50, 10, 80, 20],
}
_ARCH_PAGE = [
    _ARCHITECT,
    {"id": "D8", "page": 1, "file": "shop.pdf", "text": '12"', "box": [90, 10, 99, 20]},
]
_ANSWER = {
    "architect_pick": "D7",
    "vendor_spans": {"from": "floor", "to": "top_of_counter"},
    "architect_spans": {"from": "floor", "to": "top_of_counter"},
    "vendor_ink_black": "yes",
}


def _engine(pack: Path | None, labels: list[dict[str, Any]] | None = None) -> SimpleNamespace:
    page = list(_ARCH_PAGE if labels is None else labels)
    return SimpleNamespace(
        _price=lambda model, prompt, out: _ESTIMATE,
        cancel=threading.Event(),
        book=SimpleNamespace(
            dim_labels=SimpleNamespace(on_page=lambda file, page_no, read_now=False: page)
        ),
        tools=SimpleNamespace(workspace=SimpleNamespace(set_pack=pack)),
    )


def _asked(monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any] | None = None) -> list[str]:
    asked: list[str] = []

    def held(self: Any, finding: Any, vendor: Any, architect: Any, page_map: Any, model: Any,
             effort: str = "medium", kind: str = "advocate") -> dict[str, Any]:  # fmt: skip
        asked.append(str(vendor["id"]))
        reply = dict(answer or _ANSWER)
        reply["pick"] = f"p{architect['page']}.{reply.get('architect_pick')}"
        return reply

    monkeypatch.setattr(full_check_roster._Run, "_held_pick", held)
    return asked


# ---- the wait: a pick is never dropped because another one is running --------------------------


def test_a_pick_that_does_not_fit_waits_for_the_running_pick_and_is_then_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Room for one pick's worst case at a time: the second waits, then runs (before #1829 it
    got `None`, and its item stayed grey on that run only)."""
    started, release = threading.Event(), threading.Event()
    running: list[int] = []
    most: list[int] = [0]

    def held(self: Any, finding: Any, vendor: Any, architect: Any, page_map: Any, model: Any,
             effort: str = "medium", kind: str = "advocate") -> dict[str, Any]:  # fmt: skip
        running.append(1)
        most[0] = max(most[0], len(running))
        started.set()
        release.wait(5)
        running.pop()
        return {**_ANSWER, "pick": "p1.D7"}

    monkeypatch.setattr(full_check_roster._Run, "_held_pick", held)
    # The phase fits one pick's worst case, never two at once.
    monkeypatch.setattr(
        full_check_roster._Run, "_phase_room", lambda self, phase, money: money <= _ESTIMATE
    )
    run = full_check_roster._Run(engine=_engine(None))  # type: ignore[arg-type]
    second_architect = {**_ARCHITECT, "id": "D8", "text": '12"'}
    answers: dict[str, Any] = {}
    first = threading.Thread(
        target=lambda: answers.update(a=run._blind_pick({}, _VENDOR, _ARCHITECT, None))
    )
    first.start()
    assert started.wait(5)
    second = threading.Thread(
        target=lambda: answers.update(b=run._blind_pick({}, _VENDOR, second_architect, None))
    )
    second.start()
    time.sleep(0.2)
    assert "b" not in answers  # waiting, not dropped
    release.set()
    first.join(5)
    second.join(5)
    assert answers["a"] is not None and answers["b"] is not None
    assert most[0] == 1 and run.held == 0


def test_a_pick_is_refused_only_when_nothing_running_could_free_money(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = _asked(monkeypatch)
    monkeypatch.setattr(full_check_roster._Run, "_phase_room", lambda self, phase, money: False)
    run = full_check_roster._Run(engine=_engine(None))  # type: ignore[arg-type]
    assert run._blind_pick({}, _VENDOR, _ARCHITECT, None, strong=True) is None
    assert asked == [] and run.held == 0


def test_a_red_candidate_without_its_pick_is_not_verified_never_the_geometrys_reason() -> None:
    """Geometry cannot tell and the pick was not made: the escalation must try it again."""
    verdict = decide(
        mode="ai_pair_plus_code",
        checks=[Check(name, "pass") for name in ("ink", "annotation", "roles", "axis", "round")],
        printed=True,
        helper_pick="p1.D7",
        advocate=None,
        extent=("cannot_tell", "a dimension line's ends were not found"),
        code_paired=False,
    )
    assert verdict.result == "objects" and verdict.reason.startswith(NOT_VERIFIED)
    assert verdict.answers["extent"] == "cannot_tell"
    finding = {"critic": verdict.critic()}
    assert not full_check_roster._judged(finding)  # tried again, never kept


# ---- one pick per label pair, kept for the same drawings ----------------------------------------


def test_two_findings_on_the_same_labels_share_one_pick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _asked(monkeypatch)
    monkeypatch.setattr(full_check_roster._Run, "_phase_room", lambda self, phase, money: True)
    run = full_check_roster._Run(engine=_engine(tmp_path))  # type: ignore[arg-type]
    first = run._blind_pick({"id": "F1"}, _VENDOR, _ARCHITECT, None, strong=True)
    again = run._blind_pick({"id": "F2"}, _VENDOR, _ARCHITECT, None, strong=True)
    assert asked == ["D4"]
    assert first is not None and again is not None
    assert again["pick"] == first["pick"] == "p1.D7"
    assert again[STRONG_PICK] is True and again[pick_cache.CACHED] is True
    assert pick_cache.CACHED not in first


def test_a_decided_pick_is_reused_on_the_same_drawings_in_another_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _asked(monkeypatch)
    monkeypatch.setattr(full_check_roster._Run, "_phase_room", lambda self, phase, money: True)
    first = full_check_roster._Run(engine=_engine(tmp_path))  # type: ignore[arg-type]
    first._blind_pick({}, _VENDOR, _ARCHITECT, None, strong=True)
    assert (tmp_path / pick_cache.PICKS_FILE).is_file()

    later = full_check_roster._Run(engine=_engine(tmp_path))  # type: ignore[arg-type]
    reused = later._blind_pick({}, _VENDOR, _ARCHITECT, None, strong=True)
    assert asked == ["D4"]  # the second run asked nothing: $0
    assert reused is not None and reused["pick"] == "p1.D7" and reused[pick_cache.CACHED]
    # The code mark is set on every reuse, never stored.
    weak = later._blind_pick({}, _VENDOR, _ARCHITECT, None, strong=False)
    assert weak is not None and weak[STRONG_PICK] is False
    assert asked == ["D4", "D4"]  # a weaker pick is another pick: asked


def test_claims_on_one_vendor_label_get_one_blind_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One vendor label claimed against two architect labels in the same crop: the pick is blind
    to the claim, so it is one question, asked once; only the label it names can pass."""
    asked = _asked(monkeypatch)
    monkeypatch.setattr(full_check_roster._Run, "_phase_room", lambda self, phase, money: True)
    monkeypatch.setattr(
        full_check_roster._Run, "_half_box", lambda self, page_map, label: [0.0, 0.0, 400.0, 300.0]
    )
    run = full_check_roster._Run(engine=_engine(tmp_path))  # type: ignore[arg-type]
    other = {**_ARCHITECT, "id": "D8", "text": '12"', "box": [90, 10, 99, 20]}
    first = run._blind_pick({"id": "F1"}, _VENDOR, _ARCHITECT, None, strong=True)
    second = run._blind_pick({"id": "F2"}, _VENDOR, other, None, strong=True)
    assert asked == ["D4"]
    assert first is not None and second is not None
    assert first["pick"] == second["pick"] == "p1.D7"  # D8's claim is not the label it names


@pytest.mark.parametrize(
    "changed",
    [
        "vendor value",  # the vendor label prints another value
        "architect page",  # the architect's page has another numbered label
        "other pack",  # other drawing files: another pack folder
    ],
)
def test_anything_the_pick_is_shown_changed_asks_again(
    changed: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _asked(monkeypatch)
    monkeypatch.setattr(full_check_roster._Run, "_phase_room", lambda self, phase, money: True)
    full_check_roster._Run(engine=_engine(tmp_path))._blind_pick(  # type: ignore[arg-type]
        {}, _VENDOR, _ARCHITECT, None, strong=True
    )
    vendor, labels, pack = dict(_VENDOR), list(_ARCH_PAGE), tmp_path
    if changed == "vendor value":
        vendor["text"] = '41 3/8"'
    elif changed == "architect page":
        labels.append(
            {"id": "D9", "page": 1, "file": "shop.pdf", "text": '3"', "box": [1, 1, 2, 2]}
        )
    else:
        pack = tmp_path / "other"
        pack.mkdir()
    run = full_check_roster._Run(engine=_engine(pack, labels))  # type: ignore[arg-type]
    answer = run._blind_pick({}, vendor, _ARCHITECT, None, strong=True)
    assert asked == ["D4", "D4"]
    assert answer is not None and pick_cache.CACHED not in answer


def test_a_reply_that_was_not_readable_is_never_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def held(self: Any, *given: Any, **named: Any) -> dict[str, Any]:
        return {"pick": "none", "note": "the reply was not readable"}

    monkeypatch.setattr(full_check_roster._Run, "_held_pick", held)
    monkeypatch.setattr(full_check_roster._Run, "_phase_room", lambda self, phase, money: True)
    run = full_check_roster._Run(engine=_engine(tmp_path))  # type: ignore[arg-type]
    run._blind_pick({}, _VENDOR, _ARCHITECT, None, strong=True)
    assert not (tmp_path / pick_cache.PICKS_FILE).exists() and run.picks == {}


def test_a_kept_pick_never_carries_codes_marks(tmp_path: Path) -> None:
    key = pick_cache.pick_key(
        vendor=_VENDOR,
        architect_page=_ARCH_PAGE,
        crops=[[0, 0, 100, 100], [0, 0, 200, 200]],
        model="m",
        effort="high",
        prompt="the question",
    )
    pick_cache.store(
        tmp_path,
        key,
        {**_ANSWER, "pick": "p1.D7", STRONG_PICK: True, pick_cache.CACHED: True},
        model="m",
        effort="high",
    )
    kept = pick_cache.load(tmp_path, key)
    assert kept is not None and kept["pick"] == "p1.D7"
    assert STRONG_PICK not in kept and pick_cache.CACHED not in kept
    # The labels' order on the page never changes the key; the prompt's text does.
    same = pick_cache.pick_key(
        vendor=_VENDOR,
        architect_page=list(reversed(_ARCH_PAGE)),
        crops=[[0, 0, 100, 100], [0, 0, 200, 200]],
        model="m",
        effort="high",
        prompt="the question",
    )
    other = pick_cache.pick_key(
        vendor=_VENDOR,
        architect_page=_ARCH_PAGE,
        crops=[[0, 0, 100, 100], [0, 0, 200, 200]],
        model="m",
        effort="high",
        prompt="another question",
    )
    assert same == key and other != key


# ---- the escalation tries a pick the advocate could not make ------------------------------------


def test_the_escalation_makes_the_pick_the_advocate_could_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The advocate's turn had no room: Not verified; the escalation, with money left, asks the
    pick and the item is settled (before #1829 it read "off its drawing's scale", counted as
    judged, and was never tried again)."""
    from app.review.assistant.full_check_advocate import ADVOCATE_SYSTEM
    from app.review.assistant.full_check_roster import PricePlan
    from tests.review.assistant.chat_fakes import reply, text
    from tests.review.assistant.test_chat_engine import DOLLARS
    from tests.review.assistant.test_full_check_roster import _clean_ink, _Roster, _roster

    class _Strong(_Roster):
        """The strong pick answers as the model it was asked of."""

        def __call__(self, params: Any) -> list[dict[str, Any]]:
            if params["system"][0]["text"] != ADVOCATE_SYSTEM:
                return super().__call__(params)
            self._seen("advocate", params)
            spans = {"from": "floor", "to": "top_of_counter"}
            answer = {"architect_pick": self.advocate_pick, "vendor_spans": spans,
                      "architect_spans": spans, "vendor_ink_black": "yes", "note": ""}  # fmt: skip
            return reply(text(json.dumps(answer)), model=params["model"])

    _clean_ink(monkeypatch)
    script = _Strong()
    harness = _roster(tmp_path, script, price=5 * DOLLARS)
    run = full_check_roster._Run(harness.engine)
    run.plan = PricePlan.balanced(5 * DOLLARS, 2)
    monkeypatch.setattr(
        full_check_roster._Run,
        "_extent",
        lambda self, vendor, architect, page_map=None: ("cannot_tell", "no line found"),
    )
    finding, _notes = harness.engine.book.report(
        {"page": 3, "item": "Top on page 3", "category": "countertop", "status": "different",
         "verified": False, "source": "architect", "vendor_dim": "D1", "architect_dim": "D2"},
        "helper:1",
    )  # fmt: skip
    monkeypatch.setattr(full_check_roster._Run, "_phase_room", lambda self, phase, est: False)
    run._advocate(None)
    found = next(f for f in harness.engine.book.live() if f["id"] == finding["id"])
    assert found["critic"]["note"].startswith(NOT_VERIFIED)
    assert "advocate" not in script.calls

    monkeypatch.setattr(full_check_roster._Run, "_phase_room", lambda self, phase, est: True)
    from types import SimpleNamespace

    run._escalate(SimpleNamespace(jobs=[]), None)
    found = next(f for f in harness.engine.book.live() if f["id"] == finding["id"])
    assert script.calls.get("advocate") == 1
    assert not found["critic"]["note"].startswith(NOT_VERIFIED)
    assert found["critic"]["answers"]["advocate_pick"] == found["critic"]["answers"]["helper_pick"]
    assert not run.stopped_for_money
