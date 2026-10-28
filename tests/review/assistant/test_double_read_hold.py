"""A red needs its second reading (Anant, 2026-10-28, together with the reading fix). $0:
made-up findings, a fake reading model.

- A would-be red whose second reading did not run (the cap, a failed call, an outage, a credit
  stop, the check stopped or ended first) is never red: a please-check "not double-read" with the
  reason; a Continue reads it.
- Every would-be red is read whatever the old cap of 30 (`test_second_reads_first.py`).
- A reading is keyed by its pictures' bytes and its question: an identical picture drawn again is
  the same reading, no new call.
"""

from __future__ import annotations

import threading
import types
from typing import Any

import pytest

from app.review.assistant import chat_engine, proof, reading_routes
from app.review.assistant.chat_engine import ChatEngine
from app.review.assistant.chat_provider import ProviderFailure
from app.review.assistant.read_adjudication import SideCrops
from app.review.assistant.read_spots import ReadPictures, ReadSpot
from app.review.assistant.second_read import SecondRead
from tests.review.assistant.test_reading_routes import _Check, _Fit, _label, _marked, _wrong

NOT_DOUBLE_READ = reading_routes.NOT_DOUBLE_READ


def _proved(**checks: Any) -> dict[str, Any]:
    """A Wrong code proved in every way, both numbers read by two routes."""
    fit = _Fit({"D1": _Check(True, 23.0), "D9": _Check(True, 24.0)})
    finding = _wrong(_label("D1", '23"', "23", "vendor"), _label("D9", '24"', "24", "architect"))
    finding["checks"] = {**finding["checks"], **checks}
    return _marked(finding, fit)


def test_a_red_with_its_second_reading_stays_red() -> None:
    assert proof.proof_group(_proved(second_read="agrees")) == "clouded"


@pytest.mark.parametrize(
    "why",
    [
        "the AI call failed",
        "the AI service could not be reached",
        "the AI account ran out of credit",
        "past the 200 readings one check makes at most",
    ],
)
def test_a_red_never_read_twice_is_a_please_check_that_says_why(why: str) -> None:
    finding = {**_proved(second_read="not_run"), NOT_DOUBLE_READ: why}
    assert proof.proof_group(finding) == "likely_problem"
    assert reading_routes.held(finding) == f"not double-read: {why}"
    assert "not double-read" in proof.please_check_words(finding)


def test_a_finding_from_a_check_that_reads_once_is_judged_as_before() -> None:
    """The simple full check reads once by design: it never marks a finding, so nothing moves."""
    assert proof.proof_group(_proved(second_read="not_run")) == "clouded"


# ---- the engine's side: the mark, the failures, the slots ------------------------------------------


class _Book:
    def __init__(self, findings: list[dict[str, Any]]) -> None:
        self.found = {f["id"]: dict(f) for f in findings}

    def live(self) -> list[dict[str, Any]]:
        return [dict(f) for f in self.found.values()]

    def replace(self, finding: dict[str, Any]) -> bool:
        if finding["id"] not in self.found:
            return False
        self.found[finding["id"]] = dict(finding)
        return True


def _red(id_: str = "F1", **more: Any) -> dict[str, Any]:
    return {
        "id": id_,
        "item": "Run: depth",
        "status": "different",
        "architect": '24"',
        "vendor": '23"',
        "inches": {"vendor": "23", "architect": "24"},
        "vendor_box": {"page": 1, "x0": 1, "y0": 1, "x1": 9, "y1": 9},
        "checks": {"second_read": "not_run"},
        **more,
    }


def _engine(findings: list[dict[str, Any]], picture: bytes = b"same picture") -> Any:
    engine: Any = types.SimpleNamespace(
        _lock=threading.Lock(),
        finding_lock=threading.RLock(),
        cancel=threading.Event(),
        book=_Book(findings),
        _reads={},
        _read_slots={},
        _read_waiting={},
        _read_share=None,
        shown=[],
        thirds=[],
    )
    engine._show_finding = engine.shown.append
    engine._queue_third_read = engine.thirds.append
    engine.reading_pictures = lambda finding: ReadPictures(
        crops={"vendor": SideCrops(upright=picture)},
        spots={"vendor": ReadSpot("vendor", None, 1, (0.0, 0.0, 96.0, 96.0), values=1)},
    )
    engine._second_read_call = lambda content, share: "{}"
    for name in (
        "_not_double_read",
        "_apply_second_read",
        "_second_read_one",
        "_mark_unread_reds",
        "_reads_twice",
        "_unread_why",
    ):
        setattr(engine, name, getattr(ChatEngine, name).__get__(engine))
    return engine


class _Reader:
    """A reading module: `fail` raises on every call; else reads the vendor's 23"."""

    def __init__(self, fail: BaseException | None = None) -> None:
        self.fail = fail
        self.calls = 0

    def second_read(self, crops: Any, item: str, call: Any, **_words: Any) -> SecondRead:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return SecondRead(architect=None, vendor='23"', sure=True, asked=("vendor",))

    def compare_read(self, read: SecondRead, finding: Any) -> SecondRead:
        from app.review.assistant.second_read import compare_read

        return compare_read(read, finding)


