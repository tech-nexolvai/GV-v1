"""The sink cut-out on two sheets of the same top (`sink_sheets`, Fix D): made-up plans, no client
data.

Each sheet draws one top in plan at 4 points to the inch: the top's outline, a sink cut-out with a
faucet hole behind it, and ticked dimension lines with one printed label each (the top's width and
depth, the cut-out's width and depth, its front offset to the top's front edge).
"""

from __future__ import annotations

import math
import random
import threading
from fractions import Fraction
from types import SimpleNamespace
from typing import Any

import pytest

from app.review.assistant import job_value_rules, likely_problems, proof
from app.review.assistant import sink_offset as so
from app.review.assistant import sink_sheets as ss
from app.review.assistant.chat_trust import judged
from app.review.assistant.ledger import format_inches
from app.review.assistant.product_scope import ProductScope

PPI = 4.0
FILE = "vendor.pdf"
X0, Y0 = 100.0, 100.0
STANDARD = so.Setting(Fraction(4), '4"', "GV standard", "GV's company standard")
Segment = tuple[tuple[float, float], tuple[float, float]]


def _tick(x: float, y: float) -> Segment:
    return ((x - 2.0, y + 2.0), (x + 2.0, y - 2.0))


def _octagon(cx: float, cy: float, r: float) -> list[Segment]:
    corners = [
        (cx + r * math.cos(k * math.pi / 4), cy + r * math.sin(k * math.pi / 4)) for k in range(8)
    ]
    return [(corners[k], corners[(k + 1) % 8]) for k in range(8)]


class _Sheet:
    """One made-up sheet: line work and labels, built a dimension at a time."""

    def __init__(self, page: int) -> None:
        self.page = page
        self.lines: list[Segment] = []
        self.labels: list[dict[str, Any]] = []

    def label(self, text: str, inches: str, box: list[float], **extra: Any) -> None:
        self.labels.append(
            {
                "id": f"D{len(self.labels) + 1}",
                "text": text,
                "inches": inches,
                "box": box,
                "side": "vendor",
                "route": "text",
                **extra,
            }
        )

    def across(self, y: float, a: float, b: float, text: str, inches: str, **extra: Any) -> None:
        """A dimension along the front (left to right) at height `y`, its label over its middle."""
        self.lines += [((a, y), (b, y)), _tick(a, y), _tick(b, y)]
        middle = (a + b) / 2
        self.label(text, inches, [middle - 8, y - 9, middle + 8, y - 1], **extra)

    def deep(self, x: float, a: float, b: float, text: str, inches: str, **extra: Any) -> None:
        """A dimension front to back at `x`, its label standing across the line in its middle."""
        self.lines += [((x, a), (x, b)), _tick(x, a), _tick(x, b)]
        middle = (a + b) / 2
        self.label(text, inches, [x - 9, middle - 4, x + 9, middle + 4], **extra)


def _plan(
    page: int = 12,
    *,
    width: float = 54,
    depth: float = 23,
    cut: tuple[float, float] = (17, 12),
    front: float = 4,
    texts: dict[str, tuple[str, str]] | None = None,
    hole: bool = True,
    drain: bool = False,
    cabinet_face: bool = False,
    centre: float | None = None,
) -> _Sheet:
    """A top's plan: `width` x `depth` inches, the cut-out `cut` (width, depth) `front` inches from
    the top's front edge, centred (or `centre` inches from the left end), a faucet hole 2 1/2"
    behind it; `texts` overrides a dimension's printed words (by its name)."""
    words = {
        "top_width": (f'{width:g}"', f"{width:g}"),
        "top_depth": (f'{depth:g}"', f"{depth:g}"),
        "cutout_width": (f'{cut[0]:g}"', f"{cut[0]:g}"),
        "cutout_depth": (f'{cut[1]:g}"', f"{cut[1]:g}"),
        "front_offset": (f'{front:g}"', f"{front:g}"),
        **(texts or {}),
    }
    sheet = _Sheet(page)
    x1, y1 = X0 + width * PPI, Y0 + depth * PPI
    sheet.lines += [
        ((X0, Y0), (x1, Y0)),
        ((X0, y1), (x1, y1)),
        ((X0, Y0), (X0, y1)),
        ((x1, Y0), (x1, y1)),
    ]
    mid = X0 + (centre if centre is not None else width / 2) * PPI
    cl, cr = mid - cut[0] * PPI / 2, mid + cut[0] * PPI / 2
    cf = y1 - front * PPI
    cb = cf - cut[1] * PPI
    sheet.lines += [
        ((cl, cb), (cr, cb)),
        ((cl, cf), (cr, cf)),
        ((cl, cb), (cl, cf)),
        ((cr, cb), (cr, cf)),
    ]
    if hole:
        sheet.lines += _octagon(mid, cb - 2.5 * PPI, 0.7 * PPI)
    if drain:
        sheet.lines += _octagon(mid, (cb + cf) / 2, 0.8 * PPI)
    if cabinet_face:  # the cabinets' front 1" in front of the top's edge: two outlines
        sheet.lines.append(((X0, y1 + 1 * PPI), (x1, y1 + 1 * PPI)))
    sheet.across(Y0 - 30, X0, x1, *words["top_width"])
    sheet.across(Y0 - 12, cl, cr, *words["cutout_width"])
    sheet.deep(X0 - 30, Y0, y1, *words["top_depth"])
    sheet.deep(X0 - 12, cb, cf, *words["cutout_depth"])
    sheet.deep(X0 - 12, cf, y1, *words["front_offset"])
    return sheet


