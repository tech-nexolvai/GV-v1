"""GV's run fit (`run_fit`): a vendor's run against the architect's by the product's site-fit rule.

Made-up runs only (no client data, no PDF): each drawing is built here as the set pack would hold
it (`dims.json` labels with their dimension and extension lines) and its line work (ticks on the
dimension lines, and inside each part's column the look of the part: door swings crossed, drawer
fronts stacked, a filler a plain strip). The cabinet rule is the repository's registry entry; a
made-up "bench" product shows a new product needs only a registry entry.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.review.assistant import escalation, proof
from app.review.assistant import run_fit as rf
from app.review.assistant.chat_trust import judged
from app.review.assistant.finding_checks import to_inches
from app.review.assistant.product_scope import CODE_MADE, ProductScope, refiled
from vocabulary.product_registry import (
    DEFAULT_REGISTRY,
    Product,
    RegistryError,
    load_registry,
    registry,
    registry_data,
)

VENDOR = "vendor.pdf"
ARCHITECT = "architect.pdf"
CABINET = registry().get("cabinet")
assert CABINET is not None and CABINET.run_fit is not None

F = Fraction


# ---- a made-up drawing ---------------------------------------------------------------------------

#: Points per inch on the made-up drawings.
SCALE = 4.0
ROW_Y = 100.0
OVERALL_Y = 70.0
TOP_Y = 110.0
BOTTOM_Y = 300.0


def _text(value: Fraction) -> str:
    whole, rest = divmod(value, 1)
    return f'{int(whole)} {rest.numerator}/{rest.denominator}"' if rest else f'{int(whole)}"'


@dataclass
class Sheet:
    """One made-up drawing: its labels and its line work."""

    file: str
    side: str
    labels: list[dict[str, Any]] = field(default_factory=list)
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = field(default_factory=list)
    phrases: list[dict[str, Any]] = field(default_factory=list)

    def label(
        self,
        value: Fraction,
        start: float,
        end: float,
        across: float,
        orientation: str = "h",
        line: bool = True,
        words: str = "",
        extension: Any = None,
    ) -> dict[str, Any]:
        text = _text(value)
        middle = (start + end) / 2
        if orientation == "h":
            box = [middle - 6, across - 12, middle + 6, across - 1]
            dim_line = [[start, across], [end, across]]
        else:
            box = [across - 12, middle - 6, across - 1, middle + 6]
            dim_line = [[across, start], [across, end]]
        made: dict[str, Any] = {
            "id": f"D{len(self.labels) + 1}",
            "text": f"{text} {words}".strip(),
            "inches": str(float(value)),
            "side": self.side,
            "route": "text",
            "orientation": orientation,
            "box": box,
            "dim_line": dim_line if line else None,
            "extension": extension or [None, None],
        }
        self.labels.append(made)
        return made

    def tick(self, x: float, y: float) -> None:
        self.segments.append(((x - 2.5, y + 2.5), (x + 2.5, y - 2.5)))

    def column(self, start: float, end: float, drawn: str) -> None:
        """What is drawn inside a part: its top and bottom, and its look."""
        top, bottom = TOP_Y, BOTTOM_Y - 5
        self.segments += [((start, top), (end, top)), ((start, bottom), (end, bottom))]
        if drawn == "crossed":
            middle = (top + bottom) / 2
            self.segments += [((start, top), (end, middle)), ((start, bottom), (end, middle))]
        elif drawn == "stacked":
            for y in (top + 40, top + 90):
                self.segments.append(((start, y), (end, y)))
        elif drawn == "odd":
            self.segments.append(((start, top + 40), (end, top + 40)))


def run_sheet(
    file: str,
    side: str,
    parts: Sequence[tuple[Fraction, str]],
    *,
    overall: Fraction | None = None,
    x0: float = 100.0,
    lineless_ends: bool = False,
    words: Sequence[str] | None = None,
    stack: Sequence[Fraction] | None = None,
) -> Sheet:
    """A run from `x0`: each part's printed value and its look (crossed / stacked / plain / none),
    drawn to its value at `SCALE`; the overall printed above (its own sum unless given). With
    `lineless_ends`, the two end parts' labels have no dimension line of their own (a narrow
    filler's label printed wider than its span), only their place over it."""
    sheet = Sheet(file, side)
    end = x0 + float(sum(v for v, _ in parts)) * SCALE
    total = overall if overall is not None else sum((v for v, _ in parts), F(0))
    sheet.label(
        total,
        x0,
        end,
        OVERALL_Y,
        extension=[[[x0, BOTTOM_Y], [x0, OVERALL_Y]], [[end, BOTTOM_Y], [end, OVERALL_Y]]],
    )
    sheet.tick(x0, OVERALL_Y)
    sheet.tick(end, OVERALL_Y)
    x = x0
    for index, (value, drawn) in enumerate(parts):
        right = x + float(value) * SCALE
        line = not (lineless_ends and index in (0, len(parts) - 1))
        sheet.label(value, x, right, ROW_Y, line=line, words=(words or [""] * len(parts))[index])
        sheet.tick(x, ROW_Y)
        sheet.tick(right, ROW_Y)
        if drawn != "none":
            sheet.column(x, right, drawn)
        x = right
    if stack is not None:
        # The height stack beside the run: floor first at the bottom.
        stack_total = sum(stack, F(0))
        top = BOTTOM_Y - float(stack_total) * SCALE
        sheet.label(stack_total, top, BOTTOM_Y, x0 - 40, orientation="v")
        y = BOTTOM_Y
        for value in stack:
            upper = y - float(value) * SCALE
            sheet.label(value, upper, y, x0 - 20, orientation="v")
            sheet.segments.append(((x0 - 22.5, upper + 2.5), (x0 - 17.5, upper - 2.5)))
            sheet.segments.append(((x0 - 22.5, y + 2.5), (x0 - 17.5, y - 2.5)))
            y = upper
    return sheet


