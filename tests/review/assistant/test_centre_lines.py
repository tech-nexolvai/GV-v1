"""An architect dimension measured to a centre line is never a size of the item
(`centre_lines`): made-up drawings, no client data.

The architect's bath elevation (3 points to the inch) draws a 58" niche wall to wall. A 30" vanity
stands against the left wall; a toilet's centre line runs 40" from the left wall, drawn dashed with
a "C L" mark on it. Above the elevation one row of dimensions: 2'-6" (the wall to the vanity's
edge: the vanity's own width), 0'-10" (the vanity's edge to the toilet's centre line) and 1'-6"
(the toilet's centre line to the right wall). The vanity's edge is drawn dashed too, with no mark:
a dashed extension line is not a centre line.

The vendor's vanity top plan (4 points to the inch) prints the top's width, 30", and its depth,
18", each between the top's own edges.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from app.review.assistant import centre_lines, classification, dim_ends, proof
from app.review.assistant.chat_trust import judged
from app.review.assistant.escalation import EscalationContext, escalate

type Point = tuple[float, float]
type Segment = tuple[Point, Point]

VENDOR = "vendor.pdf"
ARCHITECT = "architect.pdf"


class Drawing:
    """One page: inches to points through its own origin and scale."""

    def __init__(self, side: str, scale: float, x0: float = 100.0) -> None:
        self.side, self.scale, self.x0 = side, scale, x0
        self.segments: list[Segment] = []
        self.labels: list[dict[str, Any]] = []
        self.phrases: list[dict[str, Any]] = []

    def x(self, inches: float) -> float:
        return self.x0 + inches * self.scale

    def line(self, a: Point, b: Point) -> None:
        self.segments.append((a, b))

    def dashes(
        self, x: float, top: float, bottom: float, dash: float = 4.5, gap: float = 4.5
    ) -> None:
        """An upright dashed line at `x` from `top` down to `bottom`."""
        y = top
        while y < bottom:
            self.line((x, y), (x, min(y + dash, bottom)))
            y += dash + gap

    def dash_dot(self, x: float, top: float, bottom: float) -> None:
        """An upright dash-dot line (long 9, short 2, gaps 2) at `x` from `top` down."""
        y, long = top, True
        while y < bottom:
            size = 9.0 if long else 2.0
            self.line((x, y), (x, min(y + size, bottom)))
            y += size + 2.0
            long = not long

    def phrase(self, text: str, x: float, y: float) -> None:
        half = 3 * len(text) / 2
        self.phrases.append({"text": text, "box": [x - half, y - 4, x + half, y + 4]})

    def dim(
        self, a_in: float, b_in: float, row: float, value: float, text: str, **more: Any
    ) -> str:
        """A ticked dimension line from a_in to b_in at `row`, its text centred above it."""
        a, b = self.x(a_in), self.x(b_in)
        self.line((a, row), (b, row))
        for at in (a, b):
            self.line((at - 1.2, row + 1.2), (at + 1.2, row - 1.2))
        key = f"D{len(self.labels) + 1}"
        centre = (a + b) / 2
        self.labels.append(
            {
                "id": key,
                "text": text,
                "inches": str(value),
                "box": [centre - 8, row - 11, centre + 8, row - 2],
                "side": self.side,
                "orientation": "h",
                "route": "text",
                "dim_line": [[a, row], [b, row]],
                "scale_ok": True,
                **more,
            }
        )
        return key


ROW = 80.0
FLOOR = 400.0


def architect(*, centre: str = "mark", vanity_edge: str = "dashes") -> Drawing:
    """The bath elevation: walls at 0" and 58", the vanity 0"-30", the toilet's centre line at
    40" (`centre`: "mark" = dashed with a C L mark, "dash_dot", "plain" = dashed, no mark)."""
    d = Drawing("architect", 3.0)
    for wall in (0, 58):
        d.line((d.x(wall), ROW - 3), (d.x(wall), FLOOR))
    d.line((d.x(0), FLOOR), (d.x(58), FLOOR))
    d.line((d.x(0), 300), (d.x(30), 300))  # the vanity's top
    if vanity_edge == "dashes":
        d.dashes(d.x(30), ROW, 300)
    else:
        d.line((d.x(30), ROW - 3), (d.x(30), FLOOR))
    if centre == "dash_dot":
        d.dash_dot(d.x(40), ROW, FLOOR)
    else:
        d.dashes(d.x(40), ROW, FLOOR)
    if centre == "mark":
        d.phrase("C L", d.x(40), 112)
    d.dim(0, 30, ROW, 30, "2' - 6\"")  # D1: the vanity's width
    d.dim(30, 40, ROW, 10, "0' - 10\"")  # D2: the vanity's edge to the toilet's centre line
    d.dim(40, 58, ROW, 18, "1' - 6\"")  # D3: the toilet's centre line to the wall
    return d


