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
    offset_in: float,
    *,
    dimension: bool = True,
    second_edge_in: float | None = None,
    dashed_in: float | None = None,
    cabinets: bool = True,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """A run's plan with a sink cut-out `offset_in` behind its front line; with `cabinets`, the
    sink cabinet's sides meet the front line on each side of the cut-out (a cabinet plan), else
    only the run's outline is drawn (a top's plan: its front line may be the top's edge)."""
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
    if cabinets:
        lines += [((60.0, BACK_Y), (60.0, FRONT_Y)), ((140.0, BACK_Y), (140.0, FRONT_Y))]
    if second_edge_in is not None:
        at = FRONT_Y + second_edge_in * PT_PER_IN
        lines.append(((0.0, at), (200.0, at)))
    if dashed_in is not None:  # a dashed line `dashed_in` behind the front line (2 pt dashes)
        at = FRONT_Y - dashed_in * PT_PER_IN
        lines += [((x, at), (x + 2.0, at)) for x in range(0, 200, 4)]
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
    architect_extra: list[dict[str, Any]] | None = None,
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
        given = [] if value is None else [_label(value, 4.0, "A5")]
        theirs = _drawing(ARCH, given + (architect_extra or []), page=1)
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


def test_a_dashed_cabinet_face_under_the_top_makes_the_offset_unsure_never_red() -> None:
    """GV's rule measures from the cabinet's front, not the top's edge: a dashed line between the
    cut-out and the front line (a cabinet face under the top) leaves the edge unknown."""
    (plan,) = so.plans_on(
        [_label("5", 5.0)], _plan_lines(5.0, dashed_in=1.0), (-10, 50, 210, 200), PT_PER_IN
    )
    assert plan.unsure is not None and "cabinet face" in plan.unsure
    (offset,) = plan.offsets
    assert offset.unsure is not None
    vendor = _drawing(FILE, [_label("5", 5.0)])
    (made,) = so.findings_for(vendor, [plan], [], STANDARD)
    assert made["status"] == "check" and "settled" not in made


def test_a_top_plan_with_no_cabinet_sides_is_unsure_never_red_nor_right() -> None:
    """GV's rule measures from the cabinet's front: on a top's plan the only front line may be
    the top's edge (a correct 4" plus the overhang reads 5 1/2"), so nothing is decided."""
    lines = _plan_lines(5.5, cabinets=False)
    vendor = _drawing(FILE, [_label("5.5", 5.5)])
    (plan,) = so.drawing_plans(vendor, lambda _f, _p: lines) or []
    assert plan.unsure is not None and "cabinets' front" in plan.unsure
    (made,) = so.findings_for(vendor, [plan], [], STANDARD)
    assert made["status"] == "check" and "settled" not in made
    lines = _plan_lines(4.0, cabinets=False)
    vendor = _drawing(FILE, [_label("4", 4.0)])
    assert (
        so.findings_for(vendor, so.drawing_plans(vendor, lambda _f, _p: lines) or [], [], STANDARD)
        == []
    )


def test_the_architect_printing_the_vendors_value_anywhere_is_not_silent() -> None:
    """An enlarged detail or a dimension placed off to the side may give the architect's offset:
    the vendor's value printed anywhere on the architect's drawing means code cannot call it
    silent, so never red."""
    elsewhere = {**_label("3", 4.0, "A9"), "box": [150.0, 60.0, 158.0, 65.0]}
    (made,) = _findings("3", (None, False), vendor_offset=3.0, architect_extra=[elsewhere])
    assert made["status"] == "check" and "elsewhere on the drawing" in made["why"]
    beside = {**_label("2", 4.0, "A7"), "box": [180.0, 142.0, 186.0, 146.0]}
    (made,) = _findings("3", (None, False), vendor_offset=3.0, architect_extra=[beside])
    assert made["status"] == "check" and "label in front" in made["why"]