def _views(sheet: _Sheet, words: str = "SINK CUT OUT") -> list[ss.SinkView]:
    return ss.read_drawing(FILE, sheet.page, (0, 0, 2000, 2000), sheet.labels, sheet.lines, words)


def _top(sheet: _Sheet, **facts: Any) -> ss.SheetTop:
    (top,) = ss._tops_of(_views(sheet), file=FILE, page=sheet.page, **facts)
    return top


def _values(top: ss.SheetTop) -> dict[str, str]:
    return {name: m.text for name, m in top.values.items() if m.unsure is None}


# ---- what code reads on one sheet ---------------------------------------------------------------


def test_the_cut_out_the_top_and_each_dimension_are_named_by_their_two_ends() -> None:
    top = _top(_plan())
    assert _values(top) == {
        "top_width": '54"',
        "top_depth": '23"',
        "cutout_width": '17"',
        "cutout_depth": '12"',
        "front_offset": '4"',
    }
    (view,) = top.views
    assert view.front == "bottom" and view.top is not None and view.scale == pytest.approx(PPI)
    assert top.size() == (Fraction(54), Fraction(23))


def test_millimetres_with_the_inches_are_read_as_the_inches() -> None:
    texts = {
        "front_offset": ('3 5/8" [92mm]', "3.625"),
        "cutout_width": ("445 [17 1/2]", "17.5"),
    }
    top = _top(_plan(front=3.625, cut=(17.5, 12), texts=texts))
    assert top.values["front_offset"].value == Fraction(29, 8)
    assert top.values["cutout_width"].value == Fraction(35, 2)
    assert top.values["front_offset"].unsure is None


def test_a_plan_turned_upside_down_reads_the_same() -> None:
    sheet = _plan()
    flipped = _Sheet(sheet.page)
    flipped.lines = [((-a[0], -a[1]), (-b[0], -b[1])) for a, b in sheet.lines]
    for label in sheet.labels:
        x0, y0, x1, y1 = label["box"]
        flipped.labels.append({**label, "box": [-x1, -y1, -x0, -y0]})
    (view,) = ss.read_drawing(FILE, 12, (-2000, -2000, 0, 0), flipped.labels, flipped.lines, "SINK")
    assert view.front == "top"
    assert {m.feature: m.text for m in view.measures} == _values(_top(_plan()))


def test_no_faucet_hole_no_sink_words_or_a_drain_inside_mean_no_plan() -> None:
    assert _views(_plan(hole=False)) == []
    assert _views(_plan(), words="CABINET ELEVATION") == []
    # A sink drawn in plan (its drain inside the outline) is not the cut-out.
    assert _views(_plan(drain=True)) == []


def test_a_second_edge_outside_the_outline_leaves_the_top_unproved() -> None:
    """The cabinets' front drawn in front of the top's edge: which outline is the top's is open, so
    only the cut-out's own sizes are read (never an offset to an edge)."""
    top = _top(_plan(cabinet_face=True))
    assert set(_values(top)) == {"cutout_width", "cutout_depth"}
    assert top.size() is None


def test_a_row_of_chained_dimensions_is_never_read_as_one_line() -> None:
    sheet = _plan()
    # The top's width printed again as three chained parts on one line (left, cut-out, right).
    cl, cr = X0 + 18.5 * PPI, X0 + 35.5 * PPI
    y = Y0 - 50
    for a, b, text in ((X0, cl, '18 1/2"'), (cl, cr, '17"'), (cr, X0 + 54 * PPI, '18 1/2"')):
        sheet.across(y, a, b, text, text.rstrip('"').replace(" 1/2", ".5"))
    top = _top(sheet)
    assert top.values["top_width"].text == '54"'  # the overall's own line, not the row
    assert top.values["left_to_cutout"].text == '18 1/2"'
    assert "top_width" not in top.conflicts


