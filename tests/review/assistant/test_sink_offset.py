"""GV's sink front offset in the full check (`sink_offset`, #1807): made-up plans, no client data.

Each plan is drawn at 2 points to the inch: a run 24" deep (back line, front line), a 30" x 16"
sink cut-out with a faucet hole behind it, and a ticked dimension from the cut-out's front edge to
the run's front line with one printed label.
"""

from __future__ import annotations

import math
import threading
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.review.assistant import escalation, proof
from app.review.assistant import sink_offset as so
from app.review.assistant.chat_trust import judged
from app.review.assistant.decision_resolver import ResolverContext, resolve, settings_in_force
from app.review.assistant.product_scope import (
    CODE_MADE,
    ProductScope,
    refiled,
    sink_cutout_category,
)

PT_PER_IN = 2.0
FILE = "vendor.pdf"
ARCH = "architect.pdf"
BACK_Y = 100.0
FRONT_Y = BACK_Y + 24 * PT_PER_IN  # a run 24" deep
LEFT, RIGHT = 70.0, 130.0  # a cut-out 30" wide
DIM_X = 85.0
STANDARD = so.Setting(Fraction(4), '4"', "GV standard", "GV's company standard")
RULES_MD = """\
| Setting | In force | Where it comes from | Used by |
|---|---|---|---|
| Sink front offset (`front_offset_required`) | 4" | GV's company standard | CT-SINK-OFFSET-FRONT-001 |
"""