def _drawing(sheet: Sheet) -> rf.DrawingLabels:
    return rf.DrawingLabels(sheet.file, 1, tuple(sheet.labels), tuple(sheet.phrases))


def _check(vendor: Sheet, *architects: Sheet, products: Sequence[Product] = ()) -> list[rf.Paired]:
    lines = {(s.file, 1): s.segments for s in (vendor, *architects)}

    def segments(file: str, page: int) -> Any:
        return lines.get((file, page))

    return rf.check_drawings(
        _drawing(vendor),
        [_drawing(a) for a in architects],
        segments,
        list(products) or [CABINET],
    )


def _findings(vendor: Sheet, *architects: Sheet, **kw: Any) -> list[dict[str, Any]]:
    return [f for paired in _check(vendor, *architects, **kw) for f in rf.findings_of(paired)]


def cabinet_run(
    filler: Fraction, drawer: Fraction, doors: Sequence[Fraction], *, right: Fraction | None = None
) -> list[tuple[Fraction, str]]:
    """filler, drawer bank, door cabinets, filler (the right filler as given, else the left's)."""
    return [
        (filler, "plain"),
        (drawer, "stacked"),
        *((d, "crossed") for d in doors),
        (right if right is not None else filler, "plain"),
    ]


def _outcomes(found: Sequence[dict[str, Any]]) -> list[tuple[str, str, str]]:
    return [(f["item"], f["status"], f["vendor"]) for f in found]


# ---- the arithmetic ------------------------------------------------------------------------------


def test_the_rule_on_a_smaller_and_a_bigger_site() -> None:
    roles = ["fixed", "fixed", "flex", "flex", "flex", "fixed"]
    names = ["filler", "drawer bank", "door 1", "door 2", "door 3", "filler"]
    design = [F(3), F(24), F(16), F(16), F(16), F(3)]
    smaller = rf.judge_run(
        design, [F(3), F(24), F(14), F(14), F(14), F(3)], roles, names, F(78), F(72)
    )
    assert smaller is not None and smaller.adds_up and smaller.change == F(-2)
    assert all(p is not None and p.outcome == "right" for p in smaller.parts)
    door = smaller.parts[2]
    assert door is not None and door.proof == 'door 1 should be 14" = (72" - 3" - 24" - 3") / 3'
    bigger = rf.judge_run(
        design, [F(3), F(24), F(19), F(19), F(19), F(3)], roles, names, F(78), F(87)
    )
    assert bigger is not None and [p.outcome for p in bigger.parts if p] == ["right"] * 6