def test_a_label_far_from_its_drawn_length_is_dropped_and_a_little_off_is_unsure() -> None:
    far = _top(_plan(texts={"front_offset": ('9"', "9")}))
    assert "front_offset" not in far.values
    near = _top(_plan(texts={"front_offset": ('4 1/2"', "4.5")}))
    assert "drawn 4" in str(near.values["front_offset"].unsure)


def test_words_beside_the_number_or_drawn_ink_are_not_sure() -> None:
    words = _top(_plan(texts={"front_offset": ('4" TYP', "4")}))
    assert words.values["front_offset"].unsure == "the label holds words beside the number"
    sheet = _plan()
    sheet.labels[-1]["route"] = "drawn"
    assert "drawn ink" in str(_top(sheet).values["front_offset"].unsure)


# ---- two sheets of the same top -----------------------------------------------------------------


def _pair(first: _Sheet, second: _Sheet, **facts: Any) -> list[dict[str, Any]]:
    one = _top(first, **{"tags": frozenset({"XX-213"}), **facts})
    two = _top(second, **{"tags": frozenset({"XX-213"}), **facts})
    return ss.findings([one, two])


def test_two_sheets_of_one_item_printing_another_front_offset_is_a_wrong_on_both_pages() -> None:
    vanity = _plan(12, texts={"front_offset": ("102 [4]", "4")})
    countertop = _plan(17, front=3.625, texts={"front_offset": ('3 5/8" [92mm]', "3.625")})
    (finding,) = _pair(vanity, countertop)
    assert finding["status"] == "different" and finding["paired_by"] == "code"
    assert finding["source"] == "cross_view" and finding["reference_side"] == "vendor"
    assert (finding["vs_page"], finding["page"]) == (12, 17)
    assert finding["arch_box"]["page"] == 12 and finding["vendor_box"]["page"] == 17
    assert finding["architect"] == "102 [4]" and finding["vendor"] == '3 5/8" [92mm]'
    assert 'page 12 prints 102 [4], page 17 prints 3 5/8" [92mm]' in finding["why"]
    assert "item XX-213" in finding["why"] and "front edge" in finding["why"]
    assert finding["same_item_pages"]["pages"][0] == {"page": 12, "value": "102 [4]", "file": FILE}
    # Which sheet is right is never guessed: every output says "correct one so both pages agree".
    assert proof.should_be(judged({**finding, "id": "S1"})).startswith(
        '102 [4] (page 12) or 3 5/8" [92mm] (page 17): correct one so both pages agree'
    )


def test_matching_values_make_nothing() -> None:
    assert _pair(_plan(12), _plan(17)) == []


def test_a_one_32nd_difference_counts_inches_are_exact() -> None:
    texts = {"cutout_depth": ('12 1/32" [305mm]', "12.03125")}
    (finding,) = _pair(_plan(12), _plan(17, cut=(17, 12.03125), texts=texts))
    assert finding["item"] == "XX-213: sink cut-out depth on two sheets"


def test_a_label_code_is_not_sure_of_makes_a_please_check_never_red() -> None:
    texts = {"front_offset": ('3 1/2" TYP', "3.5")}
    (finding,) = _pair(_plan(12), _plan(17, front=3.5, texts=texts))
    assert finding["status"] == "check" and "same_item_pages" not in finding
    mark = finding[likely_problems.FIELD]
    assert mark["by"] == "set_check" and mark["proof"] == "likely_only"
    assert mark["words"].startswith("Please check and confirm: the sink cut-out front offset")
    assert "words beside the number" in mark["why"]
    assert proof.proof_group(judged({**finding, "id": "S2"})) == "likely_problem"


def test_another_top_size_or_another_round_is_never_compared() -> None:
    assert _pair(_plan(12), _plan(17, width=42, front=5)) == []
    one = _top(_plan(12), tags=frozenset({"XX-213"}), round=1)
    two = _top(_plan(17, front=5), tags=frozenset({"XX-213"}), round=2)
    assert ss.findings([one, two]) == []


def test_no_shared_item_code_and_no_shared_architect_page_is_no_pair() -> None:
    one = _top(_plan(12), tags=frozenset({"XX-213"}))
    two = _top(_plan(17, front=5), tags=frozenset({"XX-214"}))
    assert ss.findings([one, two]) == []


