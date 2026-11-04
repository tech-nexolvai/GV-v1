"""The slot call: pictures with offset badges, the mark table, the questions, and code applying the
answers; nothing at all when the switch is off.

Mirrors `app/review/assistant/slot_look.py`, `slot_prompt.py` and the slot rows of
`coverage_ledger.py`. A fake crop and a fake recorder; no AI call; made-up labels and values.
"""

from __future__ import annotations

import io
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest
from PIL import Image

from app.review.assistant import coverage_ledger, slot_look, slot_prompt, slots, whole_look
from app.review.assistant.slot_answers import MarkMap

WHEN = datetime(2026, 1, 2, tzinfo=UTC)
V, A = "vendor.pdf", "architect.pdf"


def _png(width: int = 400, height: int = 300) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


@dataclass
class _Crop:
    png: bytes
    box: tuple[float, float, float, float]
    width: int
    height: int


def _fake_crop(file: str, page: int, box: tuple[float, float, float, float]) -> _Crop:
    return _Crop(_png(), box, 400, 300)


def _label(lid: str, text: str, inches: str, x: float, *, side: str = "vendor") -> dict[str, Any]:
    return {
        "id": lid,
        "text": text,
        "inches": inches,
        "side": side,
        "route": "text",
        "orientation": "h",
        "box": [x, 100.0, x + 30, 110.0],
        "dim_line": [[x - 20, 115.0], [x + 50, 115.0]],
        "extension": [[[x - 20, 90.0], [x - 20, 120.0]], [[x + 50, 90.0], [x + 50, 120.0]]],
    }


class _Labels:
    def __init__(self, pages: Mapping[tuple[str, int], list[dict[str, Any]]]) -> None:
        self.pages = pages

    def on_page(self, file: str, page: int, *, read_now: bool = True) -> list[dict[str, Any]]:
        return [dict(label) for label in self.pages.get((file, page), [])]


def _item() -> slot_look.Item:
    return slot_look.Item(
        whole_look.Drawing(V, 2, (0.0, 0.0, 400.0, 300.0), "vendor"),
        whole_look.Drawing(A, 5, (0.0, 0.0, 400.0, 300.0), "architect"),
    )


def _labels() -> _Labels:
    return _Labels(
        {
            (V, 2): [_label("D1", '1 1/4"', "1.25", 60), _label("D2", '24"', "24", 200)],
            (A, 5): [_label("D7", '1"', "1", 80, side="architect")],
        }
    )


def test_off_by_default_makes_no_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(slots.SLOTS_ENV, raising=False)

    class Untouchable:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"the off switch touched run.{name}")

    assert slot_look.roster_agents(Untouchable(), [1, 2], None, None, slot_look.SlotRuns()) == []


def test_the_roster_makes_no_slot_call_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The roster's slot agents come only from `roster_agents`, which is empty when off."""
    monkeypatch.delenv(slots.SLOTS_ENV, raising=False)
    source = Path(slot_look.__file__).with_name("full_check_roster.py").read_text(encoding="utf-8")
    assert source.count("slot_look.roster_agents(") == 1
    assert "agents += slotted" in source


def test_marks_cover_both_drawings_and_the_table_carries_code_values(tmp_path: Path) -> None:
    item = _item()
    marks = slot_look.mark_map(_labels(), item, "slots-p2")
    assert len(marks.marks("vendor")) == 2 and len(marks.marks("architect")) == 1
    content = slot_look.opening(item, marks, _fake_crop, tmp_path)
    images = [block for block in content if block["type"] == "image"]
    assert len(images) == 2
    assert all((tmp_path / f"{b['source']['picture_id']}.png").is_file() for b in images)
    table = content[-1]["text"]
    assert "<drawing_data" in table and '1 1/4"' in table and "architect" in table


def test_badges_stay_off_the_extension_lines() -> None:
    labels = [_label("D1", '1 1/4"', "1.25", 60), _label("D2", '24"', "24", 200)]
    placed: list[tuple[float, float, float, float]] = []
    png = slot_prompt.draw_slot_marks(
        _png(), (0, 0, 400, 300), [("M1", labels[0]), ("M2", labels[1])], placed
    )
    assert png.startswith(b"\x89PNG") and len(placed) == 2
    for tag, label in zip(placed, labels, strict=True):
        for (ax, ay), (bx, by) in label["extension"]:
            steps = max(2, int(math.hypot(bx - ax, by - ay)))
            for n in range(steps + 1):
                x, y = ax + (bx - ax) * n / steps, ay + (by - ay) * n / steps
                assert not (tag[0] <= x <= tag[2] and tag[1] <= y <= tag[3])
        box = label["box"]
        assert not (tag[0] < box[2] and box[0] < tag[2] and tag[1] < box[3] and box[1] < tag[3])