def test_a_filler_changed_instead_of_the_doors_is_wrong() -> None:
    roles = ["fixed", "flex", "flex", "fixed"]
    names = ["filler", "door 1", "door 2", "filler"]
    judged_run = rf.judge_run(
        [F(3), F(16), F(16), F(3)], [F(1), F(16), F(16), F(1)], roles, names, F(38), F(34)
    )
    assert judged_run is not None
    left, door, _, _ = judged_run.parts
    assert left is not None and left.outcome == "wrong" and left.should_be == F(3)
    assert left.proof == 'filler should stay 3": it keeps its design size'
    assert door is not None and door.outcome == "wrong" and door.should_be == F(14)


def test_unequal_doors_and_a_changed_drawer_bank_are_wrong() -> None:
    roles = ["fixed", "flex", "flex", "flex"]
    names = ["drawer bank", "door 1", "door 2", "door 3"]
    design = [F(24), F(16), F(16), F(16)]
    unequal = rf.judge_run(design, [F(24), F(13), F(15), F(14)], roles, names, F(72), F(66))
    assert unequal is not None
    assert [p.outcome for p in unequal.parts if p] == ["right", "wrong", "wrong", "right"]
    drawer = rf.judge_run(design, [F(22), F(15), F(15), F(14)], roles, names, F(72), F(66))
    assert drawer is not None
    assert [p.outcome for p in drawer.parts if p] == ["wrong", "wrong", "wrong", "right"]


def test_a_string_that_does_not_add_up_judges_only_its_fixed_parts() -> None:
    roles = ["fixed", "flex", "flex", "fixed"]
    found = rf.judge_run(
        [F(3), F(16), F(16), F(3)], [F(3), F(15), F(17), F(4)], roles, list("abcd"), F(38), F(36)
    )
    assert found is not None and not found.adds_up
    assert found.parts[1] is None and found.parts[2] is None
    assert found.parts[3] is not None and found.parts[3].outcome == "wrong"


def test_what_the_rule_cannot_settle_is_not_judged() -> None:
    roles = ["fixed", "flex", "flex", "flex"]
    # A share that is not a whole 1/16": the door cabinets are not judged.
    third = rf.judge_run(
        [F(3), F(16), F(16), F(16)], [F(3), F(15), F(15), F(16)], roles, list("abcd"), F(51), F(50)
    )
    assert third is not None and third.parts[1:] == (None, None, None)
    # The architect's own parts do not add up, or no part may flex: no judgement at all.
    two, names = ["fixed", "flex"], ["a", "b"]
    assert rf.judge_run([F(3), F(16)], [F(3), F(16)], two, names, F(19), F(19)) is not None
    assert rf.judge_run([F(3), F(16)], [F(3), F(16)], two, names, F(18), F(19)) is None
    fixed = ["fixed", "fixed"]
    assert rf.judge_run([F(3), F(16)], [F(3), F(16)], fixed, names, F(19), F(19)) is None


# ---- whole runs on made-up drawings --------------------------------------------------------------


DOORS = {3: [F(16)] * 3, 4: [F(16)] * 4, 5: [F(16)] * 5}


@pytest.mark.parametrize("count", [2, 3, 4, 5])
@pytest.mark.parametrize(
    ("filler", "change"), [(F(3), F(-2)), (F(1, 2) * 3, F(3)), (F(5), F(1, 2))]
)
def test_a_run_fitted_by_the_rule_is_right(count: int, filler: Fraction, change: Fraction) -> None:
    design = cabinet_run(filler, F(24), [F(16)] * count)
    site = cabinet_run(filler, F(24), [F(16) + change] * count)
    found = _findings(run_sheet(VENDOR, "vendor", site), run_sheet(ARCHITECT, "architect", design))
    assert len(found) == count + 3
    assert all(f["status"] == "same" for f in found), _outcomes(found)
    doors = [f for f in found if "door cabinet" in f["item"]]
    assert all(f["run_fit"]["should_be"] == _text(F(16) + change) for f in doors)


def test_the_parts_kinds_and_names_are_read_from_the_drawing() -> None:
    design = cabinet_run(F(3), F(24), [F(16)] * 3)
    site = cabinet_run(F(3), F(24), [F(14)] * 3)
    (paired,) = _check(run_sheet(VENDOR, "vendor", site), run_sheet(ARCHITECT, "architect", design))
    assert paired.kinds == ("filler", "drawer bank", *["door cabinet"] * 3, "filler")
    items = [f["item"] for f in rf.findings_of(paired)]
    assert items[0] == "cabinet run, filler (left) width"
    assert items[2] == "cabinet run, door cabinet 1 width"