def test_the_same_architect_page_proves_the_pair_only_with_the_top_size() -> None:
    arch = frozenset({("architect.pdf", 52)})
    one = _top(_plan(12), architect=arch)
    two = _top(_plan(17, front=5), architect=arch)
    (finding,) = ss.findings([one, two])
    assert finding["status"] == "different" and "architect's page 52" in finding["why"]
    # A sheet whose top code cannot prove: no offset to an edge is read, so nothing to compare;
    # a cut-out size it prints differently is a please-check (the size cannot tie the pair).
    detail = _plan(17, front=5, cabinet_face=True)
    assert ss.findings([one, _top(detail, architect=arch)]) == []
    texts = {"cutout_width": ('17 1/4"', "17.25")}
    other = _top(_plan(17, cut=(17.25, 12), texts=texts, cabinet_face=True), architect=arch)
    (please,) = ss.findings([one, other])
    assert please["status"] == "check" and "no item code or printed size" in please["why"]


def test_an_offset_one_sheet_prints_on_its_other_side_is_never_compared() -> None:
    """A sheet drawn mirrored: its left offset is the other's right one."""
    left, right = _plan(12, centre=20), _plan(17, centre=34)
    words = {11.5: '11 1/2"', 25.5: '25 1/2"'}
    for sheet, (a, b) in ((left, (11.5, 25.5)), (right, (25.5, 11.5))):
        cl = X0 + a * PPI
        sheet.across(Y0 - 50, X0, cl, words[a], f"{a:g}")
        cr = X0 + (54 - b) * PPI
        sheet.across(Y0 - 50, cr, X0 + 54 * PPI, words[b], f"{b:g}")
    assert _values(_top(left))["left_to_cutout"] == '11 1/2"'
    assert _values(_top(right))["right_to_cutout"] == '11 1/2"'
    assert [f["item"] for f in _pair(left, right)] == []


def test_an_item_code_printed_on_tops_of_two_sizes_pairs_nothing() -> None:
    """A sink's model number on several vanities' sheets is no item's code."""
    model = frozenset({"XX-221"})
    tops = [
        _top(_plan(5, width=72), tags=model),
        _top(_plan(6, width=60), tags=model),
        _top(
            _plan(
                13, cabinet_face=True, cut=(17.5, 12), texts={"cutout_width": ('17 1/2"', "17.5")}
            ),
            tags=model,
        ),
    ]
    assert ss.findings(tops) == []
    # The same code on one size of top only: it is the item's, and the pair is made.
    assert len(ss.findings(tops[1:])) == 1


def test_a_short_dimension_drawn_as_two_ticked_stubs_reads_its_label_between_them() -> None:
    """A cut-out detail's faucet hole: its 2 1/2" printed between two short ticked stubs."""
    sheet = _plan(17)
    hole_y = Y0 + 23 * PPI - 4 * PPI - 12 * PPI - 2.5 * PPI
    back = hole_y + 2.5 * PPI
    x = X0 + 54 * PPI + 20
    sheet.lines += [((x, hole_y), (x, hole_y + 1.0)), ((x, back - 1.0), (x, back))]
    sheet.lines += [_tick(x, hole_y), _tick(x, back)]
    sheet.label('2 1/2" [64mm]', "2.5", [x - 25, hole_y + 0.5, x + 25, back - 0.5])
    assert _values(_top(sheet))["faucet_to_cutout"] == '2 1/2" [64mm]'


def test_earlier_pages_printing_the_same_value_are_listed_with_the_finding() -> None:
    sheets = [_plan(11), _plan(12), _plan(17, front=3.5, texts={"front_offset": ('3 1/2"', "3.5")})]
    tops = [_top(s, tags=frozenset({"XX-213"})) for s in sheets]
    (finding,) = ss.findings(tops)
    assert (finding["vs_page"], finding["page"]) == (11, 17)
    assert [a["page"] for a in finding["also_on"]] == [12]
    assert '(page 12 prints 4" too)' in finding["why"]


# ---- GV's front offset rule on a top's plan -----------------------------------------------------


def _edge(front: float, text: str, overhang: str | None = None, *, alone: bool = False) -> Any:
    top = _top(_plan(18, front=front, texts={"front_offset": (text, str(front))}), alone=alone)
    setting = (
        None
        if overhang is None
        else so.Setting(
            Fraction(overhang), f'{overhang}"', "project value", "this project's value", "overhang"
        )
    )
    found = ss.edge_findings([top], STANDARD, setting)
    return found[0] if found else None


def test_a_top_edge_offset_over_the_rule_waits_for_the_jobs_overhang() -> None:
    item = _edge(5.125, '5 1/8" [130mm]')
    assert item["status"] == "check" and item["created_by"] == so.CREATED_BY
    assert item["gv_standard"]["rule"] == so.RULE_ID and item["gv_standard"]["edge"] is True
    assert 'only if the top overhangs the cabinets by exactly 1 1/8"' in item["why"]
    assert item["job_value_rule"]["missing"] == ["front overhang"]
    found = proof.classified(judged({**item, "id": "R1", "checks": {"printed_vendor": "match"}}))
    assert found.reason == "missing_setting" and "1 1/8" in str(found.why)
    assert "front overhang" in str(found.settle)
    plain = _edge(4, '4"')
    assert "does not overhang the cabinets at all" in plain["why"]