def test_an_unreadable_architect_drawing_or_two_sinks_never_turn_red() -> None:
    lines = _plan_lines(3.0)
    vendor = _drawing(FILE, [_label("3", 3.0)])
    plans = so.drawing_plans(vendor, lambda _f, _p: lines) or []
    unread = _drawing(ARCH, [], page=1)
    (made,) = so.findings_for(vendor, plans, [(unread, None)], STANDARD)
    assert made["status"] == "check" and "line work" in made["why"]
    made, _again = so.findings_for(vendor, plans * 2, [], STANDARD)
    assert made["status"] == "check" and "more than one sink" in made["why"]


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
    # #1839: the vendor follows the architect's other value: decided, GV asks the architect.
    (conflict,) = _findings("5", ("5", True), vendor_offset=5.0)
    shown = proof.proof_of(judged({**conflict, "id": "S2"}))
    assert shown["group"] == "rule_note" and shown["should_be"] is None
    assert shown["rule_note"]["question"] == (
        "Architect: the sink front offset is specified at 5\" (Kitchen, architect's page 1); "
        "GV's standard is 4\". Please confirm the size."
    )
    # The vendor differs from the architect's other value too: never decided by GV's rule.
    (other,) = _findings("3", ("5", True), vendor_offset=3.0)
    assert other["keep_reason"] == so.CONFLICT
    shown = proof.proof_of(judged({**other, "id": "S4"}))
    assert shown["group"] == "not_compared" and "reviewer decides" in shown["reason"]
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

    def check_pack(
        folder: Any, uploads: Any, setting: Any, main: Any, category: str, **more: Any
    ) -> Any:
        assert setting == STANDARD and category == "cabinet"
        assert more["settings"] == {so.FRONT.id: STANDARD}
        return [dict(f, category=category) for f in found]

    monkeypatch.setattr(so, "check_pack", check_pack)
    return tmp_path, found


def test_the_hook_replaces_an_agreeing_item_on_a_label_it_decided(
    hooked: tuple[Path, list[dict[str, Any]]],
) -> None:
    hooked[1].extend(_findings("3", (None, False), vendor_offset=3.0))
    engine = _Engine(hooked[0], [{**_other("check"), "architect": ""}])
    assert so.add_to_answer(engine) == {"added": 1, "replaced": 1}
    (recorded,) = engine.book.live()
    assert recorded["status"] == "different" and recorded["settled"]["method"] == so.METHOD
    assert recorded["gv_standard"]["value"] == '4"' and recorded["category"] == "cabinet"
    assert engine.dropped[0][0] == "F1" and "sink front offset" in engine.dropped[0][1]
    assert so.add_to_answer(_Engine(hooked[0], [], mode="chat")) is None


def test_the_hook_never_hides_an_item_that_contradicts_it(
    hooked: tuple[Path, list[dict[str, Any]]],
) -> None:
    """A Right of the architect comparison (vendor 3" = architect 3") is never turned red, and a
    red the comparison found (vendor 4" vs architect 3") is never turned Right: the other item
    speaks and this check adds nothing."""
    hooked[1].extend(_findings("3", (None, False), vendor_offset=3.0))
    right = {**_other("same"), "architect": '3"', "source": "architect"}
    engine = _Engine(hooked[0], [right])
    assert so.add_to_answer(engine) == {"added": 0, "replaced": 0}
    assert engine.book.live() == [right]
    hooked[1].clear()
    hooked[1].extend(_findings("4", (None, False)))
    red = {**_other("different"), "architect": '3"', "source": "architect"}
    engine = _Engine(hooked[0], [red])
    assert so.add_to_answer(engine) == {"added": 0, "replaced": 0}
    assert engine.book.live() == [red]
    consistency = {**_other("different"), "architect": '9"', "source": "vendor_arithmetic"}
    engine = _Engine(hooked[0], [consistency])
    assert so.add_to_answer(engine) == {"added": 0, "replaced": 0}
    # The same red from the comparison (architect 4", the standard): one item, this check's.
    hooked[1].clear()
    hooked[1].extend(_findings("3", (None, False), vendor_offset=3.0))
    same_red = {**_other("different"), "architect": '4"', "source": "architect"}
    engine = _Engine(hooked[0], [same_red])
    assert so.add_to_answer(engine) == {"added": 1, "replaced": 1}


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


