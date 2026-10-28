"""Code's own findings on the pages the late look compared (`late_code_findings`, 2026-10-28).

The full check makes code's own findings once, before its proof steps; the late look (#1980) adds
items after that, so they never met them. These tests run the real engine and finding book with
stand-in code steps (each shaped like the real one: GV's rule replacing the items on its label,
the ledger's sums, the measuring of open items) and check that the late round:

- reaches the late items and the late pages (the ledger's sums of the late strings, a GV rule
  replacing a late item on its label, the per-item steps on the late items only);
- never adds a code finding twice, never adds one on a first-round page, and never changes,
  absorbs or proves a first-round item;
- names a step that fails (the check is then not for sending), and does nothing when the first
  round's code findings were never made;
- is what the late look runs before its proof steps, which then take what it added.

Made-up pages, items and values.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.review.assistant import chat_engine, full_check_roster, late_code_findings
from app.review.assistant.full_check_roster import PricePlan
from tests.review.assistant.chat_fakes import ScriptedProvider
from tests.review.assistant.test_chat_engine import Harness

#: The pages: 2 was compared in the first round; 4 is the late look's.
FIRST, LATE = 2, 4


def _item(page: int, item: str, vendor: str = '31"', **more: Any) -> dict[str, Any]:
    return {
        "page": page,
        "item": item,
        "category": "cabinet",
        "status": "different",
        "verified": False,
        "source": "architect",
        "architect": '30"',
        "vendor": vendor,
        **more,
    }


def _rule(page: int) -> dict[str, Any]:
    """GV's rule's item on a run's width label (a stand-in for the site-fit rule)."""
    return {**_item(page, "Run: base width", '30"', source="rules"), "status": "same"}


class _Steps:
    """Stand-ins for code's finding steps, shaped like the real ones, counting their calls."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.seen: dict[str, list[str]] = {}
        self.ledger_out: list[dict[str, Any]] = []

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def rule(self, engine: Any) -> None:
        """Like `run_fit.add_to_answer`: every rule item, the other items on its label absorbed
        first, then the rule's item added (it never looks for its own)."""
        self._count("rule")
        book = engine.book
        with engine.finding_lock:
            for given in (_rule(FIRST), _rule(LATE)):
                for other in book.live():
                    if other.get("created_by") == "run_fit":
                        continue
                    same = (other["page"], other["item"]) == (given["page"], given["item"])
                    if same and book.absorb(str(other["id"])):
                        engine._show_dropped(str(other["id"]), "Settled by GV's rule.")
                added = book.add_code_finding(given, created_by="run_fit")
                if added is not None:
                    engine._show_finding(added)

    def per_item(self, name: str) -> Callable[[Any], None]:
        """Like the scale step: every open item it sees, measured (its words changed)."""

        def step(engine: Any) -> None:
            self._count(name)
            book = engine.book
            self.seen[name] = [str(f["id"]) for f in book.live()]
            for finding in book.live():
                if book.replace({**finding, "why": f"measured by {name}"}):
                    engine._show_finding(finding)

        return step

    def none(self, name: str) -> Callable[[Any], None]:
        def step(_engine: Any) -> None:
            self._count(name)

        return step


@pytest.fixture
def steps(monkeypatch: pytest.MonkeyPatch) -> _Steps:
    found = _Steps()
    for name in ("add_consistency", "add_architect_items", "add_code_pairs", "add_filler_range"):
        monkeypatch.setattr(chat_engine, name, found.none(name))
    for name in ("add_sink_offset", "add_sink_centre"):
        monkeypatch.setattr(chat_engine, name, found.none(name))
    monkeypatch.setattr(chat_engine, "add_run_fit", found.rule)
    monkeypatch.setattr(chat_engine, "add_splash_views", found.per_item("splash"))
    monkeypatch.setattr(chat_engine, "add_scale_measures", found.per_item("scale"))
    return found


def _engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: _Steps) -> Any:
    engine = Harness(tmp_path, ScriptedProvider()).engine
    monkeypatch.setattr(engine, "_ledger_used", lambda: True)
    monkeypatch.setattr(
        engine.reading_ledger, "ledger_findings", lambda live, coverage=None: list(steps.ledger_out)
    )
    return engine


def _ids(engine: Any, **where: Any) -> list[str]:
    return [
        str(f["id"])
        for f in engine.book.live()
        if all(f.get(key) == value for key, value in where.items())
    ]


def _first_round(engine: Any) -> tuple[dict[str, Any], set[str]]:
    """A first-round helper item on page 2 (another item than the rule's), the first round's code
    findings, then the ids live before the late look."""
    kept, _ = engine.book.report(_item(FIRST, "Run: tall width"), "helper:1")
    engine.add_code_findings()
    return kept, {str(f["id"]) for f in engine.book.live()}