def test_a_narrow_filler_whose_label_has_no_line_is_taken_in_over_its_marked_span() -> None:
    design = cabinet_run(F(3), F(24), [F(16)] * 3)
    site = cabinet_run(F(3), F(24), [F(18)] * 3)
    vendor = run_sheet(VENDOR, "vendor", site, lineless_ends=True)
    architect = run_sheet(ARCHITECT, "architect", design, lineless_ends=True)
    (paired,) = _check(vendor, architect)
    assert [p.taken_in for p in paired.vendor.parts] == [True, False, False, False, False, True]
    assert all(f["status"] == "same" for f in rf.findings_of(paired))
    # Without the ticks that mark its span, no label is taken in: no run, no finding.
    vendor.segments = [s for s in vendor.segments if not _is_tick_at(s, 100.0, ROW_Y)]
    assert _check(vendor, architect) == []


def _is_tick_at(segment: Any, x: float, y: float) -> bool:
    (ax, ay), (bx, by) = segment
    return abs((ax + bx) / 2 - x) < 0.1 and abs((ay + by) / 2 - y) < 0.1


@pytest.mark.parametrize(
    ("site", "wrong"),
    [
        # A filler changed instead of the doors (the run still adds up).
        (
            cabinet_run(F(3, 2), F(24), [F(16)] * 3),
            {"filler (left)", "filler (right)", "door cabinet"},
        ),
        # Unequal doors.
        (cabinet_run(F(3), F(24), [F(13), F(15), F(14)]), {"door cabinet"}),
        # The drawer bank changed.
        (cabinet_run(F(3), F(22), [F(15), F(15), F(14)]), {"drawer bank", "door cabinet"}),
    ],
)
def test_a_vendor_that_breaks_the_rule_is_wrong_with_the_rules_value(
    site: list[tuple[Fraction, str]], wrong: set[str]
) -> None:
    design = cabinet_run(F(3), F(24), [F(16)] * 3)
    found = _findings(run_sheet(VENDOR, "vendor", site), run_sheet(ARCHITECT, "architect", design))
    reds = [f for f in found if f["status"] == "different"]
    assert reds, _outcomes(found)
    assert {
        f["run_fit"]["kind"] if "door" in f["item"] else f["item"].split(", ")[1][: -len(" width")]
        for f in reds
    } == wrong
    for red in reds:
        assert red["run_fit"]["proof"] in red["why"]


def test_the_greens_and_the_proofs_of_the_wrong_parts() -> None:
    """The vendor narrowed the fillers instead of the doors: 1 1/2" + 24" + 3 x 16" + 1 1/2" = 75"."""
    design = cabinet_run(F(3), F(24), [F(16)] * 3)
    site = cabinet_run(F(3, 2), F(24), [F(16)] * 3)
    found = {
        f["item"]: f
        for f in _findings(
            run_sheet(VENDOR, "vendor", site), run_sheet(ARCHITECT, "architect", design)
        )
    }
    filler = _recorded(found["cabinet run, filler (left) width"])
    assert proof.proof_group(filler) == "clouded"
    assert proof.should_be(filler).startswith('3" (architect')
    door = _recorded(found["cabinet run, door cabinet 1 width"])
    assert door["source"] == "rules" and door["architect"] == '15"'
    assert proof.proof_group(door) == "clouded"
    assert proof.should_be(door) == '15" (GV rule)'
    assert 'door cabinet 1 should be 15" = (75" - 3" - 24" - 3") / 3' in proof.proof_line(door)
    right = _recorded(found["cabinet run, drawer bank width"])
    assert proof.proof_group(right) == "right"


def test_a_door_narrowed_by_the_site_is_right_not_red() -> None:
    design = cabinet_run(F(3), F(24), [F(16)] * 3)
    site = cabinet_run(F(3), F(24), [F(14)] * 3)
    found = _findings(run_sheet(VENDOR, "vendor", site), run_sheet(ARCHITECT, "architect", design))
    door = _recorded(next(f for f in found if "door cabinet 2" in f["item"]))
    assert door["status"] == "same" and proof.proof_group(door) == "right"


