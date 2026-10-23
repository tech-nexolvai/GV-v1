"""GV's other sink plan rules and vendor drawings with no architect drawing (`sink_offset`, #1906):
made-up plans, no client data.

Each plan is drawn at 2 points to the inch: a run with a back line and a front line, a sink cut-out
30" wide with a faucet hole behind it, the sink cabinet's sides meeting the front line, and ticked
dimensions with one printed label each: the front offset, the back offset, the cut-out's width and
depth, and (when the top's edge is drawn in front of the cabinets) the countertop's depth.
"""

from __future__ import annotations

import json
import math
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import chat_trust, proof, rule_notes
from app.review.assistant import sink_offset as so
from app.review.assistant.simple_check import worklist as worklist_module
from app.review.assistant.vendor_consistency import TEXTS_FILE

PT = 2.0
FILE = "vendor.pdf"
ARCH = "architect.pdf"
BACK_Y = 100.0
LEFT, RIGHT = 70.0, 130.0  # a cut-out 30" wide
FRONT_X, BACK_X, DEPTH_X, TOP_X = 85.0, 115.0, 125.0, 50.0
WIDTH_Y_IN = 6.0  # the width dimension stands this far in front of the run's front line

Line = tuple[tuple[float, float], tuple[float, float]]


def _ticked(a: tuple[float, float], b: tuple[float, float]) -> list[Line]:
    """A dimension line from `a` to `b` with a slanted tick at each end."""
    return [
        (a, b),
        ((a[0] - 2, a[1] + 2), (a[0] + 2, a[1] - 2)),
        ((b[0] - 2, b[1] + 2), (b[0] + 2, b[1] - 2)),
    ]


class Plan:
    """A sink plan: `back` offset, cut-out `depth`, `front` offset (inches); `overhang`: the top's
    edge drawn that far in front of the cabinets' front (None: no top edge drawn)."""

    def __init__(
        self,
        back: float = 2.5,
        depth: float = 17.5,
        front: float = 4.0,
        width: float = 30.0,
        overhang: float | None = None,
    ) -> None:
        self.back_line = BACK_Y
        self.cut_back = BACK_Y + back * PT
        self.cut_front = self.cut_back + depth * PT
        self.front_line = self.cut_front + front * PT
        self.left, self.right = LEFT, LEFT + width * PT
        self.top_edge = None if overhang is None else self.front_line + overhang * PT

    def lines(self, *, dims: tuple[str, ...] = ("front", "back", "width", "depth", "top")) -> list[Line]:
        end = self.top_edge or self.front_line
        out: list[Line] = [
            ((0.0, self.back_line), (240.0, self.back_line)),
            ((0.0, self.front_line), (240.0, self.front_line)),
            ((0.0, self.back_line), (0.0, end)),
            ((240.0, self.back_line), (240.0, end)),
            ((self.left, self.cut_back), (self.right, self.cut_back)),
            ((self.left, self.cut_front), (self.right, self.cut_front)),
            ((self.left, self.cut_back), (self.left, self.cut_front)),
            ((self.right, self.cut_back), (self.right, self.cut_front)),
            # the sink cabinet's sides, from the back line to the cabinets' front
            ((60.0, self.back_line), (60.0, self.front_line)),
            ((self.right + 10, self.back_line), (self.right + 10, self.front_line)),
        ]
        if self.top_edge is not None:
            out.append(((0.0, self.top_edge), (240.0, self.top_edge)))
        # the faucet hole: a small octagon halfway between the back line and the cut-out
        cx, cy, r = 100.0, (self.back_line + self.cut_back) / 2, 1.5
        corners = [
            (cx + r * math.cos(k * math.pi / 4), cy + r * math.sin(k * math.pi / 4))
            for k in range(8)
        ]
        out += [(corners[k], corners[(k + 1) % 8]) for k in range(8)]
        if "front" in dims:
            out += _ticked((FRONT_X, self.cut_front), (FRONT_X, self.front_line))
        if "back" in dims:
            out += _ticked((BACK_X, self.back_line), (BACK_X, self.cut_back))
        if "depth" in dims:
            out += _ticked((DEPTH_X, self.cut_back), (DEPTH_X, self.cut_front))
        if "width" in dims:
            y = self.front_line + WIDTH_Y_IN * PT
            out += _ticked((self.left, y), (self.right, y))
        if "top" in dims and self.top_edge is not None:
            out += _ticked((TOP_X, self.back_line), (TOP_X, self.top_edge))
        return out

    def label(self, which: str, value: str, label_id: str, **extra: Any) -> dict[str, Any]:
        """A plain printed label beside the `which` dimension."""
        if which == "width":
            y = self.front_line + WIDTH_Y_IN * PT
            middle = (self.left + self.right) / 2
            box = [middle - 4, y - 6, middle + 4, y - 1]
            span = self.right - self.left
        else:
            x, start, end = {
                "front": (FRONT_X, self.cut_front, self.front_line),
                "back": (BACK_X, self.back_line, self.cut_back),
                "depth": (DEPTH_X, self.cut_back, self.cut_front),
                "top": (TOP_X, self.back_line, self.top_edge or self.front_line),
            }[which]
            middle = (start + end) / 2
            box = [x + 1, middle - 1.5, x + 6, middle + 1.5]
            span = end - start
        return {
            "id": label_id,
            "text": f'{value}"',
            "inches": str(float(_plain(value))),
            "route": "text",
            "side": "vendor",
            "box": box,
            "span_pt": span,
            "scale_ok": True,
            **extra,
        }