@pytest.mark.parametrize(
    ("failure", "why"),
    [
        (ProviderFailure("boom", status=500, retryable=False), "the AI call failed"),
        (
            ProviderFailure("down", status=None, retryable=False, kind="unreachable"),
            "the AI service could not be reached",
        ),
        (
            ProviderFailure("pay", status=402, retryable=False, kind="payment"),
            "the AI account ran out of credit",
        ),
    ],
)
def test_a_failed_reading_marks_the_would_be_red(failure: ProviderFailure, why: str) -> None:
    engine = _engine([_red()])
    engine._second_read_one(_Reader(failure), _red())
    held = engine.book.found["F1"]
    assert held[NOT_DOUBLE_READ] == why
    assert held["checks"]["second_read"] == "not_run"
    assert engine._read_slots == {}  # never made: a later queue (or a Continue) may ask again


def test_a_reading_made_later_clears_the_mark() -> None:
    engine = _engine([_red(**{NOT_DOUBLE_READ: "the AI call failed"})])
    engine._second_read_one(_Reader(), _red())
    read = engine.book.found["F1"]
    assert read["checks"]["second_read"] == "agrees" and NOT_DOUBLE_READ not in read


def test_an_identical_picture_is_one_reading() -> None:
    findings = [_red("F1"), _red("F2")]
    engine = _engine(findings)
    reader = _Reader()
    for finding in findings:
        engine._second_read_one(reader, finding)
    assert reader.calls == 1  # the second finding's identical picture and question: no new call
    assert all(f["checks"]["second_read"] == "agrees" for f in engine.book.live())


def test_a_would_be_red_not_reached_by_the_finish_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chat_engine, "load_module", lambda name: object())
    engine = _engine([_red("F1"), _red("F2", checks={"second_read": "unsure"})])
    engine._simple = False
    engine._mark_unread_reds()
    assert engine.book.found["F1"][NOT_DOUBLE_READ] == "it was not reached before the check ended"
    assert NOT_DOUBLE_READ not in engine.book.found["F2"]  # read: its own doubt holds it


def test_the_simple_check_marks_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_engine, "load_module", lambda name: object())
    engine = _engine([_red("F1")])
    engine._simple = True
    engine._mark_unread_reds()
    assert NOT_DOUBLE_READ not in engine.book.found["F1"]


# ---- the invariant: nothing is stored red without its second reading ------------------------------


def test_no_red_is_stored_without_its_second_reading(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whole roster full check whose second readings all fail: every would-be red is stored a
    please-check "not double-read", none red; and the stored answer's last guard marks one that a
    late step left unmarked."""
    from tests.review.assistant.test_full_check_roster import _clean_ink, _Roster, _roster

    _clean_ink(monkeypatch)
    script = _Roster()
    asked: list[str] = []

    def answer(params: Any) -> Any:
        system = params["system"][0]["text"]
        if system == chat_engine.SECOND_READ_SYSTEM and params["model"].startswith("claude-haiku"):
            asked.append("read")
            raise ProviderFailure("boom", status=500, retryable=False)
        return script(params)

    harness = _roster(tmp_path, _Roster())
    harness.provider.helper_answer = answer
    outcome = harness.run()

    findings = [b["finding"] for b in outcome.blocks if b["type"] == "finding"]
    assert asked  # the readings were asked, and failed
    marked = [f for f in findings if f.get(NOT_DOUBLE_READ)]
    assert marked and all(proof.proof_group(f) != "clouded" for f in marked)
    for finding in findings:
        if proof.proof_group(finding) == "clouded":
            assert finding["checks"]["second_read"] not in (None, "not_run"), finding["id"]

    # A late step that rebuilds a would-be red without its mark: the stored answer marks it.
    engine = harness.engine
    one = next(f for f in engine.book.live() if f.get(NOT_DOUBLE_READ))
    stripped = {k: v for k, v in one.items() if k != NOT_DOUBLE_READ}
    engine.book.replace(stripped)
    engine._show_finding(stripped)
    stored = next(
        b["finding"]
        for b in engine.stored_blocks()
        if b["type"] == "finding" and b["finding"]["id"] == one["id"]
    )
    assert stored[NOT_DOUBLE_READ] == chat_engine.NOT_REACHED_WHY


def test_one_card_keeps_the_kept_cards_own_mark() -> None:
    """One item, one card: the card kept is one of the copies, unchanged, so it keeps its own
    "not double-read" mark; the copies folded on it never decide its group."""
    from app.review.assistant import one_card

    box = {"page": 1, "x0": 10.0, "y0": 10.0, "x1": 30.0, "y1": 18.0, "file": "set.pdf"}
    copies = [
        {**_proved(second_read="not_run"), "id": f"F{n}", "vendor_box": box,
         NOT_DOUBLE_READ: "the AI call failed"}
        for n in (1, 2)
    ]  # fmt: skip
    folded = one_card.fold(copies)
    kept = folded.kept or copies
    assert all(k.get(NOT_DOUBLE_READ) == "the AI call failed" for k in kept)
    assert all(proof.proof_group(k) == "likely_problem" for k in kept)