def _recorded(made: dict[str, Any]) -> dict[str, Any]:
    """As the finding book records it: code read the printed sides, and its values in inches."""
    checks = {"printed_vendor": "match", **made.get("checks", {})}
    if made.get("arch_box"):
        checks["printed_architect"] = "match"
    inches = {k: str(to_inches(made[k])) for k in ("architect", "vendor")}
    return judged({**made, "id": "F1", "checks": checks, "inches": inches, "compared": True})


def test_the_height_stack_keeps_its_design_sizes() -> None:
    stack = [F(4), F(12), F(9), F(7), F(2)]
    design = cabinet_run(F(3), F(24), [F(16)] * 3)
    site = cabinet_run(F(3), F(24), [F(14)] * 3)
    found = _findings(
        run_sheet(VENDOR, "vendor", site, stack=stack),
        run_sheet(ARCHITECT, "architect", design, stack=stack),
    )
    heights = [f for f in found if "from the floor" in f["item"]]
    assert len(heights) == 5 and all(f["status"] == "same" for f in heights)
    changed = [F(4), F(12), F(10), F(7), F(2)]
    found = _findings(
        run_sheet(VENDOR, "vendor", site, stack=changed),
        run_sheet(ARCHITECT, "architect", design, stack=stack),
    )
    (red,) = [f for f in found if f["status"] == "different"]
    assert red["item"] == "cabinet run, height 3 of 5 from the floor"
    assert (
        red["run_fit"]["proof"]
        == 'height 3 of 5 from the floor should stay 9": the heights keep their design sizes'
    )


# ---- unclear: no finding -------------------------------------------------------------------------


def test_a_part_neither_drawing_tells_gives_no_finding() -> None:
    design = cabinet_run(F(3), F(24), [F(16)] * 3)
    site = cabinet_run(F(3), F(24), [F(14)] * 3)
    design[3] = (F(16), "odd")
    site[3] = (F(14), "odd")
    assert (
        _check(run_sheet(VENDOR, "vendor", site), run_sheet(ARCHITECT, "architect", design)) == []
    )


def test_one_side_tells_a_part_the_other_cannot() -> None:
    design = cabinet_run(F(3), F(24), [F(16)] * 3)
    site = cabinet_run(F(3), F(24), [F(14)] * 3)
    site[3] = (F(14), "none")
    (paired,) = _check(run_sheet(VENDOR, "vendor", site), run_sheet(ARCHITECT, "architect", design))
    assert paired.kinds[3] == "door cabinet"


def test_a_plain_part_mid_run_is_never_a_filler_and_disagreeing_kinds_give_nothing() -> None:
    design = cabinet_run(F(3), F(24), [F(16)] * 3)
    site = cabinet_run(F(3), F(24), [F(14)] * 3)
    site[3] = (F(14), "plain")  # a door drawn without its swing: not told
    design[3] = (F(16), "plain")
    assert (
        _check(run_sheet(VENDOR, "vendor", site), run_sheet(ARCHITECT, "architect", design)) == []
    )
    design = cabinet_run(F(3), F(24), [F(16)] * 3)
    site = cabinet_run(F(3), F(24), [F(14)] * 3)
    site[1] = (F(24), "crossed")  # the vendor draws a door where the architect has drawers
    assert (
        _check(run_sheet(VENDOR, "vendor", site), run_sheet(ARCHITECT, "architect", design)) == []
    )


def test_two_architect_runs_that_fit_give_no_finding() -> None:
    design = cabinet_run(F(3), F(24), [F(16)] * 3)
    site = cabinet_run(F(3), F(24), [F(14)] * 3)
    vendor = run_sheet(VENDOR, "vendor", site)
    first = run_sheet(ARCHITECT, "architect", design)
    second = run_sheet("architect-2.pdf", "architect", design)
    assert _check(vendor, first, second) == []


def test_a_run_of_another_length_is_not_paired() -> None:
    design = cabinet_run(F(3), F(24), [F(16)] * 4)
    site = cabinet_run(F(3), F(24), [F(14)] * 3)
    assert (
        _check(run_sheet(VENDOR, "vendor", site), run_sheet(ARCHITECT, "architect", design)) == []
    )