def _plain(value: str) -> Fraction:
    whole, _, rest = value.partition(" ")
    return Fraction(whole) + (Fraction(rest) if rest else 0)


def _drawing(file: str, labels: list[dict[str, Any]], page: int = 7) -> so.Drawing:
    return so.Drawing(file, page, (-10.0, 50.0, 250.0, 220.0), tuple(labels), PT, "SINK BASE")


RULES_MD = """\
| Setting | In force | Where it comes from | Used by |
|---|---|---|---|
| Sink front offset (`front_offset_required`) | 4" | GV's company standard | CT-SINK-OFFSET-FRONT-001 |
| Back offset minimum (`back_offset_minimum`) | 2 1/2" | the rulebook's default | CT-BACK-OFFSET-MIN-001 |
| Clearance (`sink_cutout_clearance`) | 1/4" | the rulebook's default | CT-SINK-CUTOUT-WIDTH-001 |
| Sink inside width (`sink_interior_width`) | 30 1/2" | typed for this drawing set | CT-SINK-CUTOUT-WIDTH-001 |
| Sink inside depth (`sink_interior_depth`) | 18" | typed for this drawing set | CT-SINK-CUTOUT-DEPTH-001 |
| Cabinet depth (`cabinet_depth`) | 24 3/4" | this project's value | CT-DEPTH-001 |
| Overhang (`countertop_overhang`) | 1" | this project's value | CT-DEPTH-001 |
"""


def _settings(text: str = RULES_MD) -> dict[str, so.Setting]:
    return so.settings_from(text)


def _items(
    plan: Plan,
    labels: list[dict[str, Any]],
    rule: so.Rule,
    *,
    alone: bool = True,
    architect: tuple[Plan, list[dict[str, Any]]] | None = None,
    settings: dict[str, so.Setting] | None = None,
) -> list[dict[str, Any]]:
    lines = {FILE: plan.lines()}
    vendor = _drawing(FILE, labels)
    architects: list[tuple[so.Drawing, list[so.SinkPlan] | None]] = []
    if architect is not None:
        theirs_plan, theirs_labels = architect
        lines[ARCH] = theirs_plan.lines(dims=tuple(l["which"] for l in theirs_labels))
        theirs = _drawing(ARCH, [l["label"] for l in theirs_labels], page=1)
        architects.append((theirs, so.drawing_plans(theirs, lambda f, _p: lines[f])))
    plans = so.drawing_plans(vendor, lambda f, _p: lines[f])
    assert plans
    value = (settings or _settings())[rule.id]
    return so.findings_for(vendor, plans, architects, value, "Kitchen", rule=rule, alone=alone)


# ---- the settings in force -------------------------------------------------------------------------


