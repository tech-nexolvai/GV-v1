"""A back splash's length against the top's run between two walls (Anant, 2026-10-25): made-up
drawings, no client data.

The reference (3 points to the inch) draws a vanity in plan in an alcove: the top's run is printed
on a dimension line above it, and a wall is drawn at each end of the run as a band (two parallel
solid lines 4" apart, running down past the top). The vendor (4 points to the inch) prints its
back splash's length as one overall. #1918 never settles a splash size against a size code cannot
see printed as the splash's; this one case may be compared, and only when code sees a wall at both
ends of the reference's run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.review.assistant import dim_ends, proof, splash_views
from app.review.assistant.chat_trust import judged
from app.review.assistant.escalation import EscalationContext, escalate

type Point = tuple[float, float]
type Segment = tuple[Point, Point]

VENDOR = "vendor.pdf"
REFERENCE = "reference.pdf"
ROW = 200.0


@dataclass
class Drawing:
    side: str
    scale: float
    x0: float = 100.0
    segments: list[Segment] = field(default_factory=list)
    labels: list[dict[str, Any]] = field(default_factory=list)

    def x(self, inches: float) -> float:
        return self.x0 + inches * self.scale

    def line(self, a: Point, b: Point) -> None:
        self.segments.append((a, b))

    def dimension(self, stations: list[float], row: float) -> None:
        """One dimension line through `stations` (inches), a slash tick and a short extension
        line at each."""
        xs = [self.x(at) for at in stations]
        self.line((xs[0] - 0.5, row), (xs[-1] + 0.5, row))
        for at in xs:
            self.line((at - 1.2, row + 1.2), (at + 1.2, row - 1.2))
            self.line((at, row - 3.5), (at, row + 15.0))

    def wall(self, near_in: float, outward: int) -> None:
        """A wall in plan whose near face is at `near_in`: two solid lines 4" apart, 30" long."""
        for at in (near_in, near_in + 4 * outward):
            self.line((self.x(at), ROW + 10.0), (self.x(at), ROW + 100.0))

    def label(self, text: str, inches: float, a_in: float, b_in: float, row: float) -> str:
        key = f"D{len(self.labels) + 1}"
        centre = (self.x(a_in) + self.x(b_in)) / 2
        self.labels.append(
            {
                "id": key,
                "text": text,
                "inches": str(inches),
                "box": [centre - 8, row - 11, centre + 8, row - 2],
                "side": self.side,
                "orientation": "h",
                "route": "text",
            }
        )
        return key


def reference(*, left_wall: bool = True, filler: bool = False, chain: bool = False) -> Drawing:
    """The top's run, 60" (printed, or 1" + 59" with no overall), between two walls."""
    d = Drawing("architect", 3.0)
    if chain:
        d.dimension([0, 1, 60], ROW)
        d.label("1499 [59]", 59, 1, 60, ROW)  # D1
        d.label("25 [1]", 1, -1.5, 2.5, ROW)  # D2, its 1" just outside its ticks
        d.labels[1]["box"] = [99.0, ROW - 14, 110.0, ROW - 3]
    else:
        d.dimension([0, 60], ROW)
        d.label("1524 [60]", 60, 0, 60, ROW)  # D1
    if left_wall:
        d.wall(0, -1)
    d.wall(60, 1)
    if filler:
        # A 4" filler dimensioned beyond the left end: its far extension line is no wall.
        d.dimension([-4, 0], ROW - 30.0)
        d.label('4"', 4, -4, 0, ROW - 30.0)
    return d


def vendor(value: float = 60) -> Drawing:
    d = Drawing("vendor", 4.0)
    d.dimension([0, value], 120.0)
    d.label(f'{value:g}"', value, 0, value, 120.0)  # D1
    return d


def _finding(
    vendor_value: float = 60,
    *,
    item: str = "Vanity: back splash length",
    architect_value: float = 60,
    architect_text: str = "1524 [60]",
    status: str = "check",
) -> dict[str, Any]:
    v, a = f"{vendor_value:g}", f"{architect_value:g}"
    return {
        "id": "F1",
        "page": 1,
        "vs_page": 1,
        "item": item,
        "vendor": f'{v}"',
        "architect": architect_text,
        "status": status,
        "source": "architect",
        "category": "countertop",
        "created_by": "helper:1",
        "inches": {"vendor": v, "architect": a, "difference": str(vendor_value - architect_value)},
        "checks": {"printed_vendor": "match", "printed_architect": "match"},
        # The spots the AI named: around each label (the vendor's at y 120, the reference's at 200).
        "vendor_box": {"x0": 90, "y0": 100, "x1": 360, "y1": 125, "file": VENDOR, "page": 1},
        "arch_box": {"x0": 90, "y0": 180, "x1": 290, "y1": 205, "file": REFERENCE, "page": 1},
        "dims": {
            "vendor": {"id": "D1", "page": 1, "file": VENDOR, "inches": v, "route": "text"},
            "architect": {
                "id": "D1",
                "page": 1,
                "file": REFERENCE,
                "inches": a,
                "route": "text",
                "text": architect_text,
            },
        },
    }