def _simple(live: list[dict[str, Any]]) -> Any:
    from app.review.assistant.simple_check import run as run_module

    book = _Book(live)
    dropped: list[str] = []
    engine = SimpleNamespace(book=book, _show_dropped=lambda i, _r: dropped.append(i))
    fake = SimpleNamespace(engine=engine, dropped=dropped)
    fake._label_key = lambda one: run_module._Run._label_key(fake, one)
    return fake


def test_the_simple_check_drops_agreeing_compare_items_on_a_label_it_decided() -> None:
    from app.review.assistant.simple_check import run as run_module

    (red,) = _findings("3", (None, False), vendor_offset=3.0)
    fake = _simple([{**red, "id": "P1", "created_by": so.CREATED_BY}])
    vendor = {"id": "D10", "page": 3, "file": FILE}
    agreeing = SimpleNamespace(vendor=vendor, outcome="candidate", finding={"architect": '4"'})
    other = SimpleNamespace(vendor={"id": "D99", "page": 3, "file": FILE}, outcome="red")
    kept = run_module._Run._without_run_fit(fake, [(agreeing, "a"), (other, "b")])
    assert kept == [(other, "b")] and fake.dropped == []


def test_the_simple_check_keeps_a_contradicting_compare_item_and_drops_codes() -> None:
    """The comparison reads vendor 3" = architect 3" (Right) where code said red from the
    standard: the Right speaks, code's red goes (never a red over the architect's own value)."""
    from app.review.assistant.simple_check import run as run_module

    (red,) = _findings("3", (None, False), vendor_offset=3.0)
    fake = _simple([{**red, "id": "P1", "created_by": so.CREATED_BY}])
    vendor = {"id": "D10", "page": 3, "file": FILE}
    right = SimpleNamespace(vendor=vendor, outcome="right", finding={"architect": '3"'})
    kept = run_module._Run._without_run_fit(fake, [(right, "a")])
    assert kept == [(right, "a")] and fake.dropped == ["P1"]
    assert fake.engine.book.live() == []


def _depth_dimension(x: float) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """A ticked 24" depth dimension drawn inside a top's plan (never a cabinet side)."""
    return [
        ((x, BACK_Y), (x, FRONT_Y)),
        ((x - 2, BACK_Y + 2), (x + 2, BACK_Y - 2)),
        ((x - 2, FRONT_Y + 2), (x + 2, FRONT_Y - 2)),
    ]


def test_depth_dimensions_on_a_top_plan_are_not_cabinet_sides() -> None:
    lines = _plan_lines(3.0, cabinets=False) + _depth_dimension(50.0) + _depth_dimension(150.0)
    vendor = _drawing(FILE, [_label("3", 3.0)])
    (plan,) = so.drawing_plans(vendor, lambda _f, _p: lines) or []
    assert plan.unsure is not None and "cabinets' front" in plan.unsure
    (made,) = so.findings_for(vendor, [plan], [], STANDARD)
    assert made["status"] == "check"


def test_the_vendors_value_in_the_architects_page_words_is_not_silent() -> None:
    lines = {FILE: _plan_lines(3.0), ARCH: _plan_lines(4.0, dimension=False)}
    vendor = _drawing(FILE, [_label("3", 3.0)])
    noted = so.Drawing(ARCH, 1, (-10.0, 50.0, 210.0, 200.0), (), PT_PER_IN, 'SINK 3" FROM CAB')
    plans = so.drawing_plans(vendor, lambda f, _p: lines[f]) or []
    architects = [(noted, so.drawing_plans(noted, lambda f, _p: lines[f]))]
    (made,) = so.findings_for(vendor, plans, architects, STANDARD)
    assert made["status"] == "check" and "page words" in made["why"]


def test_other_references_and_unreadable_architect_words_are_never_hidden() -> None:
    (right,) = _findings("4", (None, False))
    assert not so.agrees(right, "check", "24", "brand_prototype")
    assert not so.agrees(right, "check", "see detail", "architect")
    assert so.agrees(right, "check", "", "architect")
    assert so.agrees(right, "same", '4"', "architect")
    assert not so.agrees(right, "different", '4"', "architect")