def test_every_rule_reads_its_values_from_rules_md() -> None:
    found = _settings()
    assert found[so.FRONT.id].text == '4"'
    assert found[so.BACK.id].text == '2 1/2"' and so.BACK.required(found[so.BACK.id]) == (
        'at least 2 1/2"'
    )
    # The cut-out: the sink's inside size less the clearance each side.
    assert found[so.WIDTH.id].value == Fraction(30) and found[so.WIDTH.id].text == '30"'
    assert found[so.DEPTH.id].value == Fraction(35, 2)
    assert found[so.WIDTH.id].origin == "drawing set value"
    assert "sink inside width 30 1/2\"" in found[so.WIDTH.id].where
    # The countertop's depth: the cabinet depth plus the overhang.
    assert found[so.TOP.id].value == Fraction(103, 4) and found[so.TOP.id].origin == "project value"


def test_a_job_value_nobody_entered_skips_its_rule_quietly() -> None:
    unset = RULES_MD.replace('| 30 1/2" | typed', "| not set | missing").replace(
        '| 24 3/4" | this', "| not set | this"
    )
    found = _settings(unset)
    assert so.WIDTH.id not in found and so.TOP.id not in found
    assert so.DEPTH.id in found and so.FRONT.id in found and so.BACK.id in found
    assert so.settings_from("") == {} and so.settings_from(None) == {}


# ---- what code reads on the plan ---------------------------------------------------------------


def test_the_plan_gives_every_dimension_with_its_label() -> None:
    plan = Plan(overhang=1.0)
    labels = [
        plan.label("back", "2 1/2", "D1"),
        plan.label("depth", "17 1/2", "D2"),
        plan.label("width", "30", "D3"),
        plan.label("top", "25 3/4", "D4"),
    ]
    (read,) = so.plans_on(labels, plan.lines(), (-10, 50, 250, 220), PT)
    assert [o.id for o in read.back] == ["D1"] and read.back[0].unsure is None
    assert [o.id for o in read.depths] == ["D2"] and read.depths[0].value == Fraction(35, 2)
    assert [o.id for o in read.widths] == ["D3"] and read.widths[0].value == 30
    assert [o.id for o in read.tops] == ["D4"] and read.tops[0].unsure is None
    assert read.size == (30.0, 17.5)


def test_no_top_edge_in_front_of_the_cabinets_means_no_countertop_depth() -> None:
    plan = Plan()
    # Without a top edge the front line may be the cabinets' or the top's: never read as the top.
    labels = [plan.label("top", "24 1/2", "D4")]
    (read,) = so.plans_on(labels, plan.lines(), (-10, 50, 250, 220), PT)
    assert read.tops == ()


# ---- the outcomes on a vendor drawing with no architect drawing --------------------------------


def test_a_back_offset_under_the_minimum_is_red_with_no_architect_drawing() -> None:
    plan = Plan(back=2.0)
    (made,) = _items(plan, [plan.label("back", "2", "D1")], so.BACK)
    assert made["status"] == "different" and made["source"] == "rules"
    assert made["architect"] == 'at least 2 1/2"' and made["vendor"] == '2"'
    assert made["gv_standard"]["rule"] == "CT-BACK-OFFSET-MIN-001"
    assert made["settled"]["method"] == so.METHOD and made["checks"] == {"rule": "agrees"}
    assert so.NO_ARCHITECT in made["why"]
    assert proof.should_be(made) == 'at least 2 1/2" (GV standard: sink back offset minimum)'


def test_a_back_offset_at_or_over_the_minimum_is_right() -> None:
    plan = Plan(back=3.0)
    (made,) = _items(plan, [plan.label("back", "3", "D1")], so.BACK)
    assert made["status"] == "same" and made["why"].startswith(
        'Meets GV standard: sink back offset minimum at least 2 1/2"'
    )


def test_a_front_offset_off_the_standard_is_red_with_no_architect_drawing() -> None:
    plan = Plan(front=3.0)
    (made,) = _items(plan, [plan.label("front", "3", "D1")], so.FRONT)
    assert made["status"] == "different" and made["architect"] == '4"'
    assert so.NO_ARCHITECT in made["why"]
    # With the area's architect drawing paired but code unable to read it: Not compared, as before.
    (paired,) = _items(plan, [plan.label("front", "3", "D1")], so.FRONT, alone=False)
    assert paired["status"] == "check"