def vendor(*, sink_mark: bool = False) -> Drawing:
    """The vanity top in plan: 30" wide (D1), 18" deep (D2), each between its own edges; with
    `sink_mark`, also the sink's centre 18" from the left end (D3), marked "CL SINK"."""
    d = Drawing("vendor", 4.0)
    top, front = 200.0, 200.0 + 18 * 4.0
    for x in (0, 30):
        d.line((d.x(x), ROW - 3), (d.x(x), front))
    d.line((d.x(0), top), (d.x(30), top))
    d.line((d.x(0), front), (d.x(30), front))
    d.dim(0, 30, ROW, 30, '30"')
    d.labels.append(
        {
            "id": "D2",
            "text": '18"',
            "inches": "18",
            "box": [40, 230, 52, 240],
            "side": "vendor",
            "orientation": "v",
            "route": "text",
            "dim_line": [[60.0, top], [60.0, front]],
            "scale_ok": True,
        }
    )
    d.line((60.0, top), (60.0, front))
    if sink_mark:
        d.dashes(d.x(18), ROW - 40, front)
        d.phrase("CL SINK", d.x(18), ROW + 12)
        d.dim(0, 18, ROW - 40, 18, '18"')  # D3
    return d


def finding(
    vendor_id: str,
    architect_id: str,
    vendor_value: str,
    architect_value: str,
    status: str,
    **more: Any,
) -> dict[str, Any]:
    def inches(text: str) -> int | None:
        if "'" in text:
            feet, rest = text.split("'")
            return int(feet) * 12 + int(rest.strip(' -"') or 0)
        found = re.search(r"\d+", text)
        return int(found.group()) if found else None

    a, b = inches(vendor_value), inches(architect_value)
    apart = None if a is None or b is None else str(abs(a - b))
    return {
        "id": "F1",
        "page": 1,
        "vs_page": 1,
        "item": "Vanity: top depth",
        "vendor": vendor_value,
        "architect": architect_value,
        "status": status,
        "source": "architect",
        "category": "countertop",
        "created_by": "helper:1",
        "inches": {"vendor": str(a), "architect": str(b), "difference": apart},
        "checks": {"printed_vendor": "match", "printed_architect": "match"},
        "witnesses": ["printed"],
        "dims": {
            "vendor": {"id": vendor_id, "page": 1, "file": VENDOR, "text": vendor_value},
            "architect": {
                "id": architect_id,
                "page": 1,
                "file": ARCHITECT,
                "text": architect_value,
            },
        },
        **more,
    }


def context(v: Drawing, a: Drawing) -> EscalationContext:
    labels = {(VENDOR, 1): v.labels, (ARCHITECT, 1): a.labels}
    phrases = {(VENDOR, 1): v.phrases, (ARCHITECT, 1): a.phrases}
    lines = {(VENDOR, 1): v.segments, (ARCHITECT, 1): a.segments}
    return EscalationContext(
        main_file=VENDOR,
        labels=labels,
        phrases=phrases,
        pages=dim_ends.pages_reader(labels, phrases, lambda f, p: lines.get((f, p)), None, VENDOR),
    )


def escalated(
    item: dict[str, Any], v: Drawing | None = None, a: Drawing | None = None
) -> dict[str, Any]:
    result = escalate([item], context(v or vendor(), a or architect()))
    changed = {str(f["id"]): f for f in result.changed}
    return judged(changed.get("F1", item))


def page(d: Drawing, file: str) -> dim_ends.Page:
    return dim_ends.page_of(file, 1, d.labels, d.phrases, d.segments)


# ---- what code reads at a dimension's ends -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["CL", "C L", "C/L", "C / L", "C.L.", "℄", "CENTER LINE", "CENTRELINE", "CL SINK", "C/L OF WC"],
)
def test_centre_line_marks(text: str) -> None:
    assert centre_lines.is_mark(text)