def _plan_lines(
    offset_in: float, *, dimension: bool = True, second_edge_in: float | None = None
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """A run's plan with a sink cut-out `offset_in` behind its front line."""
    edge = FRONT_Y - offset_in * PT_PER_IN
    top = edge - 16 * PT_PER_IN
    lines = [
        ((0.0, BACK_Y), (200.0, BACK_Y)),  # the run's back line
        ((0.0, FRONT_Y), (200.0, FRONT_Y)),  # the run's front line
        ((0.0, BACK_Y), (0.0, FRONT_Y)),
        ((200.0, BACK_Y), (200.0, FRONT_Y)),
        ((LEFT, top), (RIGHT, top)),  # the cut-out
        ((LEFT, edge), (RIGHT, edge)),
        ((LEFT, top), (LEFT, edge)),
        ((RIGHT, top), (RIGHT, edge)),
    ]
    # The faucet hole: a small octagon behind the cut-out, 1 1/2" across.
    cx, cy, r = 100.0, (BACK_Y + top) / 2, 1.5
    corners = [
        (cx + r * math.cos(k * math.pi / 4), cy + r * math.sin(k * math.pi / 4)) for k in range(8)
    ]
    lines += [(corners[k], corners[(k + 1) % 8]) for k in range(8)]
    if dimension:
        lines += [
            ((DIM_X, edge), (DIM_X, FRONT_Y)),
            ((DIM_X - 2, edge + 2), (DIM_X + 2, edge - 2)),  # ticks at both ends
            ((DIM_X - 2, FRONT_Y + 2), (DIM_X + 2, FRONT_Y - 2)),
        ]
    if second_edge_in is not None:
        at = FRONT_Y + second_edge_in * PT_PER_IN
        lines.append(((0.0, at), (200.0, at)))
    return lines


def _label(value: str, offset_in: float, label_id: str = "D10", **extra: Any) -> dict[str, Any]:
    edge = FRONT_Y - offset_in * PT_PER_IN
    middle = (edge + FRONT_Y) / 2
    return {
        "id": label_id,
        "text": f'{value}"',
        "inches": value,
        "route": "text",
        "box": [DIM_X + 2, middle - 2.5, DIM_X + 8, middle + 2.5],
        **extra,
    }


def _drawing(file: str, labels: list[dict[str, Any]], page: int = 3) -> so.Drawing:
    return so.Drawing(
        file, page, (-10.0, 50.0, 210.0, 200.0), tuple(labels), PT_PER_IN, "SINK BASE"
    )


def _findings(
    vendor_value: str | None,
    architect: tuple[str | None, bool] | None,
    setting: so.Setting = STANDARD,
    *,
    vendor_offset: float = 4.0,
    second_edge_in: float | None = None,
) -> list[dict[str, Any]]:
    """The vendor's front offset items: the vendor prints `vendor_value` on a cut-out drawn
    `vendor_offset` inches back; the architect (`(value, dimension drawn)`, or `None` for no
    architect drawing code can read) prints its value, or nothing."""
    lines = {FILE: _plan_lines(vendor_offset, second_edge_in=second_edge_in)}
    vendor_labels = [] if vendor_value is None else [_label(vendor_value, vendor_offset)]
    vendor = _drawing(FILE, vendor_labels)
    architects: list[tuple[so.Drawing, list[so.SinkPlan] | None]] = []
    if architect is not None:
        value, drawn = architect
        lines[ARCH] = _plan_lines(4.0, dimension=drawn)
        theirs = _drawing(ARCH, [] if value is None else [_label(value, 4.0, "A5")], page=1)
        architects.append((theirs, so.drawing_plans(theirs, lambda f, _p: lines[f])))
    plans = so.drawing_plans(vendor, lambda f, _p: lines[f])
    assert plans is not None
    return so.findings_for(vendor, plans, architects, setting, "Kitchen")


# ---- what code proves in the plan ----------------------------------------------------------------


def test_the_cut_out_its_front_and_the_offset_are_proved_from_the_drawing() -> None:
    (plan,) = so.plans_on([_label("4", 4.0)], _plan_lines(4.0), (-10, 50, 210, 200), PT_PER_IN)
    assert plan.front == "bottom" and plan.unsure is None
    (offset,) = plan.offsets
    assert offset.value == 4 and offset.unsure is None


def test_no_faucet_hole_means_no_front_and_nothing_is_claimed() -> None:
    lines = [line for line in _plan_lines(4.0) if math.dist(*line) > 2]  # the hole's sides go
    assert so.plans_on([_label("4", 4.0)], lines, (-10, 50, 210, 200), PT_PER_IN) == []


def test_a_page_that_names_no_sink_is_not_read() -> None:
    drawing = so.Drawing(FILE, 3, (-10, 50, 210, 200), (_label("4", 4.0),), PT_PER_IN, "BASE")
    assert so.drawing_plans(drawing, lambda _f, _p: _plan_lines(4.0)) == []
    assert so.drawing_plans(drawing, lambda _f, _p: None) is None


# ---- the outcomes --------------------------------------------------------------------------------


def test_architect_silent_and_vendor_three_inches_is_red_from_the_gv_standard() -> None:
    (made,) = _findings("3", (None, False), vendor_offset=3.0)
    assert made["status"] == "different" and made["source"] == "rules"
    assert made["architect"] == '4"' and made["vendor"] == '3"'
    assert made["why"].startswith('Vendor: 3" · Should be: 4" (GV standard: sink front offset)')
    assert made["settled"]["method"] == so.METHOD and made["checks"] == {"rule": "agrees"}
    assert proof.should_be(made) == '4" (GV standard: sink front offset)'
    assert made["gv_standard"]["rule"] == so.RULE_ID and made["category"] == "countertop"


def test_a_project_value_of_three_inches_makes_the_vendors_three_inches_right() -> None:
    project = so.Setting(Fraction(3), '3"', "project value", "this project's value")
    (made,) = _findings("3", (None, False), project, vendor_offset=3.0)
    assert made["status"] == "same" and 'project value: sink front offset 3"' in made["why"]
    wrong = _findings("4", (None, False), project)
    assert wrong[0]["status"] == "different"
    assert proof.should_be(wrong[0]) == '3" (project value: sink front offset)'


def test_vendor_matching_the_standard_with_the_architect_silent_is_right() -> None:
    (made,) = _findings("4", (None, False))
    assert made["status"] == "same" and made["why"].startswith(
        'Matches GV standard: sink front offset 4"'
    )
    assert "settled" not in made


def test_architect_printing_the_standard_gives_one_item_the_architect_governs() -> None:
    (right,) = _findings("4", ("4", True))
    assert right["status"] == "same" and right["source"] == "architect"
    assert right["paired_by"] == "code" and right["dims"]["architect"]["id"] == "A5"
    (wrong,) = _findings("3", ("4", True), vendor_offset=3.0)
    assert wrong["status"] == "different" and wrong["source"] == "architect"
    assert proof.governs(wrong).endswith("GV standard: sink front offset agrees")


def test_architect_printing_another_value_is_never_red_the_reviewer_decides() -> None:
    (follows,) = _findings("5", ("5", True), vendor_offset=5.0)
    assert follows["status"] == "check" and follows["keep_reason"] == so.CONFLICT
    assert 'architect 5" vs GV standard 4"' in follows["gv_note"]
    assert "reviewer decides" in follows["gv_note"]
    assert "settled" not in follows and "agrees" not in proof.governs(follows)
    assert so.decided_labels([follows]) == {(FILE, 3, "D10")}
    # The vendor differs from the architect too: noted, but the architect comparison speaks.
    (differs,) = _findings("4", ("5", True))
    assert differs["status"] == "check" and differs["keep_reason"] == so.CONFLICT
    assert so.decided_labels([differs]) == set()


def test_a_second_front_edge_makes_the_offset_unsure_never_red() -> None:
    (made,) = _findings("3", (None, False), vendor_offset=3.0, second_edge_in=1.0)
    assert made["status"] == "check" and "settled" not in made
    assert "second front edge" in made["why"]
    assert so.decided_labels([made]) == set()
    assert so.noted_labels([{**made, "id": "S1"}]) == {(FILE, 3, "D10"): "S1"}
    # Equal to the standard while unsure: nothing to say.
    assert _findings("4", (None, False), second_edge_in=1.0) == []


def test_an_architect_code_cannot_read_never_turns_a_difference_red() -> None:
    (made,) = _findings("3", None, vendor_offset=3.0)
    assert made["status"] == "check" and "settled" not in made
    assert "no sink plan on the architect's drawings" in made["why"]
    assert _findings("4", None) == []  # equal: left to the architect comparison


def test_a_value_read_from_drawn_ink_is_never_red() -> None:
    lines = _plan_lines(3.0)
    vendor = _drawing(FILE, [_label("3", 3.0, route="ink")])
    (plan,) = so.drawing_plans(vendor, lambda _f, _p: lines) or []
    (made,) = so.findings_for(vendor, [plan], [], STANDARD)
    assert made["status"] == "check" and "drawn ink" in made["why"]


def test_a_label_far_from_the_drawn_length_is_unsure() -> None:
    (made,) = _findings("9", (None, False), vendor_offset=3.0)
    assert made["status"] == "check" and "far from the drawn length" in made["why"]


# ---- the setting in force and the product scope --------------------------------------------------


def test_the_setting_and_where_it_comes_from_are_read_from_rules_md() -> None:
    assert so.setting_from(RULES_MD) == STANDARD
    project = RULES_MD.replace("GV's company standard", "this project's value (set by A)")
    assert so.setting_from(project.replace('| 4" |', '| 3" |')) == so.Setting(
        Fraction(3), '3"', "project value", "this project's value (set by A)"
    )
    typed = RULES_MD.replace("GV's company standard", "typed for this drawing set")
    assert so.setting_from(typed).origin == "drawing set value"  # type: ignore[union-attr]
    assert so.setting_from(RULES_MD.replace('| 4" |', "| not set |")) is None
    assert so.setting_from("") is None and so.setting_from(None) is None


def test_a_cabinet_only_check_files_the_item_under_cabinets() -> None:
    assert so.CREATED_BY in CODE_MADE
    assert sink_cutout_category(None) == "countertop"
    assert sink_cutout_category(ProductScope.of(["countertop"])) == "countertop"
    assert sink_cutout_category(ProductScope.of(["cabinet"])) == "cabinet"
    assert sink_cutout_category(ProductScope.of(["countertop", "cabinet"])) == "countertop"
    (made,) = _findings("3", (None, False), vendor_offset=3.0)
    assert refiled({**made, "category": "cabinet"}, ProductScope.of(["cabinet"])) == "cabinet"


def test_the_resolver_and_the_escalation_leave_its_items_alone() -> None:
    (note,) = _findings("3", None, vendor_offset=3.0)
    (conflict,) = _findings("5", ("5", True), vendor_offset=5.0)
    for item in ({**note, "id": "S1"}, {**conflict, "id": "S2"}):
        result = resolve([item], ResolverContext(settings=settings_in_force(RULES_MD)))
        assert result.changed == [] and result.absorbed == {}
        assert not escalation.undecided(item)


def test_every_output_names_the_standard_and_a_not_compared_item_its_reason() -> None:
    (red,) = _findings("3", (None, False), vendor_offset=3.0)
    shown = proof.proof_of(judged({**red, "id": "S1"}))
    assert shown["group"] == "clouded"
    assert shown["should_be"] == '4" (GV standard: sink front offset)'
    assert shown["proof"].startswith('Settled by GV Review: GV standard: sink front offset 4"')
    (conflict,) = _findings("5", ("5", True), vendor_offset=5.0)
    shown = proof.proof_of(judged({**conflict, "id": "S2"}))
    assert shown["group"] == "not_compared" and shown["should_be"] is None
    assert "reviewer decides" in shown["reason"]
    (note,) = _findings("3", None, vendor_offset=3.0)
    assert proof.proof_of(judged({**note, "id": "S3"}))["group"] == "not_compared"


# ---- the hook ------------------------------------------------------------------------------------


class _Book:
    main_file = FILE

    def __init__(self, live: list[dict[str, Any]]) -> None:
        self._live = {str(f["id"]): f for f in live}
        self.added: list[dict[str, Any]] = []

    def live(self) -> list[dict[str, Any]]:
        return list(self._live.values())

    def add_code_finding(self, given: dict[str, Any], *, created_by: str) -> dict[str, Any]:
        kept = {k: v for k, v in given.items() if k not in so._EXTRA + ("checks",)}
        made = {**kept, "id": f"P{len(self.added) + 1}", "created_by": created_by, "checks": {}}
        self.added.append(made)
        self._live[made["id"]] = made
        return dict(made)

    def replace(self, finding: dict[str, Any]) -> bool:
        self._live[str(finding["id"])] = finding
        return True

    def absorb(self, finding_id: str) -> bool:
        return self._live.pop(finding_id, None) is not None


class _Engine:
    def __init__(self, folder: Path, live: list[dict[str, Any]], mode: str = "full_check") -> None:
        self.settings = SimpleNamespace(mode=mode)
        workspace = SimpleNamespace(set_pack=folder, skills=folder, uploads=folder)
        self.tools = SimpleNamespace(workspace=workspace)
        self.book = _Book(live)
        self.product_scope = ProductScope.of(["cabinet"])
        self.finding_lock = threading.RLock()
        self.shown: list[dict[str, Any]] = []
        self.dropped: list[tuple[str, str]] = []

    def _show_finding(self, finding: dict[str, Any]) -> None:
        self.shown.append(finding)

    def _show_dropped(self, finding_id: str, reason: str) -> None:
        self.dropped.append((finding_id, reason))


def _other(status: str, label: str = "D10") -> dict[str, Any]:
    return {
        "id": "F1",
        "page": 3,
        "status": status,
        "dims": {"vendor": {"id": label, "page": 3, "file": FILE}},
    }


@pytest.fixture
def hooked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[dict[str, Any]]]:
    """A chat folder with `rules.md`, and the items the set pack's check gives (filled by a test)."""
    (tmp_path / "rules.md").write_text(RULES_MD)
    found: list[dict[str, Any]] = []

    def check_pack(folder: Any, uploads: Any, setting: Any, main: Any, category: str) -> Any:
        assert setting == STANDARD and category == "cabinet"
        return [dict(f, category=category) for f in found]

    monkeypatch.setattr(so, "check_pack", check_pack)
    return tmp_path, found