def _late_items(engine: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """The late look's helper items on page 4: one on the rule's label, one on another."""
    on_rule, _ = engine.book.report(_item(LATE, "Run: base width"), "helper:9")
    other, _ = engine.book.report(_item(LATE, "Run: tall width"), "helper:9")
    return on_rule, other


# ---- why: the first round runs once ------------------------------------------------------------


def test_the_first_round_runs_once_so_the_late_items_never_meet_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: _Steps
) -> None:
    engine = _engine(tmp_path, monkeypatch, steps)
    _first_round(engine)
    on_rule, _other = _late_items(engine)
    engine.add_code_findings()  # a second call does nothing: the late item stays beside the rule
    assert steps.calls["rule"] == 1
    assert str(on_rule["id"]) in _ids(engine)


# ---- the late round ------------------------------------------------------------------------------


def test_gvs_rule_replaces_a_late_item_on_its_label_and_is_never_added_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: _Steps
) -> None:
    engine = _engine(tmp_path, monkeypatch, steps)
    kept, before = _first_round(engine)
    rules_before = _ids(engine, created_by="run_fit")
    assert len(rules_before) == 2  # the whole-set rule made its item on page 4 too, before
    on_rule, other = _late_items(engine)
    added = engine.add_late_code_findings([LATE], before)
    assert steps.calls["rule"] == 2
    assert str(on_rule["id"]) not in _ids(engine)  # replaced by the rule, as a first-round one is
    assert str(other["id"]) in _ids(engine)
    assert _ids(engine, created_by="run_fit") == rules_before  # never made twice
    assert added == []
    assert str(kept["id"]) in _ids(engine)


def test_the_ledgers_sums_of_the_late_strings_are_added_and_the_first_rounds_never_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: _Steps
) -> None:
    engine = _engine(tmp_path, monkeypatch, steps)
    cross = {
        **_item(FIRST, "Run: base width (same item, different views)"),
        "status": "check",
        "source": "cross_view",
        "reference_side": "vendor",
    }
    steps.ledger_out = [cross]
    _kept, before = _first_round(engine)
    first = _ids(engine, created_by="ledger")
    assert len(first) == 1
    late_sum = {
        "page": LATE,
        "item": "Dimension string",
        "category": "other",
        "architect": '61"',
        "vendor": '60"',
        "status": "different",
        "verified": False,
        "source": "vendor_arithmetic",
        "parts": ['30"', '31"'],
        "printed_total": '60"',
        "ledger_ids": ["S7"],
    }
    on_first_page = {**late_sum, "page": FIRST, "printed_total": '50"', "vendor": '50"'}
    # The ledger now holds the late helpers' string too; it lists the first round's item again,
    # and a string on a first-round page (made there in the first round's terms).
    steps.ledger_out = [cross, late_sum, on_first_page]
    added = engine.add_late_code_findings([LATE], before)
    ledger = _ids(engine, created_by="ledger")
    assert len(ledger) == 2 and ledger[0] == first[0]
    (made,) = [f for f in engine.book.live() if str(f["id"]) == ledger[1]]
    assert made["page"] == LATE and made["printed_total"] == '60"'
    assert added == [ledger[1]]


def test_the_per_item_steps_see_and_change_only_the_late_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: _Steps
) -> None:
    engine = _engine(tmp_path, monkeypatch, steps)
    kept, before = _first_round(engine)
    assert str(kept["id"]) in steps.seen["scale"]  # the first round measured it
    kept_now = next(f for f in engine.book.live() if f["id"] == kept["id"])
    _on_rule, other = _late_items(engine)
    engine.add_late_code_findings([LATE], before)
    assert steps.seen["scale"] == [str(other["id"])]  # the late item the rule left
    assert steps.seen["splash"] == [str(other["id"])]
    late_now = next(f for f in engine.book.live() if f["id"] == other["id"])
    assert late_now["why"] == "measured by scale"
    assert next(f for f in engine.book.live() if f["id"] == kept["id"]) == kept_now


def test_a_first_round_item_is_never_changed_absorbed_or_proved_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: _Steps
) -> None:
    engine = _engine(tmp_path, monkeypatch, steps)
    kept, before = _first_round(engine)
    snapshot = {str(f["id"]): dict(f) for f in engine.book.live()}
    _on_rule, other = _late_items(engine)
    tried: dict[str, list[Any]] = {"replace": [], "absorb": [], "prove": []}

    def greedy(scoped: Any) -> None:
        book = scoped.book
        for finding in book.live():
            fid = str(finding["id"])
            tried["replace"].append((fid, book.replace({**finding, "why": "changed"})))
            tried["prove"].append((fid, book.prove_by_code(fid, {"paired_by": "code"})))
        for finding in book.live():
            tried["absorb"].append((str(finding["id"]), book.absorb(str(finding["id"]))))
        added = book.add_code_finding(_item(FIRST, "Run: new on page 2"), created_by="pairs")
        tried["add"] = [added]

    monkeypatch.setattr(chat_engine, "add_filler_range", greedy)
    engine.add_late_code_findings([LATE], before)
    changed = {fid for fid, ok in tried["replace"] if ok}
    absorbed = {fid for fid, ok in tried["absorb"] if ok}
    assert changed <= {str(other["id"])} and absorbed <= {str(other["id"])}
    assert all(proved is None for fid, proved in tried["prove"] if fid in snapshot)
    assert tried["add"] == [None]  # never a code finding on a first-round page
    for fid, finding in snapshot.items():
        assert next(f for f in engine.book.live() if str(f["id"]) == fid) == finding
    assert str(kept["id"]) in _ids(engine)