@pytest.mark.parametrize(
    "text",
    ["CLG", "CLOSET", "CAB", "CL-1", "PT-10", "CIRCLE", "ALIGN CL OF SINK WITH CL OF MIRROR", ""],
)
def test_not_centre_line_marks(text: str) -> None:
    assert not centre_lines.is_mark(text)


def test_a_dash_dot_line_is_long_and_short_dashes_in_turn() -> None:
    pattern = [(0, 9), (11, 13), (15, 24), (26, 28), (30, 39), (41, 43)]
    assert centre_lines.dash_dot(pattern)
    even = [(0, 4.5), (9, 13.5), (18, 22.5), (27, 31.5), (36, 40.5), (45, 49.5)]
    assert not centre_lines.dash_dot(even)  # plain dashes: an extension line or a hidden edge
    mixed = [(0, 51), (54, 84), (90, 92), (95, 140), (141, 143), (150, 160)]
    assert not centre_lines.dash_dot(mixed)  # pieces of drawn things in line, no pattern
    # A solid line drawn over a dash-dot line joins its long dashes into runs of different
    # lengths: two long runs agreeing by chance are no pattern.
    covered = [(0, 193), (195, 204), (206, 422), (424, 433), (435, 732), (734, 743), (745, 880)]
    assert not centre_lines.dash_dot(covered)


def test_the_ends_on_a_marked_centre_line_are_found() -> None:
    found = page(architect(), ARCHITECT)
    assert centre_lines.centre_ends(found, "D1", architect().phrases) == ()  # dashed, no mark
    to_centre = centre_lines.centre_ends(found, "D3", architect().phrases)
    assert to_centre is not None and [end.side for end in to_centre] == ["a"]
    assert '"C L"' in to_centre[0].how
    between = centre_lines.centre_ends(found, "D2", architect().phrases)
    assert between is not None and [end.side for end in between] == ["b"]


def test_a_dash_dot_line_with_no_mark_is_a_centre_line() -> None:
    drawn = architect(centre="dash_dot")
    found = centre_lines.centre_ends(page(drawn, ARCHITECT), "D3", drawn.phrases)
    assert found is not None and len(found) == 1
    assert "dash-dot" in found[0].how


def test_plain_dashes_with_no_mark_are_not_a_centre_line() -> None:
    drawn = architect(centre="plain")
    assert centre_lines.centre_ends(page(drawn, ARCHITECT), "D3", drawn.phrases) == ()


def test_a_mark_on_another_line_is_not_this_end_s() -> None:
    drawn = architect(centre="plain")
    drawn.phrase("C L", drawn.x(46), 112)  # a mark 6" along: centred on no end's line
    assert centre_lines.centre_ends(page(drawn, ARCHITECT), "D3", drawn.phrases) == ()


def test_a_label_with_no_line_is_unknown() -> None:
    drawn = architect()
    assert centre_lines.centre_ends(page(drawn, ARCHITECT), "D9", drawn.phrases) is None


# ---- the outcome ---------------------------------------------------------------------------------


def test_a_centre_line_dimension_never_makes_a_right() -> None:
    # Set A's case (made-up values): the vendor's 18" depth against the architect's 1'-6" from the
    # toilet's centre line to the wall. Equal numbers, different things: not a green tick.
    found = escalated(finding("D2", "D3", '18"', "1' - 6\"", "same"))
    assert proof.proof_group(found) == "not_compared"
    said = classification.classify(found, proved=False)
    assert said.outcome == "not_compared"
    assert said.reason == "different_reference"
    assert said.why is not None
    assert "the architect's 1' - 6\" is measured to a centre line" in said.why
    assert found[centre_lines.FIELD]["architect"] == f"{ARCHITECT}|1|D3"


def test_without_the_rule_the_same_pair_was_a_right() -> None:
    drawn = architect(centre="plain")  # the same line, not drawn as a centre line
    found = escalated(finding("D2", "D3", '18"', "1' - 6\"", "same"), a=drawn)
    assert proof.proof_group(found) == "right"
    assert centre_lines.FIELD not in found


