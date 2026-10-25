"""A reference's chain of dimensions added up like a printed overall (fix 4): made-up drawings, no
client data.

The reference (3 points to the inch) prints a vanity top as a chain on one dimension line: a 1"
overhang beside a 35" cabinet, the 1"'s text printed just outside its two extension lines (over
the 35"'s line), and no overall. A second view prints the same chain with the 1"'s text taken out
of the way on a leader. The vendor (4 points to the inch) prints the top as one 36" overall.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.review.assistant import dim_ends, dim_labels, proof
from app.review.assistant.chat_trust import judged
from app.review.assistant.escalation import EscalationContext, escalate

type Point = tuple[float, float]
type Segment = tuple[Point, Point]

VENDOR = "vendor.pdf"
REFERENCE = "reference.pdf"


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

    def chain(self, stations: list[float], row: float, *, run_on: float = 0.5) -> None:
        """One dimension line through `stations` (inches), a slash tick and an extension line at
        each, the line running `run_on` points past its two ends."""
        xs = [self.x(at) for at in stations]
        self.line((xs[0] - run_on, row), (xs[-1] + run_on, row))
        for at in xs:
            self.line((at - 1.2, row + 1.2), (at + 1.2, row - 1.2))
            self.line((at, row - 3.5), (at, row + 15.0))

    def label(self, text: str, inches: float, box: list[float]) -> str:
        key = f"D{len(self.labels) + 1}"
        self.labels.append(
            {
                "id": key,
                "text": text,
                "inches": str(inches),
                "box": box,
                "side": self.side,
                "orientation": "h",
                "route": "text",
            }
        )
        return key

    def over(self, text: str, inches: float, a_in: float, b_in: float, row: float) -> str:
        """A label centred over its span, above the line."""
        centre = (self.x(a_in) + self.x(b_in)) / 2
        return self.label(text, inches, [centre - 8, row - 11, centre + 8, row - 2])


def reference(*, leader: bool = False, overall: bool = False, run_on: float = 0.5) -> Drawing:
    """1" + 35" on one line at y 200 (its 1" beside its ticks, or on a leader)."""
    d = Drawing("architect", 3.0)
    d.chain([0, 1, 36], 200.0, run_on=run_on)
    d.over("889 [35]", 35, 1, 36, 200.0)  # D1
    if leader:
        # The 1"'s text below and right of the line, a leader from the middle of its ticks.
        d.label("25 [1]", 1, [112.0, 225.0, 124.0, 234.0])  # D2
        d.line((d.x(0.5), 200.0), (112.5, 228.0))
    else:
        # The 1"'s text just outside its ticks, its middle over the 35"'s line.
        d.label("25 [1]", 1, [99.0, 186.0, 110.0, 197.0])  # D2
    if overall:
        d.chain([0, 36], 170.0)
        d.over('36"', 36, 0, 36, 170.0)  # D3
    return d


def vendor(value: float = 36) -> Drawing:
    d = Drawing("vendor", 4.0)
    d.chain([0, value], 120.0)
    d.over(f'{value:g}"', value, 0, value, 120.0)  # D1
    return d


def _page(d: Drawing, file: str) -> dim_ends.Page:
    return dim_ends.page_of(file, 1, d.labels, [], d.segments)


# ---- the small dimension's own span ----------------------------------------------------------------


def test_a_small_dimension_printed_beside_its_ticks_gets_its_own_span() -> None:
    page = _page(reference(), REFERENCE)
    one = page.spans["D2"]
    assert (one.a, one.b) == (100.0, 103.0)
    assert page.spans["D1"].a == pytest.approx(103.0)


def test_a_small_dimension_on_a_leader_gets_the_span_at_the_leader_end() -> None:
    page = _page(reference(leader=True), REFERENCE)
    one = page.spans["D2"]
    assert (one.row, one.a, one.b) == (200.0, 100.0, 103.0)


def test_a_leader_to_nowhere_names_no_span() -> None:
    d = reference(leader=True)
    d.segments = [s for s in d.segments if s != ((d.x(0.5), 200.0), (112.5, 228.0))]
    d.line((118.0, 225.0), (140.0, 260.0))  # a stroke from the label to empty paper
    assert "D2" not in _page(d, REFERENCE).spans


# ---- the chain -----------------------------------------------------------------------------------


@pytest.mark.parametrize("leader", [False, True])
def test_one_chain_end_to_end_adds_up(leader: bool) -> None:
    chain = dim_ends.chain_of(_page(reference(leader=leader), REFERENCE), "D1")
    assert chain is not None
    assert [part.id for part in chain.parts] == ["D2", "D1"]
    assert chain.inches == 36


def test_no_chain_when_the_overall_is_printed() -> None:
    assert dim_ends.chain_of(_page(reference(overall=True), REFERENCE), "D1") is None


def test_no_chain_when_the_line_runs_on_past_its_end() -> None:
    # A part code has no label for (an unread 3/4") may lie past the end: the sum is not the run.
    assert dim_ends.chain_of(_page(reference(run_on=4.0), REFERENCE), "D1") is None


def test_no_chain_without_an_extension_line_at_the_joint() -> None:
    d = reference()
    joint = d.x(1)
    d.segments = [s for s in d.segments if not (s[0][0] == s[1][0] == joint)]
    assert dim_ends.chain_of(_page(d, REFERENCE), "D1") is None


def test_no_chain_without_the_line_work() -> None:
    d = reference()
    page = dim_ends.page_of(REFERENCE, 1, d.labels, [])
    assert dim_ends.chain_of(page, "D1") is None


# ---- the escalation step ---------------------------------------------------------------------------


def _finding(vendor_value: float = 36, item: str = "Vanity: top width") -> dict[str, Any]:
    v, a = f"{vendor_value:g}", "35"
    return {
        "id": "F1",
        "page": 1,
        "vs_page": 1,
        "item": item,
        "vendor": f'{v}"',
        "architect": "889 [35]",
        "status": "different",
        "source": "architect",
        "category": "countertop",
        "created_by": "helper:1",
        "inches": {"vendor": v, "architect": a, "difference": str(vendor_value - 35)},
        "checks": {"printed_vendor": "match", "printed_architect": "match"},
        "dims": {
            "vendor": {"id": "D1", "page": 1, "file": VENDOR, "inches": v, "route": "text"},
            "architect": {
                "id": "D1",
                "page": 1,
                "file": REFERENCE,
                "inches": a,
                "route": "text",
                "text": "889 [35]",
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


@pytest.mark.parametrize("leader", [False, True])
def test_the_chain_s_sum_equal_to_the_vendor_s_overall_is_right(leader: bool) -> None:
    found = _escalated(_finding(36), reference(leader=leader), vendor(36))
    assert found["status"] == "same"
    assert proof.proof_group(found) == "right"
    assert found["architect"] == '1" + 35" = 36"'
    assert found["architect_parts"]["chain_proved"] is True
    assert found["dims"]["architect"]["derived"]["sum_of"] == ["D2", "D1"]
    assert found["settled"]["method"] == "escalation_chain_sums"


def test_a_different_sum_changes_nothing_and_is_never_red() -> None:
    found = _escalated(_finding(37), reference(), vendor(37))
    assert found["status"] == "different"
    assert found["architect"] == "889 [35]"
    assert "architect_parts" not in found
    assert proof.proof_group(found) != "clouded"
    steps = {s["step"]: s for s in found["escalation"]["steps"]}
    assert "adds to 36" in steps["chain_sums"]["how"]


def test_a_printed_overall_is_never_replaced_by_the_sum() -> None:
    found = _escalated(_finding(36), reference(overall=True), vendor(36))
    assert found["status"] == "different"
    assert "architect_parts" not in found


def test_a_splash_size_is_never_settled_against_the_top_s_run() -> None:
    found = _escalated(_finding(36, item="Vanity: backsplash length"), reference(), vendor(36))
    assert found["status"] == "different"
    steps = {s["step"]: s for s in found["escalation"]["steps"]}
    assert steps["chain_sums"]["how"].startswith("not like for like")


def test_unlike_ends_are_never_added_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dim_ends, "alike", lambda *_a: ("unlike", "a sink's centre line"))
    found = _escalated(_finding(36), reference(), vendor(36))
    assert found["status"] == "different"
    assert "architect_parts" not in found


def test_a_part_read_as_drawn_ink_is_never_summed() -> None:
    ref = reference()
    ref.labels[1]["route"] = "drawn"
    found = _escalated(_finding(36), ref, vendor(36))
    assert found["status"] == "different"


# ---- the set pack's labels -----------------------------------------------------------------------


def test_the_set_pack_gives_a_small_label_its_own_line() -> None:
    d = reference()
    d.chain([0, 12], 260.0)
    labels: list[dict[str, Any]] = [
        dict(label, dim_line=None, extension=None, span_pt=None) for label in d.labels
    ]
    labels[0].update(dim_line=[[103.0, 200.0], [208.0, 200.0]], span_pt=105.0)
    # The 1" took the 35"'s line (the span under its middle).
    labels[1].update(dim_line=[[103.0, 200.0], [208.0, 200.0]], span_pt=105.0)
    # Two more drawn to scale (the drawing's scale needs three).
    for value, a, b, row in ((12, 0, 12, 260.0), (24, 0, 24, 300.0)):
        labels.append(
            {
                "id": "",
                "text": f'{value}"',
                "inches": str(value),
                "box": [d.x(a), row - 11, d.x(b), row - 2],
                "side": "architect",
                "orientation": "h",
                "route": "text",
                "dim_line": [[d.x(a), row], [d.x(b), row]],
                "span_pt": (b - a) * 3.0,
            }
        )
    dim_labels._short_lines(labels, d.segments)
    assert labels[1]["dim_line"] == [[100.0, 200.0], [103.0, 200.0]]
    assert labels[1]["span_pt"] == 3.0
    assert all(end for end in labels[1]["extension"])
    assert labels[0]["dim_line"] == [[103.0, 200.0], [208.0, 200.0]]  # drawn to its value: kept


def test_ticks_far_from_the_text_are_not_its_span() -> None:
    d = reference()
    d.labels[1]["box"] = [99.0, 178.0, 101.0, 186.0]  # a tiny 1" text 14 points above the line
    assert "D2" not in _page(d, REFERENCE).spans


def test_a_span_nearer_another_label_of_the_same_value_is_that_label_s() -> None:
    d = reference()
    d.label("25 [1]", 1, [98.0, 201.0, 104.0, 206.0])  # D3, right under the 1"'s ticks
    page = _page(d, REFERENCE)
    assert "D2" not in page.spans
    assert (page.spans["D3"].a, page.spans["D3"].b) == (100.0, 103.0)


# ---- GV's formula: top width = the chain's sum (final plan #9) -----------------------------------


def test_a_right_by_the_chain_shows_the_formula() -> None:
    found = _escalated(_finding(36), reference(), vendor(36))
    assert found["formula"]["kind"] == "formula_chain" and found["formula"]["outcome"] == "same"
    assert found["settled"]["how"].startswith("GV's formula (top width = ")


def test_a_different_sum_whose_ends_code_proved_is_the_formula_s_likely_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dim_ends, "same_kinds_run", lambda *_a: ("same_extent", "both end at the walls")
    )
    found = _escalated(_finding(37), reference(), vendor(37))
    assert proof.proof_group(found) == "likely_problem"
    assert found["formula"]["kind"] == "formula_chain"
    assert found["formula"]["outcome"] == "different" and found["formula"]["expected"] == '36"'
    assert "1\" more" in found["formula"]["why"]
    assert proof.likely_words(found).startswith("Please check and confirm: by GV's formula")