def test_a_top_edge_offset_under_the_rule_is_under_it_whatever_the_overhang() -> None:
    item = _edge(3.625, '3 5/8" [92mm]')
    assert item["status"] == "check" and 'under 4" whatever the overhang' in item["why"]
    mark = item[likely_problems.FIELD]
    assert mark["proof"] == "likely_only" and mark["words"].startswith("Please check and confirm")
    assert proof.proof_group(judged({**item, "id": "R2"})) == "likely_problem"


def test_with_the_overhang_entered_the_rule_decides_where_no_architect_drawing_is_paired() -> None:
    wrong = _edge(4, '4"', "0.5", alone=True)
    assert wrong["status"] == "different" and wrong["settled"]["method"] == so.METHOD
    assert wrong["architect"] == '4 1/2"' and 'Should be: 4 1/2"' in wrong["why"]
    right = _edge(4.5, '4 1/2"', "0.5", alone=True)
    assert right["status"] == "same"
    # Paired with an architect drawing: a please-check, or nothing when it meets the rule.
    assert _edge(4, '4"', "0.5")["status"] == "check"
    assert _edge(4.5, '4 1/2"', "0.5") is None


# ---- the overhang the vendor's own drawing prints (Fix K) -----------------------------------------


def _vanity(
    page: int = 11,
    *,
    depth: float = 23,
    lip: float = 0.5,
    texts: dict[str, tuple[str, str]] | None = None,
    cabinet_front: bool = True,
    lip_at_back: bool = False,
) -> _Sheet:
    """A vanity's plan: the wall at the back, the cabinets `depth - lip` deep with their front
    drawn, the top's front edge `lip` in front of it, a sink cut-out with its faucet hole, and a
    chain at the left: the whole top's depth split into the cabinets and the overhang."""
    cabinet = depth - lip
    words = {
        "whole": (format_inches(Fraction(depth)), f"{depth:g}"),
        "cabinet": (format_inches(Fraction(cabinet)), f"{cabinet:g}"),
        "lip": (format_inches(Fraction(lip)), f"{lip:g}"),
        **(texts or {}),
    }
    sheet = _Sheet(page)
    x1 = X0 + 54 * PPI
    yc, yt = Y0 + cabinet * PPI, Y0 + depth * PPI
    sheet.lines += [((X0, Y0), (x1, Y0)), ((X0, yt), (x1, yt))]
    sheet.lines += [((X0, Y0), (X0, yt)), ((x1, Y0), (x1, yt))]
    if cabinet_front:
        sheet.lines.append(((X0, yc), (x1, yc)))
    mid = X0 + 27 * PPI
    cl, cr = mid - 8.5 * PPI, mid + 8.5 * PPI
    cf = yc - 4 * PPI
    cb = cf - 12 * PPI
    sheet.lines += [
        ((cl, cb), (cr, cb)),
        ((cl, cf), (cr, cf)),
        ((cl, cb), (cl, cf)),
        ((cr, cb), (cr, cf)),
    ]
    sheet.lines += _octagon(mid, cb - 2.5 * PPI, 0.7 * PPI)
    x = X0 - 12
    split = Y0 + lip * PPI if lip_at_back else yc
    parts = [
        (Y0, split, words["lip" if lip_at_back else "cabinet"]),
        (split, yt, words["cabinet" if lip_at_back else "lip"]),
    ]
    for a, b, (text, inches) in parts:
        sheet.deep(x, a, b, text, inches, dim_line=[[x, a], [x, b]])
    sheet.deep(X0 - 30, Y0, yt, *words["whole"], dim_line=[[X0 - 30, Y0], [X0 - 30, yt]])
    return sheet


def _drawn(sheet: _Sheet, depth: float = 23) -> set[tuple[Fraction, Fraction, str]]:
    box = (0.0, 0.0, 2000.0, 2000.0)
    drawing = so.Drawing(FILE, sheet.page, box, tuple(sheet.labels), PPI, "SINK")
    plans = so.plans_on(sheet.labels, sheet.lines, box, PPI)
    assert plans, "the made-up vanity plan has a sink cut-out code proves"
    found: set[tuple[Fraction, Fraction, str]] = set()
    for plan in plans:
        found |= ss._overhangs_on(drawing, plan, sheet.lines, Fraction(depth))
    return found