@pytest.mark.parametrize("status", ["different", "check"])
def test_a_centre_line_dimension_is_never_a_difference_or_a_please_check(status: str) -> None:
    item = finding("D2", "D3", '22"', "1' - 6\"", status)
    item["checks"].update({"second_read": "agrees", "critic": "passed"})
    found = escalated(item)
    assert proof.proof_group(found) == "not_compared"
    assert not proof.code_proved(found)
    assert dim_ends.unlike_reason(found) is not None


def test_a_dash_dot_centre_line_is_not_compared_either() -> None:
    found = escalated(
        finding("D2", "D3", '18"', "1' - 6\"", "same"), a=architect(centre="dash_dot")
    )
    assert proof.proof_group(found) == "not_compared"


def test_the_vanity_s_own_width_still_compares() -> None:
    # The architect's 2'-6" runs from the wall to the vanity's edge (a dashed extension line, no
    # mark): a real size of the vanity, compared as before.
    found = escalated(finding("D1", "D1", '30"', "2' - 6\"", "same"))
    assert proof.proof_group(found) == "right"
    assert centre_lines.FIELD not in found


def test_both_dimensions_to_a_centre_line_are_left_to_the_other_checks() -> None:
    # The vendor's 18" also runs to a centre line (its sink's, marked "CL SINK"): like with like.
    found = escalated(finding("D3", "D3", '18"', "1' - 6\"", "same"), v=vendor(sink_mark=True))
    assert proof.proof_group(found) == "right"
    assert centre_lines.FIELD not in found


def test_an_equal_split_runs_to_a_centre_too() -> None:
    drawn = vendor()
    drawn.labels[1] |= {"text": "EQ", "derived": {"rule": "equal_parts", "from": "D9", "parts": 2}}
    item = finding("D2", "D3", 'EQ (18")', "1' - 6\"", "different")
    item["dims"]["vendor"]["derived"] = {"rule": "equal_parts", "from": "D9", "parts": 2}
    found = escalated(item, v=drawn)
    assert centre_lines.FIELD not in found
    assert centre_lines.reason(found) is None


def test_a_thing_the_vendor_leaves_out_is_its_own_item() -> None:
    # The architect locates the toilet's centre line; the vendor does not show it. The locating
    # dimension IS the item ("not shown on the vendor"), never "not a size to compare".
    item = finding("D2", "D3", "not shown on the vendor", "1' - 6\"", "check")
    item["dims"].pop("vendor")
    found = escalated(item)
    assert centre_lines.FIELD not in found


@pytest.mark.parametrize(
    "extra",
    [
        {"paired_by": "code"},
        {"decision": {"decision": "agree"}},
        {"created_by": "sink_centre"},
        {"formula": {"id": "top_depth", "outcome": "same"}},
    ],
)
def test_items_code_compared_or_the_reviewer_decided_are_left(extra: dict[str, Any]) -> None:
    item = finding("D2", "D3", '18"', "1' - 6\"", "same") | extra
    pages = context(vendor(), architect()).pages
    assert pages is not None
    record = centre_lines.judge(item, pages, {})
    assert record is None
    marked = item | {
        centre_lines.FIELD: {
            "outcome": "centre_line",
            "why": "measured to a centre line",
            "vendor": f"{VENDOR}|1|D2",
            "architect": f"{ARCHITECT}|1|D3",
        }
    }
    assert centre_lines.reason(marked) is None


def test_the_record_holds_only_for_the_labels_it_was_made_for() -> None:
    found = escalated(finding("D2", "D3", '18"', "1' - 6\"", "same"))
    assert centre_lines.reason(found) is not None
    moved = dict(found)
    moved["dims"] = {
        **found["dims"],
        "architect": {**found["dims"]["architect"], "id": "D1", "text": "2' - 6\""},
    }
    assert centre_lines.reason(moved) is None


def test_a_line_that_is_no_longer_a_centre_line_takes_the_record_off() -> None:
    marked = escalated(finding("D2", "D3", '18"', "1' - 6\"", "same"))
    assert centre_lines.FIELD in marked
    again = escalated(marked, a=architect(centre="plain"))
    assert centre_lines.FIELD not in again
    assert proof.proof_group(again) == "right"


def test_no_line_work_leaves_every_item_as_it_was() -> None:
    item = finding("D2", "D3", '18"', "1' - 6\"", "same")
    result = escalate([item], EscalationContext(main_file=VENDOR))
    assert all(centre_lines.FIELD not in f for f in result.changed)