def test_the_cut_out_from_the_entered_sink_less_the_clearances() -> None:
    plan = Plan(depth=17.0)
    (wrong,) = _items(plan, [plan.label("depth", "17", "D2")], so.DEPTH)
    assert wrong["status"] == "different" and wrong["architect"] == '17 1/2"'
    assert "sink inside depth 18\"" in wrong["why"] and "2 x clearance 1/4\"" in wrong["why"]
    assert proof.should_be(wrong) == '17 1/2" (drawing set value: sink cut-out depth)'
    plan = Plan()
    (right,) = _items(plan, [plan.label("width", "30", "D3")], so.WIDTH)
    assert right["status"] == "same"
    # The sink's size, not the architect, governs the cut-out: no "no architect drawing" words.
    assert so.NO_ARCHITECT not in right["why"]


def test_the_countertop_depth_is_the_cabinet_depth_plus_the_overhang() -> None:
    # Back line to the top's edge: 2 1/2" + 17 1/2" + 4" + 1" = 25" drawn and printed; GV's
    # values give 24 3/4" + 1" = 25 3/4".
    plan = Plan(overhang=1.0)
    (red,) = _items(plan, [plan.label("top", "25", "D4")], so.TOP)
    assert red["status"] == "different" and red["architect"] == '25 3/4"'
    assert "cabinet depth 24 3/4\"" in red["why"] and "overhang 1\"" in red["why"]
    (right,) = _items(plan, [plan.label("top", "25 3/4", "D4")], so.TOP)
    assert right["status"] == "same"


def test_a_value_read_from_drawn_ink_is_never_red() -> None:
    plan = Plan(back=2.0)
    (made,) = _items(plan, [plan.label("back", "2", "D1", route="drawn")], so.BACK)
    assert made["status"] == "check" and "drawn ink" in made["why"]
    assert "settled" not in made


def test_sinks_of_different_sizes_in_a_set_leave_the_cut_out_not_compared() -> None:
    plan = Plan(depth=17.0)
    vendor = _drawing(FILE, [plan.label("depth", "17", "D2")])
    plans = so.drawing_plans(vendor, lambda _f, _p: plan.lines())
    assert plans
    small = Plan(width=20.0)
    other = so.plans_on([], small.lines(), (-10, 50, 250, 220), PT)
    assert so._sizes_differ([*plans, *other])
    (made,) = so.findings_for(
        vendor,
        plans,
        [],
        _settings()[so.DEPTH.id],
        rule=so.DEPTH,
        alone=True,
        doubt=so.SIZES_DIFFER,
    )
    assert made["status"] == "check" and "different sizes" in made["why"]
    assert not so._sizes_differ([*plans, *plans])


# ---- with the architect's drawing ----------------------------------------------------------------


def test_an_architect_back_offset_under_the_minimum_the_vendor_follows_is_a_rule_note() -> None:
    plan = Plan(back=2.0)
    theirs = Plan(back=2.0)
    made = _items(
        plan,
        [plan.label("back", "2", "D1")],
        so.BACK,
        alone=False,
        architect=(theirs, [{"which": "back", "label": theirs.label("back", "2", "D9")}]),
    )
    (note,) = made
    assert note["status"] == "check" and note["keep_reason"] == so.CONFLICT
    found = rule_notes.rule_note_of(note)
    assert found is not None and found.settings == 'at least 2 1/2"'


def test_a_vendor_back_offset_other_than_the_architects_is_left_to_the_comparison() -> None:
    plan = Plan(back=3.0)
    theirs = Plan(back=3.5)
    made = _items(
        plan,
        [plan.label("back", "3", "D1")],
        so.BACK,
        alone=False,
        architect=(theirs, [{"which": "back", "label": theirs.label("back", "3 1/2", "D9")}]),
    )
    assert made == []


# ---- the set pack: vendor drawings no architect drawing is paired with -----------------------------


def _pack(tmp_path: Path, plan: Plan, labels: list[dict[str, Any]]) -> Path:
    """A set pack with one vendor page (7) whose area has no architect drawing, and an area with
    the architect's drawing over the vendor's (page 2, nothing to read there)."""
    page_map = {
        "version": 5,
        "files": [FILE],
        "pages": [
            {
                "file": FILE,
                "page": 2,
                "role": "architect+vendor",
                "halves": [
                    {"box": [300.0, 50.0, 500.0, 300.0], "role": "architect", "heading": None},
                    {"box": [300.0, 350.0, 500.0, 700.0], "role": "vendor", "heading": None},
                ],
                "section": "1. WALL",
                "area": "1. Bar",
                "sheet_numbers": [],
                "headings": [],
            },
            {
                "file": FILE,
                "page": 7,
                "role": "vendor",
                "halves": [
                    {"box": [-10.0, 50.0, 250.0, 220.0], "role": "vendor", "heading": None, "round": 1}
                ],
                "section": "1. WALL",
                "area": "2. Kitchen",
                "sheet_numbers": [],
                "headings": [],
            },
        ],
    }
    dims = {"version": 1, "files": [FILE], "pages": [{"file": FILE, "page": 7, "labels": labels}]}
    (tmp_path / "dims.json").write_text(json.dumps(dims))
    (tmp_path / TEXTS_FILE).write_text(
        json.dumps(
            {"files": [FILE], "pages": [{"file": FILE, "page": 7, "text": "KITCHEN SINK BASE"}]}
        )
    )
    (tmp_path / "page_map.json").write_text(json.dumps(page_map))
    return tmp_path