def _ctx(ref: Drawing, ven: Drawing) -> EscalationContext:
    labels = {(VENDOR, 1): ven.labels, (REFERENCE, 1): ref.labels}
    lines = {(VENDOR, 1): ven.segments, (REFERENCE, 1): ref.segments}
    phrases: dict[tuple[str, int], list[dict[str, Any]]] = {}
    return EscalationContext(
        main_file=VENDOR,
        labels=labels,
        phrases=phrases,
        pages=dim_ends.pages_reader(labels, phrases, lambda f, p: lines.get((f, p)), None, VENDOR),
    )


def _escalated(finding: dict[str, Any], ref: Drawing, ven: Drawing) -> dict[str, Any]:
    result = escalate([finding], _ctx(ref, ven))
    changed = {str(f["id"]): f for f in result.changed}
    return judged(changed.get("F1", finding))


def _how(found: dict[str, Any]) -> str:
    return " | ".join(str(s.get("how")) for s in found.get("escalation", {}).get("steps", []))


# ---- the walls -------------------------------------------------------------------------------------


def test_a_run_between_two_drawn_walls_is_wall_to_wall() -> None:
    page = dim_ends.page_of(REFERENCE, 1, reference().labels, [], reference().segments)
    ends = page.ends("D1")
    assert ends is not None
    assert dim_ends.wall_to_wall(page, ends) == (True, "code sees a wall drawn at both ends of it")


def test_a_free_end_is_no_wall() -> None:
    d = reference(left_wall=False)
    page = dim_ends.page_of(REFERENCE, 1, d.labels, [], d.segments)
    ends = page.ends("D1")
    assert ends is not None
    walls, why = dim_ends.wall_to_wall(page, ends)
    assert not walls and "no wall drawn at one end" in why


def test_two_extension_lines_a_filler_apart_are_no_wall() -> None:
    d = reference(left_wall=False, filler=True)
    # The filler's extension lines, made as long as a wall's faces.
    d.line((d.x(-4), ROW + 10.0), (d.x(-4), ROW + 100.0))
    d.line((d.x(0), ROW + 10.0), (d.x(0), ROW + 100.0))
    page = dim_ends.page_of(REFERENCE, 1, d.labels, [], d.segments)
    ends = page.ends("D1")
    assert ends is not None
    assert dim_ends.wall_band(page, ends.span, ends.span.a, -1)  # the band alone would pass
    assert dim_ends.wall_to_wall(page, ends)[0] is False


@pytest.mark.parametrize("kind", sorted(dim_ends.NOT_WALL))
def test_an_end_known_as_anything_else_is_no_wall(kind: str) -> None:
    d = reference()
    page = dim_ends.page_of(REFERENCE, 1, d.labels, [], d.segments)
    ends = page.ends("D1")
    assert ends is not None
    marked = dim_ends.Ends(ends.span, ends.low, dim_ends.End(ends.high.at, frozenset({kind})))
    walls, why = dim_ends.wall_to_wall(page, marked)
    assert not walls and "not a wall" in why


# ---- the vendor's words ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "item",
    ["Vanity: back splash length", "Vanity: splash length", "Vanity: backsplash width"],
)
def test_a_back_splash_length_may_be_compared(item: str) -> None:
    assert splash_views.wall_run_part(_finding(item=item)) is not None


@pytest.mark.parametrize(
    "item",
    [
        "Vanity: side splash length",
        "Vanity: side and back splash length",
        "Vanity: back splash height",
        "Vanity: back splash thickness",
        "Vanity: splash",
    ],
)
def test_no_other_splash_size_is_compared(item: str) -> None:
    assert splash_views.wall_run_part(_finding(item=item)) is None


def test_an_architect_size_printed_as_a_splash_s_is_left_to_the_like_for_like_rule() -> None:
    printed = _finding(architect_text='60" BACKSPLASH')
    assert splash_views.wall_run_part(printed) is None


# ---- the escalation ----------------------------------------------------------------------------------