def test_the_overhang_is_read_from_the_vendors_chain_on_a_vanity_plan() -> None:
    ((lip, cabinet, _id),) = _drawn(_vanity())
    assert (lip, cabinet) == (Fraction(1, 2), Fraction(45, 2))
    texts = {"lip": ("13 [1/2]", "0.5"), "cabinet": ("571 [22 1/2]", "22.5")}
    assert {f[0] for f in _drawn(_vanity(texts=texts))} == {Fraction(1, 2)}


def test_no_overhang_is_read_unless_every_step_is_proved() -> None:
    assert _drawn(_vanity(cabinet_front=False)) == set()  # no cabinets' front drawn at the split
    assert _drawn(_vanity(), depth=24) == set()  # the whole is not the top's depth
    wrong_sum = {"cabinet": ('22 1/4"', "22.25")}
    assert _drawn(_vanity(texts=wrong_sum)) == set()  # the parts do not add up
    assert _drawn(_vanity(lip_at_back=True)) == set()  # the small part is at the back
    words = {"lip": ('1/2" OVERHANG', "0.5")}
    assert _drawn(_vanity(texts=words)) == set()  # words beside the number
    assert _drawn(_vanity(lip=3)) == set()  # over 2": not a top's lip


def _printed(page: int = 11, value: str = "0.5", tag: str = "XX-214", depth: int = 21) -> Any:
    lip = Fraction(value)
    return ss.PrintedOverhang(
        FILE,
        page,
        frozenset({tag}),
        None,
        lip,
        Fraction(depth),
        depth - lip,
        {"id": "D9", "text": f"13 [{value}]"},
    )


def _edge_drawn(
    front: float, text: str, printed: Any, *, alone: bool = False, overhang: Any = None
) -> Any:
    sheet = _plan(18, width=42, depth=21, front=front, texts={"front_offset": (text, str(front))})
    top = _top(sheet, alone=alone, tags=frozenset({"XX-214"}))
    found = ss.edge_findings([top], STANDARD, overhang, printed=printed)
    return found[0] if found else None


def test_with_no_job_overhang_the_vendors_printed_overhang_is_used_and_named() -> None:
    item = _edge_drawn(4, '4"', [_printed(14)])
    assert item["status"] == "check" and item[likely_problems.FIELD]["proof"] == "likely_only"
    assert "on page 14" in item["why"] and "3 1/2\" from the cabinets' front" in item["why"]
    assert "should be 4 1/2\" from the top's front edge" in item["why"]
    assert item["gv_standard"]["overhang_from"] == {"page": 14, "label": "13 [0.5]"}
    assert "job_value_rule" not in item
    found = proof.classified(judged({**item, "id": "R9"}))
    assert proof.proof_group(judged({**item, "id": "R9"})) == "likely_problem", found
    over = _edge_drawn(5.0625, '5 1/16" [129mm]', [_printed(14)])
    assert over["status"] == "check" and "4 9/16\" from the cabinets' front" in over["why"]
    # A match is Right only where no architect drawing is paired; never red either way.
    right = _edge_drawn(4.5, '4 1/2"', [_printed(14)], alone=True)
    assert right["status"] == "same" and "on page 14" in right["why"]
    assert _edge_drawn(4.5, '4 1/2"', [_printed(14)]) is None
    assert _edge_drawn(4, '4"', [_printed(14)], alone=True)["status"] == "check"


def test_a_printed_overhang_is_used_only_for_the_same_item_and_when_all_agree() -> None:
    plain = "does not overhang the cabinets at all"
    assert plain in _edge_drawn(4, '4"', [_printed(14, tag="XX-999")])["why"]  # another item
    assert plain in _edge_drawn(4, '4"', [_printed(14, depth=23)])["why"]  # another depth
    two = [_printed(11), _printed(14, value="0.75")]
    assert plain in _edge_drawn(4, '4"', two)["why"]  # two values: none is used
    # An entered job overhang always wins.
    entered = so.Setting(Fraction(1), '1"', "project value", "this project's value", "overhang")
    item = _edge_drawn(4, '4"', [_printed(14)], overhang=entered)
    assert "job's 1\" front overhang" in item["why"] and "page 14" not in item["why"]


def test_the_overhang_is_passed_to_the_rule_only_when_entered() -> None:
    rules_md = (
        "| Setting | In force | Where it comes from | Used by |\n|---|---|---|---|\n"
        "| Sink front offset (`front_offset_required`) | 4\" | GV's company standard | x |\n"
        "| Countertop overhang (`countertop_overhang`) | 1/2\" | this project's value | x |\n"
    )
    settings = so.settings_from(rules_md)
    assert settings[so.TOP.setting].value == Fraction(1, 2)
    assert so.TOP.setting not in so.settings_from(rules_md.replace('1/2"', "not set"))