def test_the_brief_carries_each_question_with_its_rule_and_anti_patterns() -> None:
    asked = slots.catalogue()
    text = slot_prompt.brief(asked, has_architect=False)
    for one in asked:
        assert f"[{one.key}]" in text
    thickness = slots.slot("ct_thickness")
    assert thickness is not None and thickness.rule.split()[0] in text
    assert "not_on_architect_view" in text
    assert f"Not a problem: {thickness.not_a_problem_when[0][:40]}" in text
    assert not re.search(r"\d\s*(?:\"|in\b|mm\b)", slot_prompt.SYSTEM)  # no size in the system text


def _reply(marks: MarkMap, vendor: str, architect: str) -> dict[str, Any]:
    return {
        "slots": [
            {
                "slot": "ct_thickness",
                "status": "answered",
                "not_shown_reason": "none",
                "answers": [
                    {
                        "what_it_measures": "thickness",
                        "shop_mark_ids": [vendor],
                        "shop_against_mark_ids": [],
                        "arch_mark_ids": [architect],
                        "ends": {"left": "not_asked", "right": "not_asked"},
                    }
                ],
            },
            {
                "slot": "toe_kick",
                "status": "not_shown",
                "not_shown_reason": "not_on_vendor_view",
                "answers": [],
            },
        ]
    }


def test_code_applies_the_answers_and_reports_every_slot() -> None:
    item = _item()
    marks = slot_look.mark_map(_labels(), item, "slots-p2")
    thick = marks.mark_of(V, 2, "D1")
    arch = marks.mark_of(A, 5, "D7")
    assert thick and arch
    recorded: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def record(raw: dict[str, Any], extra: dict[str, Any]) -> str:
        recorded.append((raw, extra))
        return "F9"

    state = slot_look.SlotRuns()
    slot_look.apply_item(
        item=item,
        marks=marks,
        reply=_reply(marks, thick, arch),
        record=record,
        rules_md=None,
        when=WHEN,
        state=state,
    )
    assert len(recorded) == 1
    raw, extra = recorded[0]
    assert raw["vendor_dim"] == "p2.D1" and raw["architect_dim"] == "p5.D7"
    assert raw["status"] == "different" and extra["slot"]["key"] == "ct_thickness"
    assert extra["maths"]["read_from"] == "slots"
    rows = {(row.question, row.status) for row in state.rows}
    assert ("ct_thickness", "answered") in rows and ("toe_kick", "not_shown") in rows
    assert ("ct_overall_length", "not_asked") in rows
    assert len(state.rows) == len(slots.catalogue())


def test_an_agreement_is_reported_not_recorded() -> None:
    item = _item()
    marks = slot_look.mark_map(_labels(), item, "slots-p2")
    same = marks.mark_of(V, 2, "D1")
    labels = _labels()
    labels.pages[(A, 5)][0].update({"text": '1 1/4"', "inches": "1.25"})
    marks = slot_look.mark_map(labels, item, "slots-p2")
    same, arch = marks.mark_of(V, 2, "D1"), marks.mark_of(A, 5, "D7")
    assert same and arch
    state = slot_look.SlotRuns()
    slot_look.apply_item(
        item=item,
        marks=marks,
        reply=_reply(marks, same, arch),
        record=lambda raw, extra: pytest.fail("an agreement is never recorded"),
        rules_md=None,
        when=WHEN,
        state=state,
    )
    assert state.kept[0]["outcome"] == "same" and not state.recorded


def test_a_reply_that_breaks_the_shape_leaves_every_slot_not_asked() -> None:
    item = _item()
    marks = slot_look.mark_map(_labels(), item, "slots-p2")
    state = slot_look.SlotRuns()
    slot_look.apply_item(
        item=item,
        marks=marks,
        reply={"slots": [], "value": '36"'},
        record=lambda raw, extra: None,
        rules_md=None,
        when=WHEN,
        state=state,
    )
    assert {row.status for row in state.rows} == {"not_asked"}
    assert state.dropped and "refused" in state.dropped[0]


# ---- the coverage ledger -------------------------------------------------------------------------


def _ledger() -> coverage_ledger.Ledger:
    items = (coverage_ledger.Item(key="p2", kind="countertop_piece", label="page 2", pages=(2,)),)
    return coverage_ledger.ledger_for("job", items, [], group_of=lambda f: "likely_problem")