def test_a_late_step_that_fails_is_named_and_the_others_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: _Steps
) -> None:
    engine = _engine(tmp_path, monkeypatch, steps)
    monkeypatch.setattr(engine, "settings", dataclasses.replace(engine.settings, mode="full_check"))
    _kept, before = _first_round(engine)
    failed = list(engine.code_checks_failed)
    note = engine.incomplete_note

    def broken(_engine: Any) -> None:
        raise RuntimeError("made-up failure")

    monkeypatch.setattr(chat_engine, "add_sink_offset", broken)
    _late_items(engine)
    engine.add_late_code_findings([LATE], before)
    name = chat_engine.CODE_CHECK_NAMES["sink_offset"]
    assert engine.code_checks_failed == [*failed, name]
    assert name in str(engine.incomplete_note) and engine.incomplete_note != note
    assert steps.calls["scale"] == 2  # the steps after it still ran


def test_nothing_runs_before_the_first_round_or_without_late_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: _Steps
) -> None:
    engine = _engine(tmp_path, monkeypatch, steps)
    assert engine.add_late_code_findings([LATE], set()) == []  # the finish makes them all
    assert steps.calls == {}
    _kept, before = _first_round(engine)
    assert engine.add_late_code_findings([], before) == []
    assert steps.calls["rule"] == 1


def test_the_set_wide_lines_stay_as_the_first_round_made_them() -> None:
    engine = SimpleNamespace(consistency_line="first", consistency_gaps=["gap"], book=None)
    book = late_code_findings.LateBook(SimpleNamespace(live=list), [LATE], set(), (), [])
    view = late_code_findings.ScopedEngine(engine, book)
    view.consistency_line = "again"
    view.consistency_gaps = []
    assert engine.consistency_line == "first" and engine.consistency_gaps == ["gap"]
    assert view.book is book


def test_a_code_findings_signature_is_its_maker_kind_page_values_and_spot() -> None:
    given = _item(LATE, "Run: base width", dims={"vendor": {"id": "D3", "page": LATE}})
    same = {**given, "why": "other words", "ledger_ids": ["S9"]}
    sign = late_code_findings.code_signature
    assert sign(given, "run_fit") == sign(same, "run_fit")
    assert sign(given, "run_fit") != sign(given, "pairs")
    assert sign(given, "run_fit") != sign({**given, "vendor": '32"'}, "run_fit")
    assert sign(given, "run_fit") != sign({**given, "page": FIRST}, "run_fit")


# ---- the late look runs it before its proof steps ----------------------------------------------


def test_the_late_look_adds_codes_findings_and_proves_them_with_its_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live: list[dict[str, Any]] = [{"id": "F1", "page": FIRST}]
    asked: dict[str, Any] = {}

    def late_code(pages: Any, before: Any) -> list[str]:
        asked.update(pages=list(pages), before=set(before))
        live.append({"id": "F3", "page": LATE})  # code's own finding on the late page
        return ["F3"]

    engine = SimpleNamespace(
        spent=0,
        settings=SimpleNamespace(message_cap_micros=5_000_000, round_pick_cap_micros=0),
        package_left=10**9,
        helper_ceiling=None,
        cancel=threading.Event(),
        _lock=threading.RLock(),
        _plan=SimpleNamespace(jobs=()),
        _progress=None,
        timings=SimpleNamespace(phase=lambda _name: _null()),
        book=SimpleNamespace(live=lambda: list(live), drop_coverage_note=lambda *_a: None),
        _fits=lambda estimate: True,
        _price=lambda *_a: 0,
        _wait_checks=lambda: None,
        _show_coverage=lambda: None,
        add_late_code_findings=late_code,
    )
    run = full_check_roster._Run(engine=engine)  # type: ignore[arg-type]
    run.plan = PricePlan.balanced(5_000_000, groups=5, pages=13)
    run.money_pages = {LATE}
    monkeypatch.setattr(
        full_check_roster._Run,
        "_still_open",
        lambda self, band=None: {LATE: ["page not compared yet (x): compare"]},
    )

    def close(self: Any, pages: Mapping[int, Any], *_a: Any, **_k: Any) -> list[int]:
        live.append({"id": "F2", "page": LATE})  # the late helper's item
        return sorted(pages)

    proved: list[frozenset[str]] = []
    monkeypatch.setattr(full_check_roster._Run, "_close", close)
    monkeypatch.setattr(
        full_check_roster._Run,
        "_late_proofs",
        lambda self, plan, page_map, new: proved.append(new),
    )
    run._late_pages(None, None)
    assert asked == {"pages": [LATE], "before": {"F1"}}
    assert proved == [frozenset({"F2", "F3"})]  # code's finding takes the late proof steps too


class _null:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_a: object) -> None:
        return None