def test_a_job_value_rule_with_its_own_words_says_them() -> None:
    why, settle = job_value_rules.words(
        {"rule": "R", "name": "n", "formula": "a = b + c", "missing": ["c"], "why": "Own words."}
    )
    assert why == "Own words. GV's formula: a = b + c (R)" and "the job's c" in settle


# ---- the hooks ---------------------------------------------------------------------------------


class _Book:
    main_file = FILE

    def __init__(self, live: list[dict[str, Any]]) -> None:
        self._live = {str(f["id"]): f for f in live}
        self.n = 0

    def live(self) -> list[dict[str, Any]]:
        return list(self._live.values())

    def add_code_finding(self, given: dict[str, Any], *, created_by: str) -> dict[str, Any]:
        dropped = (*so._EXTRA, "checks", "same_item_pages", "also_on")
        self.n += 1
        made = {**{k: v for k, v in given.items() if k not in dropped}, "id": f"N{self.n}"}
        made.update({"created_by": created_by, "checks": {"printed_vendor": "match"}})
        self._live[made["id"]] = made
        return dict(made)

    def replace(self, finding: dict[str, Any]) -> bool:
        self._live[str(finding["id"])] = finding
        return True

    def absorb(self, finding_id: str) -> bool:
        return self._live.pop(finding_id, None) is not None


class _Engine:
    def __init__(self, live: list[dict[str, Any]], mode: str = "full_check") -> None:
        self.settings = SimpleNamespace(mode=mode)
        workspace = SimpleNamespace(set_pack=None, skills=None, uploads=None)
        self.tools = SimpleNamespace(workspace=workspace)
        self.book = _Book(live)
        self.product_scope = ProductScope.of(["countertop"])
        self.finding_lock = threading.RLock()
        self.cancel = threading.Event()
        self.dropped: list[tuple[str, str]] = []

    def _show_finding(self, finding: dict[str, Any]) -> None:
        pass

    def _show_dropped(self, finding_id: str, reason: str) -> None:
        self.dropped.append((finding_id, reason))


def test_the_hook_adds_each_difference_once_with_its_mark_and_both_spots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vanity = _plan(12, texts={"front_offset": ("102 [4]", "4")})
    please = {"front_offset": ('3 5/8" TYP', "3.625")}
    tops = [
        _top(vanity, tags=frozenset({"XX-213"})),
        _top(
            _plan(
                17,
                front=3.625,
                cut=(17.5, 12),
                texts={**please, "cutout_width": ("445 [17 1/2]", "17.5")},
            ),
            tags=frozenset({"XX-213"}),
        ),
    ]
    monkeypatch.setattr(ss, "read_pack", lambda *a, **k: tops)
    engine = _Engine([])
    assert ss.add_to_answer(engine) == 2
    red, check = sorted(engine.book.live(), key=lambda f: f["status"] != "different")
    assert red["created_by"] == ss.CREATED_BY and red["same_item_pages"]["pages"][1]["page"] == 17
    assert check[likely_problems.FIELD]["by"] == "set_check"
    assert ss.add_to_answer(engine) == 0  # the same two labels: never a second card
    assert ss.add_to_answer(_Engine([], mode="chat")) == 0


def _note(status: str, label: str = "D5", **extra: Any) -> dict[str, Any]:
    return {
        "id": "F1",
        "page": 18,
        "status": status,
        "dims": {"vendor": {"id": label, "page": 18, "file": FILE}},
        **extra,
    }