def test_the_hook_replaces_the_answers_item_on_a_label_it_decided(
    hooked: tuple[Path, list[dict[str, Any]]],
) -> None:
    hooked[1].extend(_findings("3", (None, False), vendor_offset=3.0))
    engine = _Engine(hooked[0], [_other("same")])
    assert so.add_to_answer(engine) == {"added": 1, "replaced": 1}
    (recorded,) = engine.book.live()
    assert recorded["status"] == "different" and recorded["settled"]["method"] == so.METHOD
    assert recorded["gv_standard"]["value"] == '4"' and recorded["category"] == "cabinet"
    assert engine.dropped[0][0] == "F1" and "sink front offset" in engine.dropped[0][1]
    assert so.add_to_answer(_Engine(hooked[0], [], mode="chat")) is None


def test_the_hook_adds_an_unsure_note_only_where_the_answer_has_none(
    hooked: tuple[Path, list[dict[str, Any]]],
) -> None:
    hooked[1].extend(_findings("3", None, vendor_offset=3.0))
    engine = _Engine(hooked[0], [_other("different")])
    assert so.add_to_answer(engine) == {"added": 0, "replaced": 0}
    assert [f["id"] for f in engine.book.live()] == ["F1"]
    alone = _Engine(hooked[0], [])
    assert so.add_to_answer(alone) == {"added": 1, "replaced": 0}
    assert alone.book.live()[0]["status"] == "check"