def test_slot_rows_fill_the_grid() -> None:
    before = _ledger()
    rows = [
        coverage_ledger.SlotRow("p2", "ct_thickness", "answered", outcome="differs"),
        coverage_ledger.SlotRow(
            "p2", "ct_counter_height", "answered", outcome="same", red_barred=True
        ),
        coverage_ledger.SlotRow("p2", "toe_kick", "not_shown", "not_on_vendor_view"),
        coverage_ledger.SlotRow("p2", "ct_finished_depth", "not_shown", "printed_without_a_mark"),
        coverage_ledger.SlotRow("p9", "ct_thickness", "answered", outcome="same"),
    ]
    after = coverage_ledger.with_slot_answers(before, rows)
    assert after.answered == before.answered + 2
    cell = {(c.item, c.question): c for c in after.cells}
    assert cell[("p2", "ct_thickness")].confidence == "proved"
    assert cell[("p2", "ct_counter_height")].confidence == "flagged"
    assert cell[("p2", "ct_finished_depth")].gap == "not_shown"
    assert ("p2", "toe_kick") not in cell  # toe kick is not a countertop question: not in the grid
    assert coverage_ledger.slot_summary(rows) == {"answered": 3, "not_shown": 2, "not_asked": 0}


def test_no_slot_rows_leave_the_ledger_as_it_was() -> None:
    before = _ledger()
    assert coverage_ledger.with_slot_answers(before, []).summary() == before.summary()


def test_an_unknown_slot_status_is_refused() -> None:
    with pytest.raises(ValueError, match="slot status"):
        coverage_ledger.with_slot_answers(
            _ledger(), [coverage_ledger.SlotRow("p2", "ct_thickness", "maybe")]
        )


# ---- the roster hooks, with a fake engine ---------------------------------------------------------


def _page_map() -> dict[str, Any]:
    def entry(file: str, page: int, role: str) -> dict[str, Any]:
        return {
            "file": file,
            "page": page,
            "halves": [{"box": [0, 0, 400, 300], "role": role, "heading": None, "round": None}],
            "from_upload": True,
        }

    return {"pages": [entry(V, 2, "vendor"), entry(A, 5, "architect"), entry(V, 3, "vendor")]}


class _Cross:
    pairs: ClassVar[dict[int, list[tuple[str, int]]]] = {2: [(A, 5)]}


class _Book:
    main_file = V

    def __init__(self) -> None:
        self.dim_labels = _labels()
        self.reported: list[dict[str, Any]] = []
        self.replaced: list[dict[str, Any]] = []

    def report(self, raw: dict[str, Any], created_by: str) -> tuple[dict[str, Any], list[str]]:
        assert created_by == "slots"
        finding = {"id": f"F{len(self.reported) + 1}", **raw}
        self.reported.append(finding)
        return finding, []

    def replace(self, finding: dict[str, Any]) -> bool:
        self.replaced.append(finding)
        return True


class _Engine:
    def __init__(self, tmp: Path) -> None:
        import threading
        from types import SimpleNamespace

        self.book = _Book()
        self.cancel = threading.Event()
        self._cross = _Cross()
        self.shown: list[dict[str, Any]] = []
        self.tools = SimpleNamespace(
            workspace=SimpleNamespace(pictures=tmp / "pictures", skills=tmp / "skills")
        )

    def _show_finding(self, finding: dict[str, Any]) -> None:
        self.shown.append(finding)


class _Agent:
    def __init__(self, call_id: str) -> None:
        self.call_id = call_id
        self.skip = False
        self.output_format: Any = None
        self.opening: tuple[Any, ...] = ()


def test_ready_draws_the_item_and_sets_its_own_reply_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(slot_look, "_crop", lambda engine: _fake_crop)
    engine, state = _Engine(tmp_path), slot_look.SlotRuns()
    paired, alone = _Agent("slots-p2"), _Agent("slots-p3")
    brief = slot_look._ready(engine, paired, _page_map(), None, 2, state)()
    assert brief is not None and "[ct_thickness]" in brief
    assert paired.output_format is not None and len(paired.opening) >= 3
    assert "slots-p2" in state.items
    assert slot_look._ready(engine, alone, _page_map(), None, 3, state)() is None
    assert alone.skip


def test_apply_roster_records_through_the_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from types import SimpleNamespace

    monkeypatch.setattr(slot_look, "_crop", lambda engine: _fake_crop)
    engine, state = _Engine(tmp_path), slot_look.SlotRuns()
    agent = _Agent("slots-p2")
    slot_look._ready(engine, agent, _page_map(), None, 2, state)()
    marks = state.items["slots-p2"][1]
    thick, arch = marks.mark_of(V, 2, "D1"), marks.mark_of(A, 5, "D7")
    assert thick and arch
    report = json.dumps(_reply(marks, thick, arch))
    slot_look.apply_roster(SimpleNamespace(engine=engine), [(agent, {"report": report})], state)
    assert len(engine.book.reported) == 1 and engine.shown
    assert engine.book.replaced[0]["slot"]["key"] == "ct_thickness"
    assert state.recorded == ["F1"]
    garbled = _Agent("slots-p2")
    slot_look.apply_roster(
        SimpleNamespace(engine=engine), [(garbled, {"report": "not json"})], state
    )
    assert state.unread == ["p2"]
