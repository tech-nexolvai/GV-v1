"""Every published wall layout has reviewer words; stone between panels is "no field cut" (#1138).

Verification for `vocabulary/wall_layouts.py`, `workflow/part_operands.wall_layout_name` and the
slot-row wall provenance. Synthetic only.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from vocabulary.wall_layouts import (
    BETWEEN_PANELS_WORDS,
    WALL_LAYOUT_WORDS,
    is_between_panels,
    wall_layout_words,
)
from workflow.part_operands import wall_layout_name

RULE = Path(__file__).resolve().parents[2] / "rules" / "rulebook" / "ct_width_001.yaml"


def _published() -> set[str]:
    rule = yaml.safe_load(RULE.read_text(encoding="utf-8"))
    return {variant["when"] for variant in rule["applicability"]["variants"]}


def test_every_published_layout_has_words_and_no_more() -> None:
    assert set(WALL_LAYOUT_WORDS) == _published()
    for layout in _published():
        assert "_" not in wall_layout_words(layout)
        assert "_" not in wall_layout_name(layout)


def test_a_wall_at_one_end_is_said_with_its_side() -> None:
    assert wall_layout_words("back_and_left") == "back wall and left end"
    assert wall_layout_words("back_and_right") == "back wall and right end"
    assert wall_layout_name("back_and_left") == "back wall and left end"
    assert wall_layout_name("back_and_right") == "back wall and right end"


def test_back_only_between_panels_says_no_field_cut_not_back_wall_only() -> None:
    words = wall_layout_words("back_only", between_panels=True)
    assert words == BETWEEN_PANELS_WORDS == "no field cut: the stone stops at panels"
    assert "back wall" not in words
    # Any other layout chosen for such a row keeps its own words.
    assert wall_layout_words("back_left_right", between_panels=True) == "back wall and both ends"
    assert wall_layout_words("back_only") == "back wall only; no field cut at the ends"


def test_between_panels_is_read_from_the_rows_check_hold_only() -> None:
    assert is_between_panels(["slot-reader", "check-hold:stone-short-of-ends"])
    assert not is_between_panels(["slot-reader", "check-hold:stone-into-walls"])
    assert not is_between_panels([])