def test_the_hook_leaves_a_label_the_reviewer_decided(
    hooked: tuple[Path, list[dict[str, Any]]],
) -> None:
    hooked[1].extend(_findings("3", (None, False), vendor_offset=3.0))
    decided = {**_other("same"), "decision": {"decision": "agree"}}
    engine = _Engine(hooked[0], [decided])
    assert so.add_to_answer(engine) == {"added": 0, "replaced": 0}


def test_no_setting_in_rules_md_means_no_check(tmp_path: Path) -> None:
    (tmp_path / "rules.md").write_text("no settings\n")
    assert so.add_to_answer(_Engine(tmp_path, [])) is None


def test_the_simple_check_drops_compare_items_on_a_label_it_decided() -> None:
    from app.review.assistant.simple_check import run as run_module

    (red,) = _findings("3", (None, False), vendor_offset=3.0)
    book = _Book([{**red, "id": "P1", "created_by": so.CREATED_BY}])
    fake = SimpleNamespace(engine=SimpleNamespace(book=book))
    fake._label_key = lambda one: run_module._Run._label_key(fake, one)
    decided = SimpleNamespace(vendor={"id": "D10", "page": 3, "file": FILE})
    other = SimpleNamespace(vendor={"id": "D99", "page": 3, "file": FILE})
    kept = run_module._Run._without_run_fit(fake, [(decided, "a"), (other, "b")])
    assert kept == [(other, "b")]