def test_drawings_the_work_list_left_unpaired_pair_only_when_every_fixed_part_agrees() -> None:
    design = cabinet_run(F(3), F(24), [F(16)] * 3)
    vendor = run_sheet(VENDOR, "vendor", cabinet_run(F(3), F(24), [F(14)] * 3))
    lines = {(VENDOR, 1): vendor.segments}
    architect = run_sheet(ARCHITECT, "architect", design)
    lines[(ARCHITECT, 1)] = architect.segments

    def segments(file: str, page: int) -> Any:
        return lines.get((file, page))

    paired = rf.check_drawings(
        _drawing(vendor), [_drawing(architect)], segments, [CABINET], by_run=True
    )
    assert len(paired) == 1
    other = run_sheet(VENDOR, "vendor", cabinet_run(F(2), F(24), [F(15)] * 3, right=F(3)))
    lines[(VENDOR, 1)] = other.segments
    assert (
        rf.check_drawings(_drawing(other), [_drawing(architect)], segments, [CABINET], by_run=True)
        == []
    )


# ---- a new product is a registry entry -----------------------------------------------------------


BENCH = {
    "id": "bench",
    "label": "Bench",
    "plural": "Benches",
    "description": "A made-up seating run for tests.",
    "active": False,
    "draft": True,
    "check_kinds": ["dimension_match"],
    "checklist": [{"name": "sizes", "text": "Each seat and end panel."}],
    "run_fit": {
        "source": "A made-up rule: end panels keep their size; seats share the difference.",
        "distribution": "equally",
        "parts": [
            {"kind": "end panel", "role": "fixed", "words": ["end panel"]},
            {"kind": "seat", "role": "flex", "words": ["seat"]},
        ],
    },
}


def _registry_with(tmp_path: Path, *entries: dict[str, Any]) -> Any:
    data = registry_data(DEFAULT_REGISTRY)
    data["skills_folder"] = str(tmp_path)
    data["products"] = [*data["products"], *entries]
    path = tmp_path / "products.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_registry(path)