def test_the_same_length_as_a_wall_to_wall_run_is_right() -> None:
    found = _escalated(_finding(60), reference(), vendor(60))
    assert found["status"] == "same"
    assert proof.proof_group(found) == "right"
    assert found["settled"]["method"] == "escalation_labels"
    assert found["splash_views"]["by"] == splash_views.WALL_RUN
    assert "from wall to wall" in found["settled"]["how"]
    # The like-for-like step that runs on Rights keeps it.
    assert splash_views.downgrade_unlike(found, None) is None


def test_a_chain_from_wall_to_wall_is_added_up_for_the_splash() -> None:
    finding = _finding(60, architect_value=59, architect_text="1499 [59]", status="different")
    found = _escalated(finding, reference(chain=True), vendor(60))
    assert found["status"] == "same"
    assert found["architect"] == '1" + 59" = 60"'
    assert found["settled"]["method"] == "escalation_chain_sums"
    assert found["splash_views"]["by"] == splash_views.WALL_RUN


def test_a_run_with_a_free_end_is_never_compared_with_the_splash() -> None:
    found = _escalated(_finding(60), reference(left_wall=False), vendor(60))
    assert found["status"] == "check"
    assert proof.proof_group(found) == "not_compared"
    assert "not a run from wall to wall" in _how(found)


def test_a_chain_with_a_free_end_is_never_compared_with_the_splash() -> None:
    finding = _finding(60, architect_value=59, architect_text="1499 [59]", status="different")
    found = _escalated(finding, reference(chain=True, left_wall=False), vendor(60))
    assert found["status"] == "different"
    assert "architect_parts" not in found
    assert "not a run from wall to wall" in _how(found)


@pytest.mark.parametrize(
    "item", ["Vanity: side splash length", "Vanity: back splash height", "Vanity: splash"]
)
def test_no_other_splash_size_is_settled_on_the_run(item: str) -> None:
    found = _escalated(_finding(60, item=item), reference(), vendor(60))
    assert found["status"] == "check"
    assert "not like for like" in _how(found)


def test_a_different_length_changes_nothing_and_is_never_red() -> None:
    found = _escalated(_finding(59, architect_value=60), reference(), vendor(59))
    assert found["status"] == "check"
    assert proof.proof_group(found) not in ("right", "clouded")


def test_a_vendor_end_known_as_a_field_cut_is_never_compared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = dim_ends.Page.ends

    def ends(self: dim_ends.Page, label_id: object) -> dim_ends.Ends | None:
        found = real(self, label_id)
        if found is None or self.file != VENDOR:
            return found
        cut = dim_ends.End(found.low.at, frozenset({"field_cut_end"}))
        return dim_ends.Ends(found.span, cut, found.high)

    monkeypatch.setattr(dim_ends.Page, "ends", ends)
    found = _escalated(_finding(60), reference(), vendor(60))
    assert found["status"] == "check"
    assert "field-cut end" in _how(found)


# ---- review fixes ------------------------------------------------------------------------------------


def test_the_top_s_free_edge_is_no_wall() -> None:
    free = dim_ends.End(0.0, frozenset({"top_edge"}))
    at_wall = dim_ends.End(0.0, frozenset({"top_edge", "wall"}))
    assert dim_ends.not_a_wall(free) == dim_ends.KIND_WORDS["top_edge"]
    assert dim_ends.not_a_wall(at_wall) is None


@pytest.mark.parametrize("item", ["Vanity: return splash length", "Vanity: end splash length"])
def test_a_splash_at_the_top_s_end_in_other_words_is_never_compared(item: str) -> None:
    assert splash_views.wall_run_part(_finding(item=item)) is None


def test_a_label_kept_from_an_older_reading_is_never_proved_on() -> None:
    ref = reference()
    ref.dimension([0, 30], ROW + 120.0)
    ref.label("762 [30]", 30, 0, 30, ROW + 120.0)  # D2, outside the AI's spot
    finding = _finding(60)
    finding["dims"]["architect"]["id"] = "D2"  # an older reading's label, not the 60" compared
    found = _escalated(finding, ref, vendor(60))
    assert found["status"] == "check"
    assert "not the size compared" in _how(found)


def test_a_wall_run_right_paired_again_with_other_labels_is_not_kept() -> None:
    found = _escalated(_finding(60), reference(), vendor(60))
    assert splash_views.downgrade_unlike(found, None) is None
    moved = dict(
        found, dims=dict(found["dims"], architect=dict(found["dims"]["architect"], id="D9"))
    )
    down = splash_views.downgrade_unlike(moved, None)
    assert down is not None and down["status"] == "check"