@pytest.fixture
def no_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    """The work list from the made-up page map alone (no plan check answers, no reading)."""

    def build(folder: Path, main_file: str | None = None, **_more: Any) -> Any:
        page_map = json.loads((folder / "page_map.json").read_text())
        dims = json.loads((folder / "dims.json").read_text())
        return worklist_module.worklist_from(page_map, dims=dims, main_file=main_file)

    monkeypatch.setattr(worklist_module, "build_worklist", build)


def test_the_work_list_keeps_a_vendor_drawing_with_no_architect_drawing_for_the_rules(
    tmp_path: Path, no_reading: None
) -> None:
    plan = Plan(back=2.0, front=3.0)
    labels = [plan.label("back", "2", "D1"), plan.label("front", "3", "D2")]
    folder = _pack(tmp_path, plan, labels)
    work = worklist_module.build_worklist(folder, FILE)
    assert [item.id for item in work.alone] == ["p7.v1"]
    assert work.alone[0].architect == () and "p7.v1" not in [i.id for i in work.items]
    # Still listed as skipped for the comparison, with why.
    assert any(s.drawing == "p7.v1" for s in work.skipped)


def test_the_pack_check_judges_a_page_with_no_architect_drawing_by_gv_rules(
    tmp_path: Path, no_reading: None
) -> None:
    plan = Plan(back=2.0, front=3.0)
    labels = [
        plan.label("back", "2", "D1"),
        plan.label("front", "3", "D2"),
        plan.label("width", "30", "D3"),
    ]
    folder = _pack(tmp_path, plan, labels)
    found = so.check_pack(
        folder,
        None,
        None,
        FILE,
        "countertop",
        lambda _f, _p: plan.lines(),
        settings=_settings(),
    )
    by_rule = {f["gv_standard"]["rule"]: f for f in found}
    assert by_rule[so.FRONT.id]["status"] == "different"
    assert by_rule[so.BACK.id]["status"] == "different"
    assert by_rule[so.WIDTH.id]["status"] == "same"
    assert all(f["page"] == 7 for f in found)
    # A rule with no value in force is not checked; no value at all: nothing.
    only_front = {so.FRONT.id: _settings()[so.FRONT.id]}
    found = so.check_pack(
        folder, None, None, FILE, "countertop", lambda _f, _p: plan.lines(), settings=only_front
    )
    assert [f["gv_standard"]["rule"] for f in found] == [so.FRONT.id]
    assert so.check_pack(folder, None, None, FILE, "countertop", lambda _f, _p: plan.lines()) == []


def test_a_right_by_a_gv_rule_counts_with_no_architect_page() -> None:
    """A Right of GV's rule has GV's rule on its other side, not a drawing: code proved the
    dimension and read its label, so it is Right on a page with no architect drawing too."""
    plan = Plan(back=3.0)
    (right,) = _items(plan, [plan.label("back", "3", "D1")], so.BACK)
    checked = {**right, "checks": {"printed_vendor": "match"}}
    assert chat_trust.code_judged_by_rule(checked)
    assert chat_trust.not_compared_why(checked, {"pages": []}) is None
    # Code's printed check did not find it there, or no rule proved it: not counted.
    assert not chat_trust.code_judged_by_rule({**right, "checks": {"printed_vendor": "not_found"}})
    unproved = {**checked, "gv_standard": {**right["gv_standard"], "proved": False}}
    assert chat_trust.not_compared_why(unproved, None) is not None
    assert not chat_trust.code_judged_by_rule({**checked, "source": "architect"})