@pytest.fixture
def rule_hook(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> tuple[list[dict[str, Any]], Any]:
    """The rule's items the set pack's check gives (filled by a test), and a chat folder with
    `rules.md`."""
    (tmp_path / "rules.md").write_text(
        "| Sink front offset (`front_offset_required`) | 4\" | GV's company standard | x |\n"
    )
    found: list[dict[str, Any]] = []
    monkeypatch.setattr(so, "check_pack", lambda *a, **k: [dict(f) for f in found])
    return found, tmp_path


def _rule_engine(live: list[dict[str, Any]], folder: Any) -> _Engine:
    engine = _Engine(live)
    engine.tools.workspace.skills = folder
    engine.product_scope = ProductScope.of(["countertop"])
    return engine


def test_the_rule_on_a_top_plan_takes_a_label_that_only_says_not_compared(
    rule_hook: tuple[list[dict[str, Any]], Any],
) -> None:
    found, folder = rule_hook
    item = _edge(5.125, '5 1/8" [130mm]')
    found.append({**item, "category": "countertop"})
    label = item["dims"]["vendor"]["id"]
    helper = _note("check", label, created_by="helper:53", architect="not shown")
    engine = _rule_engine([helper], folder)
    assert so.add_to_answer(engine) == {"added": 1, "replaced": 1}
    (kept,) = engine.book.live()
    assert kept["created_by"] == so.CREATED_BY and kept["job_value_rule"]["missing"]
    assert engine.dropped[0][0] == "F1" and engine.dropped[0][1].startswith("Said by GV Review")
    # A page reviewer's please-check or code's own item on the label speaks: the note stays out.
    reviewer = _note(
        "check", label, created_by="page_reviewer", whole_look={"proof": "likely_only"}
    )
    engine = _rule_engine([reviewer], folder)
    assert so.add_to_answer(engine) == {"added": 0, "replaced": 0}


# ---- the faster rectangle search finds what the old one did --------------------------------------


def _all_pairs(frame: so._Frame, low: float, high: float) -> list[tuple[float, ...]]:
    """`sink_offset._rectangles` as it was: every pair of sides tried, in order."""
    sides = sorted(
        (
            p
            for p in frame.along
            if not (so._crossed(frame, p[2], p[0]) and so._crossed(frame, p[2], p[1]))
        ),
        key=lambda piece: piece[2],
    )
    found = set()
    for i, (a0, a1, left) in enumerate(sides):
        for b0, b1, right in sides[i + 1 :]:
            width = right - left
            if width > high:
                break
            if width < low or abs(a0 - b0) > 0.9 or abs(a1 - b1) > 0.9:
                continue
            top_side, bottom_side = (a0 + b0) / 2, (a1 + b1) / 2
            reach = so._ROUND_SHARE * min(width, bottom_side - top_side)
            top = so._edge(frame, left, right, top_side, -1, reach)
            bottom = so._edge(frame, left, right, bottom_side, 1, reach)
            if top is None or bottom is None:
                continue
            if low <= bottom - top <= high:
                found.add((round(left, 2), round(top, 2), round(right, 2), round(bottom, 2)))
    return sorted(found)


def test_the_indexed_rectangle_search_finds_exactly_what_trying_every_pair_found() -> None:
    rng = random.Random(7)
    lines: list[Segment] = []
    for _ in range(60):
        x, y = rng.uniform(0, 300), rng.uniform(0, 300)
        w, h = rng.uniform(5, 120), rng.uniform(5, 120)
        jitter = rng.choice((0.0, 0.4, 1.2))
        lines += [
            ((x, y), (x + w, y)),
            ((x, y + h), (x + w, y + h)),
            ((x, y), (x, y + h)),
            ((x + w, y + jitter), (x + w, y + h + jitter)),
        ]
    lines += _plan().lines
    frame = so._Frame.of(lines, "bottom")
    for ppi in (1.0, 2.0, 4.0):
        assert so._rectangles(frame, ppi) == _all_pairs(frame, 6 * ppi, 48 * ppi)


def test_a_corner_drawn_as_one_chord_is_not_a_tick_with_a_margin() -> None:
    frame = so._Frame.of([((10.0, 0.0), (10.0, 20.0)), ((13.0, -3.0), (9.94, 0.06))], "bottom")
    assert so._crossed(frame, 10.0, 0.0)
    assert not so._crossed(frame, 10.0, 0.0, so._SAME_PT)


def test_printed_overhangs_reads_a_vanity_sheet_of_the_same_item() -> None:
    vanity = _vanity(11, depth=21)
    top = _top(_plan(18, width=42, depth=21), tags=frozenset({"XX-214"}))
    box = (0.0, 0.0, 2000.0, 2000.0)
    drawing = so.Drawing(FILE, 11, box, tuple(vanity.labels), PPI, "SINK")
    plans = so.plans_on(vanity.labels, vanity.lines, box, PPI)
    pages = {
        (FILE, 11): {"phrases": [{"text": "XX-214"}]},
        (FILE, 18): {"phrases": [{"text": "XX-214"}]},
    }
    (found,) = ss.printed_overhangs(
        [(drawing, plans, None)], lambda _f, _p: vanity.lines, pages, [top]
    )
    assert (found.page, found.value, found.depth, found.tags) == (
        11,
        Fraction(1, 2),
        Fraction(21),
        frozenset({"XX-214"}),
    )
    assert "page 11" in found.words() and '21" depth split into 20 1/2"' in found.words()
    other = {**pages, (FILE, 11): {"phrases": [{"text": "XX-214 XX-215"}]}}
    assert (
        ss.printed_overhangs([(drawing, plans, None)], lambda _f, _p: vanity.lines, other, [top])
        == []
    )