def test_the_simple_check_judges_every_compare_item_on_a_label_first() -> None:
    """A compare item agreeing with code's red and another saying Right on the same label: the
    Right contradicts it, so both compare items stay and code's red goes (never one red lost)."""
    from app.review.assistant.simple_check import run as run_module

    (red,) = _findings("3", (None, False), vendor_offset=3.0)
    fake = _simple([{**red, "id": "P1", "created_by": so.CREATED_BY}])
    vendor = {"id": "D10", "page": 3, "file": FILE}
    agreeing = SimpleNamespace(vendor=vendor, outcome="red", finding={"architect": '4"'})
    right = SimpleNamespace(vendor=vendor, outcome="right", finding={"architect": '3"'})
    kept = run_module._Run._without_run_fit(fake, [(agreeing, "a"), (right, "b")])
    assert kept == [(agreeing, "a"), (right, "b")] and fake.dropped == ["P1"]


def test_the_simple_check_keeps_the_note_when_the_comparisons_item_is_not_kept() -> None:
    """On a Cabinet-only pick the compare item may be out of scope (`_add` gives None): the
    sink's Not-compared note then stays, so the label never ends up with nothing."""
    from app.review.assistant.simple_check import run as run_module

    (note,) = _findings("3", None, vendor_offset=3.0)
    fake = _simple([{**note, "id": "N1", "created_by": so.CREATED_BY}])
    fake.out_of_scope, fake.not_listed, fake.rows, fake.unpaired = 0, 0, [], []
    fake.uncovered = []
    vendor = {"id": "D10", "page": 3, "file": FILE}
    one = SimpleNamespace(vendor=vendor, outcome="red", finding={"architect": '5"'})
    fake._add = lambda _one: None
    run_module._Run._record(fake, [(one, SimpleNamespace(id="I1"))])
    assert [f["id"] for f in fake.engine.book.live()] == ["N1"] and fake.out_of_scope == 1
    fake._add = lambda _one: True
    run_module._Run._record(fake, [(one, SimpleNamespace(id="I1"))])
    assert fake.engine.book.live() == [] and fake.dropped == ["N1"]


def test_the_stroke_grid_gives_the_same_ticks_as_reading_every_stroke() -> None:
    """Speed (2026-10-23): a frame's short slanted strokes are kept in a grid (`_Ticks`), and every
    question about a tick gets exactly the answer of reading every stroke: ticks crossing a line,
    strokes ending at a point, long strokes, a cell's edge and a point with no number."""
    import random

    chance = random.Random(1906)
    strokes = []
    for _ in range(2500):
        ax, ay = chance.uniform(-30, 500), chance.uniform(-30, 500)
        length = chance.choice([0.3, 1.0, 2.5, 6.0, 11.9, 12.0, 12.4, 30.0])
        angle = chance.uniform(0.2, 1.3) * chance.choice([1, -1, 2, -2])
        strokes.append(((ax, ay), (ax + length * math.cos(angle), ay + length * math.sin(angle))))
    strokes += [
        ((8.0, 8.0), (10.0, 6.0)),
        ((15.9, 0.0), (16.1, 2.0)),
        ((float("nan"), 1.0), (2.0, 2.0)),
    ]
    with_grid = so._Frame.of(strokes, "bottom")
    plain = so._Frame(
        with_grid.across, with_grid.places, with_grid.along, with_grid.slanted, with_grid.all
    )
    assert plain == with_grid and plain.ticks is None and with_grid.ticks is not None
    points = [(chance.uniform(-40, 520), chance.uniform(-40, 520)) for _ in range(3000)]
    points += [((a[0] + b[0]) / 2 + 0.5, (a[1] + b[1]) / 2) for a, b in with_grid.slanted[:400]]
    points += [(b[0] + 0.3, b[1] - 0.2) for _a, b in with_grid.slanted[:400]]
    points += [(9.0, 7.0), (16.0, 1.0), (float("nan"), 2.0)]
    for u, v in points:
        assert so._crossed(with_grid, u, v) == so._crossed(plain, u, v), (u, v)
        assert so._ticked(with_grid, (u, v)) == so._ticked(plain, (u, v)), (u, v)
    assert any(so._ticked(with_grid, p) for p in points) and any(
        so._crossed(with_grid, u, v) for u, v in points
    )
