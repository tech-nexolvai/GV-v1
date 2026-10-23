"""Measuring by scale where the reference prints no size (`scale_measure`, client meeting
2026-10-23): code's ruler at the view's own scale; more than 1/4" off is a Likely problem; never red.

A synthetic sheet (`test_measure.sheet`, 3 points per real inch): a 36" unit between line work at
x = 200 and x = 308. Made-up values only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import likely_problems, proof, scale_measure
from app.review.assistant.view_scales import ViewScale
from tests.review.assistant.test_measure import LEFT, MIDDLE, sheet

#: The view's scale code measured from its own printed dimensions (3 points per real inch).
SCALE = ViewScale(
    page=1,
    side="architect",
    in_per_pt=1 / 3,
    labels=("D1", "D2", "D3"),
    spread_percent=0.4,
    box=(100.0, 100.0, 700.0, 560.0),
)
#: The AI's spot round the unit on the reference: a few points inside its outline.
SPOT = {"page": 1, "x0": LEFT + 2.0, "y0": 260.0, "x1": MIDDLE - 2.5, "y1": 480.0, "file": "a.pdf"}


def _item(vendor: str = "37", inches: str | None = None, **more: Any) -> dict[str, Any]:
    return {
        "id": "F1",
        "page": 3,
        "item": "Vanity: base cabinet width",
        "status": "check",
        "source": "architect",
        "architect": "no size printed",
        "vendor": f'{vendor}"',
        "why": "The architect prints no width.",
        "arch_box": dict(SPOT),
        "dims": {
            "vendor": {
                "id": "D7",
                "page": 3,
                "inches": inches or vendor,
                "orientation": "h",
                "route": "text",
                "text": f'{vendor}"',
            }
        },
        **more,
    }


@pytest.fixture
def pdf(tmp_path: Path) -> Path:
    return sheet(tmp_path / "a.pdf")


def test_a_size_the_reference_does_not_print_is_measured_by_its_scale(pdf: Path) -> None:
    found = scale_measure.measure_item(_item("36 1/8", "36.125"), pdf=pdf, scales=[SCALE])
    assert found is not None
    assert found["text"] == 'about 36"' and found["scale"] == "about 1:24"
    assert not found["differs"]  # 1/8" off: within the 1/4"
    updated = scale_measure._with_measure(_item("36 1/8", "36.125"), found)
    assert updated["why"].startswith('Measured by scale: about 36" on the reference drawing')
    assert likely_problems.FIELD not in updated  # within 1/4": said, not a problem
    assert 'measured by scale: about 36"' in str(proof.classified(updated).why).lower()
    assert proof.scaled(updated)  # never proof


def test_more_than_a_quarter_inch_off_is_a_likely_problem_never_red(pdf: Path) -> None:
    item = _item("37", status="different")
    found = scale_measure.measure_item(item, pdf=pdf, scales=[SCALE])
    assert found is not None and found["differs"] and found["off_in"] == 1.0
    updated = scale_measure._with_measure(item, found)
    assert updated["status"] == "check"
    assert proof.proof_group(updated) == "likely_problem"
    words = likely_problems.please_check(updated)
    assert 'measured about 36" on the reference drawing' in words and '37"' in words
    assert not proof.code_proved({**updated, "status": "different", "trust_level": "confirmed"})


@pytest.mark.parametrize(
    "item",
    [
        # The reference prints a size: compared as printed, never measured.
        _item(architect="3'-0\""),
        _item(dims={"vendor": _item()["dims"]["vendor"], "architect": {"id": "D2"}}),
        # A whole drawing as the spot: no tight box round one piece.
        _item(arch_box={"page": 1, "x0": 0, "y0": 0, "x1": 792, "y1": 612, "file": "a.pdf"}),
        # The vendor's value is no label code read.
        _item(dims=None),
        # A Right item is not measured.
        _item(status="same"),
    ],
)
def test_only_open_items_with_no_printed_reference_size_are_measured(
    pdf: Path, item: dict[str, Any]
) -> None:
    assert scale_measure.measure_item(item, pdf=pdf, scales=[SCALE]) is None


def test_no_measured_scale_or_an_edge_on_no_line_is_no_measurement(pdf: Path) -> None:
    # No scale code measured from the view's own dimensions (the title block's is never used).
    assert scale_measure.measure_item(_item(), pdf=pdf, scales=[]) is None
    # A spot whose edges land on no line of the drawing.
    loose = {**SPOT, "x0": LEFT + 30.0, "x1": MIDDLE - 30.0}
    assert scale_measure.measure_item(_item(arch_box=loose), pdf=pdf, scales=[SCALE]) is None


def test_a_view_at_another_scale_uses_its_own(pdf: Path) -> None:
    # Two views on one sheet: the spot is in the 1:24 one; the other view's scale is not used.
    other = ViewScale(1, "architect", 1.0, ("D8", "D9", "D10"), 0.2, (600.0, 0.0, 790.0, 90.0))
    found = scale_measure.measure_item(_item("36"), pdf=pdf, scales=[other, SCALE])
    assert found is not None and found["text"] == 'about 36"'
