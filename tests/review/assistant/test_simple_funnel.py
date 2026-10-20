"""The simple full check's funnel (#1767): every run and every replay writes `funnel.json` in its
run folder, with counts and ids at each step (labels read, the AI's pairs, code's verify by reason
and check, the resolve loop, the blind second look, the end), per vendor drawing and in total; the
totals ride on the `work_items` block. A real chat engine on a made-up set, the compare calls and
the blind look replaced; no model call, no money."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant.chat_contract import ChatWorkItemsBlock
from app.review.assistant.full_check_advocate import Check
from app.review.assistant.simple_check import blind as blind_module
from app.review.assistant.simple_check import funnel as funnel_module
from app.review.assistant.simple_check import run as run_module
from app.review.assistant.simple_check.schema import (
    CompareOutcome,
    CompareResult,
    Drawing,
    Label,
    OtherItem,
    Pair,
    WorkItem,
)
from app.review.assistant.simple_check.verify import Item, Region, Verification, Verified
from tests.review.assistant.test_simple_run_stop import _agreeing, _fake_compare, _Run

FILE = "set.pdf"
BOX = (0.0, 0.0, 10.0, 10.0)


def _label(page: int, n: int, side: str, route: str = "text") -> Label:
    return Label(
        ref=f"p{page}.D{n}",
        page=page,
        id=f"D{n}",
        text=f'{n}"',
        inches=str(n),
        box=BOX,
        side=side,
        route=route,
    )


def _drawing(page: int, role: str, labels: tuple[Label, ...], ref: str) -> Drawing:
    return Drawing(
        ref=ref,
        file=FILE,
        page=page,
        half=0,
        role=role,  # type: ignore[arg-type]
        box=BOX,
        crop_box=BOX,
        labels=labels,
    )


def _item(page: int) -> WorkItem:
    vendor = (
        _label(page, 1, "vendor"),
        _label(page, 2, "vendor"),
        _label(page, 3, "vendor", "drawn"),
    )
    architect = (_label(page, 11, "architect"), _label(page, 12, "architect"))
    return WorkItem(
        id=f"p{page}.v1",
        file=FILE,
        page=page,
        vendor=_drawing(page, "vendor", vendor, f"p{page}.v1"),
        architect=(_drawing(page, "architect", architect, f"p{page}.a1"),),
        round=1,
    )


def _pair(page: int, vendor: int, architect: int | None, verdict: str) -> Pair:
    return Pair(
        vendor_label=f"p{page}.D{vendor}",
        architect_label=None if architect is None else f"p{page}.D{architect}",
        measures="width",
        vendor_from="wall_face",
        vendor_to="cabinet_side",
        architect_from="wall_face",
        architect_to="cabinet_side",
        verdict=verdict,  # type: ignore[arg-type]
        note="",
    )


def _answered(page: int, pairs: tuple[Pair, ...], unaccounted: tuple[str, ...]) -> CompareOutcome:
    other = OtherItem(
        kind="missing_on_vendor",
        what="toe kick",
        vendor=None,
        architect='4"',
        vendor_box=None,
        architect_box=None,
        architect_page=page,
        verdict="different",
        millwork=True,
    )
    return CompareOutcome(
        work_item=f"p{page}.v1",
        status="ok",
        result=CompareResult(
            work_item=f"p{page}.v1",
            pairs=pairs,
            other_items=(other,),
            unaccounted_labels=unaccounted,
        ),
        asked_model="claude-sonnet-5-5",
        model="claude-sonnet-5-5",
        effort="high",
        cost_usd=0.1,
    )


def _v(key: str, outcome: str, checks: tuple[Check, ...] = (), **more: Any) -> Verified:
    page = int(key.split(".")[0][1:])
    vendor = key.split(":")[1]
    label = {"page": page, "id": vendor.split(".")[-1]} if vendor.startswith("p") else None
    return Verified(
        key=key,
        kind=more.pop("kind", "pair"),
        outcome=outcome,  # type: ignore[arg-type]
        checks=checks,
        vendor=label,
        **more,
    )


PASS = Check("roles", "pass")
ROUND_FAILS = Check("round", "fail", "not the newest round")


def _funnel() -> dict[str, Any]:
    """Three drawings: p1 answered (a red, a Right, a difference code refused, an absence the blind
    look refused), p2 answered (a candidate the resolve loop made, then a stopped look), p3 failed.
    """
    items = [_item(1), _item(2), _item(3)]
    outcomes = {
        "p1.v1": _answered(
            1,
            (_pair(1, 1, 11, "different"), _pair(1, 2, 12, "same"), _pair(1, 3, 12, "different")),
            (),
        ),
        "p2.v1": _answered(2, (_pair(2, 1, 11, "unsure"),), ("p2.D2",)),
        "p3.v1": CompareOutcome(
            work_item="p3.v1",
            status="failed",
            reason="the call failed",
            asked_model="claude-sonnet-5-5",
            effort="high",
        ),
    }
    item = Item(id="x", vendor=Region(FILE, 1, BOX))
    first_p1 = [
        _v("p1.v1:p1.D1", "candidate", (PASS,)),
        _v("p1.v1:p1.D2", "right", (PASS,)),
        _v("p1.v1:p1.D3", "not_compared", (PASS, ROUND_FAILS), reason_code="not_proved"),
        _v("p1.v1:item1", "candidate", (PASS,), kind="missing_on_vendor", blind="absence"),
    ]
    first_p2 = [_v("p2.v1:p2.D1", "not_compared", (PASS,), reason_code="not_proved")]
    blind_pass = Check("blind", "pass")
    blind_no = Check("blind", "fail", "not proved: a blind second look finds it on the vendor's")
    stopped = Check(
        "blind",
        "fail",
        "not proved: the blind second look was not made (the check was stopped)",
    )
    rounds = ({"round": 1, "status": "candidate"},)
    final = [
        _v("p1.v1:p1.D1", "red", (PASS, blind_pass)),
        _v("p1.v1:p1.D2", "right", (PASS,)),
        _v("p1.v1:p1.D3", "not_compared", (PASS, ROUND_FAILS), reason_code="not_proved"),
        _v("p1.v1:item1", "not_compared", (PASS, blind_no), kind="missing_on_vendor"),
        _v("p2.v1:p2.D1", "not_compared", (PASS, stopped), resolve=rounds),
    ]
    return funnel_module.build(
        items,
        compared=["p1.v1", "p2.v1", "p3.v1"],
        skipped={},
        outcomes=outcomes,
        verifications={
            "p1.v1": Verification(item, first_p1),
            "p2.v1": Verification(item, first_p2, missing_labels=["p2.D3"]),
        },
        final=final,
        meta={"run": "r1"},
    )


def test_the_funnel_counts_each_step_per_drawing_and_in_total() -> None:
    funnel = _funnel()
    assert funnel["schema"] == "gv-simple-funnel/1" and funnel["run"] == "r1"
    p1, p2, p3 = (funnel["drawings"][f"p{n}.v1"] for n in (1, 2, 3))
    # Labels read: 3 vendor (one drawn) and 2 architect on each drawing.
    assert p1["labels"]["vendor"] == 3 and p1["labels"]["vendor_by_route"] == {
        "text": 2,
        "drawn": 1,
    }
    assert funnel["totals"]["labels"]["vendor"] == 9
    assert funnel["totals"]["labels"]["architect"] == 6
    # The AI's answer: pairs by verdict, other items, unaccounted and never named, with ids.
    assert p1["ai"]["verdicts"]["different"] == 2 and p1["ai"]["verdicts"]["same"] == 1
    assert p2["ai"]["unaccounted_ids"] == ["p2.D2"] and p2["ai"]["never_named_ids"] == ["p2.D3"]
    assert funnel["totals"]["ai"]["paired_with_architect"] == 4
    assert funnel["totals"]["ai"]["other_kinds"] == {"missing_on_vendor": 2}
    # Code verifies: outcomes, reasons, the first failed check, every check's pass / fail.
    assert p1["verify"]["outcomes"]["candidate"] == 2
    assert p1["verify"]["first_failed_check"] == {"round": 1}
    assert p1["verify"]["checks"]["round"] == {"fail": 1}
    assert funnel["totals"]["verify"]["reasons"] == {"not_proved": 2}
    assert funnel["totals"]["verify"]["checks"]["roles"] == {"pass": 5}
    # The resolve loop and the blind second look.
    assert p2["resolve"]["entered"] == 1 and p2["resolve"]["candidate"] == 1
    assert p1["blind"] == {
        "candidates": 2,
        "asked": 2,
        "agreed": 1,
        "disagreed": 1,
        "failed": 0,
        "skipped": 0,
        "skipped_why": {},
        "agreed_ids": ["p1.v1:p1.D1"],
        "disagreed_ids": ["p1.v1:item1"],
    }
    assert p2["blind"]["skipped"] == 1
    assert p2["blind"]["skipped_why"] == {"the check was stopped": 1}
    # The end, and the failed drawing.
    assert funnel["totals"]["final"]["red"] == 1 and funnel["totals"]["final"]["right"] == 1
    assert funnel["totals"]["final"]["not_compared"] == 3
    assert funnel["totals"]["final"]["red_ids"] == ["p1.v1:p1.D1"]
    assert p3["status"] == "failed" and p3["why"] == "the call failed" and p3["ai"] == {}
    assert funnel["totals"]["work_items"] == {
        "total": 3,
        "answered": 2,
        "failed": 1,
        "skipped": 0,
        "by_model": {"claude-sonnet-5-5": 2},
    }


def test_the_trail_names_every_possible_difference_by_ids_only() -> None:
    trail = {row["key"]: row for row in _funnel()["trail"]}
    # The Right "same" pair is not on it; differences, unsure, absences and reds are.
    assert set(trail) == {"p1.v1:p1.D1", "p1.v1:p1.D3", "p1.v1:item1", "p2.v1:p2.D1"}
    assert trail["p1.v1:p1.D1"] == {
        "key": "p1.v1:p1.D1",
        "work_item": "p1.v1",
        "page": 1,
        "kind": "pair",
        "vendor_label": "p1.D1",
        "architect_label": None,
        "ai": "different",
        "verify": "candidate",
        "failed_check": None,
        "reason_code": None,
        "resolve": "not_entered",
        "blind": "agreed",
        "final": "red",
    }
    assert trail["p1.v1:p1.D3"]["failed_check"] == "round"
    assert trail["p2.v1:p2.D1"]["resolve"] == "candidate"
    assert trail["p2.v1:p2.D1"]["blind"] == "skipped"


def test_the_headline_is_the_block_field() -> None:
    line = funnel_module.headline(_funnel())
    assert line == {
        "drawings": 2,
        "labels_read": 9,
        "paired": 4,
        "unaccounted": 2,
        "verified_different": 2,
        "resolved": 1,
        "second_looks": 2,
        "agreed": 1,
        "red": 1,
        "right": 1,
        "not_compared": 3,
    }
    block = ChatWorkItemsBlock.model_validate(
        {"type": "work_items", "work_items": [], "run": "r1", "funnel": line}
    )
    assert block.funnel is not None and block.funnel.red == 1
    # A run from before the funnel still reads.
    assert (
        ChatWorkItemsBlock.model_validate({"type": "work_items", "work_items": []}).funnel is None
    )


def test_a_run_writes_its_funnel_and_the_block_carries_the_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _Run(tmp_path)
    _fake_compare(monkeypatch, stop_after=None, cancel=check.cancel)
    monkeypatch.setattr(blind_module, "look", _agreeing)
    outcome = check.engine.run()
    folder = check.run_dir()
    funnel = json.loads((folder / "funnel.json").read_text())
    assert funnel["schema"] == "gv-simple-funnel/1"
    assert funnel["run"] == folder.name and funnel["replay_of"] is None
    assert funnel["totals"]["work_items"]["answered"] == 3
    assert funnel["totals"]["final"]["red"] == 1
    assert funnel["totals"]["blind"]["agreed"] == 1
    assert [row["final"] for row in funnel["trail"] if row["blind"] == "agreed"] == ["red"]
    assert float(funnel["cost"]["compare"]) == pytest.approx(0.15)
    work = next(b for b in outcome.blocks if b.get("type") == "work_items")
    assert work["funnel"] == funnel_module.headline(funnel)
    assert work["funnel"]["red"] == 1 and work["funnel"]["labels_read"] > 0
    ChatWorkItemsBlock.model_validate(work)
    # Replayed at $0 from the saved answers: the same end.
    _items, again = run_module.replay_with_funnel(
        folder, check.pack, main_file="set.pdf", read=lambda *_a: None, ink=lambda _l: None
    )
    assert again["replay_of"] == folder.name
    assert again["totals"]["final"]["red"] == funnel["totals"]["final"]["red"]
    assert again["totals"]["labels"] == funnel["totals"]["labels"]


def test_a_stop_counts_the_looks_not_made(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    check = _Run(tmp_path)
    _fake_compare(monkeypatch, stop_after=1, cancel=check.cancel)
    monkeypatch.setattr(blind_module, "look", _agreeing)
    outcome = check.engine.run()
    assert outcome.stopped == "cancelled"
    funnel = json.loads((check.run_dir() / "funnel.json").read_text())
    assert funnel["stop"]["stopped"] == "cancelled"
    assert funnel["totals"]["work_items"]["failed"] == 1
    assert funnel["totals"]["blind"]["skipped"] == 1 and funnel["totals"]["final"]["red"] == 0


def test_a_funnel_that_cannot_be_made_never_stops_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _Run(tmp_path)
    _fake_compare(monkeypatch, stop_after=None, cancel=check.cancel)
    monkeypatch.setattr(blind_module, "look", _agreeing)

    def broken(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(funnel_module, "build", broken)
    outcome = check.engine.run()
    work = next(b for b in outcome.blocks if b.get("type") == "work_items")
    assert "funnel" not in work and outcome.stopped is None
    assert not (check.run_dir() / "funnel.json").exists()