def test_a_made_up_product_needs_only_its_registry_entry(tmp_path: Path) -> None:
    bench = _registry_with(tmp_path, BENCH).get("bench")
    assert bench is not None and bench.run_fit is not None
    design = [(F(3), "none"), (F(20), "none"), (F(20), "none"), (F(3), "none")]
    site = [(F(3), "none"), (F(22), "none"), (F(22), "none"), (F(3), "none")]
    words = ["end panel", "seat", "seat", "end panel"]
    vendor = run_sheet(VENDOR, "vendor", site, words=words)
    architect = run_sheet(ARCHITECT, "architect", design, words=words)
    found = _findings(vendor, architect, products=[bench])
    assert [f["status"] for f in found] == ["same"] * 4
    assert found[1]["item"] == "bench run, seat 1 width" and found[1]["category"] == "bench"
    wrong = run_sheet(VENDOR, "vendor", [(F(4), "none"), *site[1:3], (F(2), "none")], words=words)
    reds = [f for f in _findings(wrong, architect, products=[bench]) if f["status"] == "different"]
    assert [f["run_fit"]["proof"] for f in reds] == [
        'end panel (left) should stay 3": it keeps its design size',
        'end panel (right) should stay 3": it keeps its design size',
    ]
    # The cabinet rule does not read it (no cabinet words or looks): no cabinet finding.
    assert _findings(vendor, architect) == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"parts": [{"kind": "seat", "role": "fixed", "words": ["seat"]}]}, "must be flex"),
        ({"parts": [{"kind": "seat", "role": "bend", "words": ["seat"]}]}, "fixed or flex"),
        ({"distribution": "by size"}, "distribution"),
        (
            {"parts": [{"kind": "seat", "role": "flex", "drawn": "plain"}]},
            "only at the ends",
        ),
        ({"parts": [{"kind": "seat", "role": "flex"}]}, "needs `words` or `drawn`"),
        ({"colour": "red"}, "unknown field"),
    ],
)
def test_a_run_fit_entry_that_does_not_hold_together_is_refused(
    tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    broken = {**BENCH, "run_fit": {**BENCH["run_fit"], **change}}
    with pytest.raises(RegistryError, match=message):
        _registry_with(tmp_path, broken)


def test_only_the_cabinet_has_a_rule_today() -> None:
    assert [p.id for p in registry().products if p.run_fit is not None] == ["cabinet"]
    assert "client lead's worked cabinet sheets" in CABINET.run_fit.source


# ---- the answer ----------------------------------------------------------------------------------


def test_it_is_code_made_and_filed_under_its_product() -> None:
    assert rf.CREATED_BY in CODE_MADE
    design = cabinet_run(F(3), F(24), [F(16)] * 3)
    site = cabinet_run(F(3), F(24), [F(14)] * 3)
    made = _findings(run_sheet(VENDOR, "vendor", site), run_sheet(ARCHITECT, "architect", design))[
        0
    ]
    assert refiled(made, ProductScope.of(["cabinet"])) == "cabinet"
    assert refiled(made, ProductScope.of(["countertop"])) is None
    assert not escalation.undecided({**made, "status": "different"})


class _Book:
    main_file = VENDOR

    def __init__(self, live: list[dict[str, Any]]) -> None:
        self._live = {str(f["id"]): f for f in live}
        self.added: list[dict[str, Any]] = []

    def live(self) -> list[dict[str, Any]]:
        return list(self._live.values())

    def add_code_finding(self, given: dict[str, Any], *, created_by: str) -> dict[str, Any]:
        kept = {k: v for k, v in given.items() if k not in ("settled", "checks", "run_fit")}
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
    def __init__(self, live: list[dict[str, Any]]) -> None:
        self.settings = type("S", (), {"mode": "full_check"})()
        workspace = type("W", (), {"set_pack": Path("pack"), "uploads": Path("uploads")})()
        self.tools = type("T", (), {"workspace": workspace})()
        self.book = _Book(live)
        self.finding_lock = threading.RLock()
        self.dropped: list[tuple[str, str]] = []

    def _show_finding(self, finding: dict[str, Any]) -> None:
        pass

    def _show_dropped(self, finding_id: str, reason: str) -> None:
        self.dropped.append((finding_id, reason))


def test_the_hook_replaces_an_item_on_a_label_the_rule_decided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    design = cabinet_run(F(3), F(24), [F(16)] * 3)
    site = cabinet_run(F(3), F(24), [F(14)] * 3)
    found = _findings(run_sheet(VENDOR, "vendor", site), run_sheet(ARCHITECT, "architect", design))
    monkeypatch.setattr(rf, "check_pack", lambda *a, **k: found)
    door = next(f for f in found if "door cabinet 1" in f["item"])
    label = door["dims"]["vendor"]
    red = {
        "id": "F7",
        "status": "different",
        "source": "architect",
        "dims": {"vendor": {"id": label["id"], "page": 1, "file": VENDOR}},
    }
    engine = _Engine([red])
    assert rf.add_to_answer(engine) == {"added": len(found), "replaced": 1}
    assert engine.dropped[0][0] == "F7" and 'should be 14"' in engine.dropped[0][1]
    assert (VENDOR, 1, label["id"]) in rf.covered_labels(engine.book.live())


def test_the_simple_check_drops_its_compare_items_on_a_label_the_rule_decided() -> None:
    """A door the site made narrower is never red against the architect's size in the simple
    check either: its compare item on that label is left out (and no blind look is paid)."""
    from types import SimpleNamespace

    from app.review.assistant.simple_check import run as run_module

    design = cabinet_run(F(3), F(24), [F(16)] * 3)
    site = cabinet_run(F(3), F(24), [F(14)] * 3)
    found = _findings(run_sheet(VENDOR, "vendor", site), run_sheet(ARCHITECT, "architect", design))
    door = next(f for f in found if "door cabinet 1" in f["item"])
    book = _Book([{**door, "id": "P1"}])
    fake = SimpleNamespace(engine=SimpleNamespace(book=book))
    fake._label_key = lambda one: run_module._Run._label_key(fake, one)
    decided = SimpleNamespace(vendor={"id": door["dims"]["vendor"]["id"], "page": 1})
    other = SimpleNamespace(vendor={"id": "D99", "page": 1})
    kept = run_module._Run._without_run_fit(fake, [(decided, "a"), (other, "b")])
    assert kept == [(other, "b")]
