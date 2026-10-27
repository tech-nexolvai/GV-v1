"""Look rules (`look_rules`, 2026-10-27): the whole-drawing look's words-only claims checked by code.

- GV's framed symbol at an extra piece's vendor spot: GV's markup, withdrawn; a count there is
  Not compared.
- A vendor spot on line work flattened into the vendor's drawing as markup (an unsure overlay):
  Not compared, never raised as the vendor's.
- A missing or extra piece is a Likely problem only when code places it inside the unit's
  dimensioned extent or finds a millwork tag on it; otherwise it is asked (Not compared).
- A claim the look marked to ask (two differences in one, a count of unlike things): Not compared.
- The whole-drawing look writes where the piece is and why to ask (`whole_look._ask`).

Made-up drawings and values only.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.review.assistant import claim_rules, look_rules, whole_look
from app.review.assistant.escalation import EscalationContext, escalate
from app.review.assistant.proof import classified, proof_group

VENDOR = "vendor.pdf"
REF = "reference.pdf"


def _box(file: str, page: int, box: tuple[float, float, float, float]) -> dict[str, Any]:
    x0, y0, x1, y1 = box
    return {"file": file, "page": page, "x0": x0, "y0": y0, "x1": x1, "y1": y1}


def _claim(
    fid: str, item: str, kind: str, spot: tuple[float, float, float, float], **mark: Any
) -> dict[str, Any]:
    """A whole-drawing look's claim code could not prove: a Likely problem."""
    return {
        "id": fid,
        "page": 4,
        "vs_page": 9,
        "item": item,
        "vendor": "see the drawing",
        "architect": "see the drawing",
        "status": "check",
        "source": "architect",
        "category": "other",
        "created_by": "whole_look",
        "vendor_box": _box(VENDOR, 4, spot),
        "arch_box": _box(REF, 9, (0, 0, 500, 500)),
        "whole_look": {
            "by": "whole_drawing_look",
            "kind": kind,
            "proof": "likely_only",
            "sure": "likely",
            "words": f"Please check and confirm: {item}.",
            **mark,
        },
    }


def _changes(findings: list[dict[str, Any]], **facts: Any) -> dict[str, claim_rules.Change]:
    return dict(look_rules.changes(findings, main_file=VENDOR, **facts))


#: GV's framed outlet symbol on the vendor's page 4, and an unsure overlay of black lines.
_SYMBOL = {"page": 4, "box": [102, 104, 116, 112], "text": "", "kind": "symbol", "file": VENDOR}
_OVERLAY = {"page": 4, "box": [300, 100, 400, 300], "text": "", "kind": "overlay", "file": VENDOR}


def _dim(
    fid: str, side: str, line: tuple[tuple[float, float], tuple[float, float]]
) -> dict[str, Any]:
    (ax, ay), (bx, by) = line
    return {
        "id": fid,
        "text": '30"',
        "inches": "30",
        "side": side,
        "box": [min(ax, bx), min(ay, by), max(ax, bx) + 10, max(ay, by) + 6],
        "dim_line": [[ax, ay], [bx, by]],
    }


#: The reference's unit on page 9: dimension lines across 100..300 and up 50..250.
_REF_DIMS = [
    _dim("D1", "architect", ((100, 260), (300, 260))),
    _dim("D2", "architect", ((90, 50), (90, 250))),
]


# ---- 1. GV's symbol ----------------------------------------------------------------------------


def test_an_extra_piece_that_is_gvs_symbol_is_withdrawn_through_the_escalation() -> None:
    extra = _claim("F1", "Top filler: duplex outlet", "extra_piece", (100, 100, 118, 114))
    result = escalate(
        [extra], EscalationContext(main_file=VENDOR, stickers={(VENDOR, 4): [_SYMBOL]})
    )
    assert set(result.absorbed) == {"F1"}
    assert "GV's own symbol" in result.absorbed["F1"]
    assert result.settled["claim_rules"] == 1


def test_a_count_over_gvs_symbol_is_not_compared_and_other_spots_stay() -> None:
    count = _claim("F1", "Run: outlets", "count", (98, 100, 130, 120))
    elsewhere = _claim("F2", "Run: outlets", "count", (200, 200, 230, 220))
    big = _claim("F3", "Run: outlets", "count", (0, 0, 400, 400))  # a whole run, not the symbol
    found = _changes([count, elsewhere, big], stickers={(VENDOR, 4): [_SYMBOL]})
    assert found["F1"].outcome == "not_compared" and found["F1"].rule == "gv_markup"
    assert set(found) == {"F1"}


# ---- 2. unsure overlays ------------------------------------------------------------------------


def test_a_claim_on_an_unsure_overlay_is_not_raised_as_the_vendors() -> None:
    on = _claim("F1", "Upper bay: shelves", "layout", (310, 110, 390, 290))
    beside = _claim("F2", "Upper bay: shelves", "layout", (380, 110, 480, 290))  # 20% on it
    result = escalate(
        [on, beside], EscalationContext(main_file=VENDOR, overlays={(VENDOR, 4): [_OVERLAY]})
    )
    held = next(f for f in result.changed if f["id"] == "F1")
    assert proof_group(held) == "not_compared"
    assert held[claim_rules.FIELD]["rule"] == "unsure_overlay"
    why = classified(held)
    assert "cannot tell apart from GV's own" in str(why.why)
    assert "without GV's markup" in str(why.settle)
    kept = next((f for f in result.changed if f["id"] == "F2"), beside)
    assert claim_rules.FIELD not in kept and proof_group(kept) == "likely_problem"


def test_a_size_two_labels_name_or_a_decided_claim_is_never_changed() -> None:
    sized = _claim("F1", "Run: height", "size", (310, 110, 390, 290))
    sized["whole_look"]["proof"] = "labels"
    decided = _claim("F2", "Upper bay: shelves", "layout", (310, 110, 390, 290))
    decided["decision"] = {"action": "agree"}
    assert _changes([sized, decided], overlays={(VENDOR, 4): [_OVERLAY]}) == {}


# ---- 3. a piece beside the unit ----------------------------------------------------------------


def _missing(fid: str, box: tuple[float, float, float, float], tag: str | None = None) -> Any:
    piece = {"side": "architect", "file": REF, "page": 9, "box": list(box), "tag": tag}
    return _claim(fid, "Run: tall end panel", "missing_piece", (0, 0, 10, 10), piece=piece)


def test_a_missing_piece_inside_the_units_extent_stays_a_likely_problem() -> None:
    inside = _missing("F1", (250, 60, 290, 240))
    assert _changes([inside], labels={(REF, 9): _REF_DIMS}) == {}


def test_a_missing_piece_beside_the_unit_with_no_tag_is_asked() -> None:
    beside = _missing("F1", (320, 60, 360, 240))
    found = _changes([beside], labels={(REF, 9): _REF_DIMS})["F1"]
    assert found.outcome == "not_compared" and found.rule == "piece_place"
    assert "outside the unit's dimensioned extent on the architect's drawing" in found.why
    held = {**beside, claim_rules.FIELD: found.record()}
    assert proof_group(held) == "not_compared"
    assert "part of this millwork unit" in str(classified(held).settle)


def test_a_millwork_tag_on_the_piece_keeps_it_wherever_it_is() -> None:
    beside = _missing("F1", (320, 60, 360, 240), tag="TP-2")
    phrases = {(REF, 9): [{"text": "TP-2", "box": [325, 120, 345, 128], "side": "architect"}]}
    assert _changes([beside], labels={(REF, 9): _REF_DIMS}, phrases=phrases) == {}
    other = {(REF, 9): [{"text": "WD-1", "box": [325, 120, 345, 128], "side": "architect"}]}
    assert "F1" in _changes([beside], labels={(REF, 9): _REF_DIMS}, phrases=other)


def test_dimensions_one_way_only_do_not_show_the_units_extent() -> None:
    heights_only = [_dim("D2", "architect", ((90, 50), (90, 250)))]
    found = _changes([_missing("F1", (250, 60, 290, 240))], labels={(REF, 9): heights_only})
    assert "no dimension lines across" in found["F1"].why


def test_an_extra_piece_uses_its_vendor_spot_and_an_older_missing_piece_is_left() -> None:
    vendor_dims = [
        _dim("D1", "vendor", ((100, 260), (300, 260))),
        _dim("D2", "vendor", ((90, 50), (90, 250))),
    ]
    extra_in = _claim("F1", "Run: fillers", "extra_piece", (110, 60, 120, 240))
    extra_out = _claim("F2", "Run: bracket", "extra_piece", (330, 60, 340, 70))
    older = _claim("F3", "Run: end panel", "missing_piece", (330, 60, 340, 70))  # no piece
    unplaced = _claim("F4", "Run: end panel", "missing_piece", (330, 60, 340, 70), piece={})
    found = _changes([extra_in, extra_out, older, unplaced], labels={(VENDOR, 4): vendor_dims})
    assert set(found) == {"F2", "F4"}
    assert "did not say where the piece is" in found["F4"].why


# ---- 4. a claim to ask, and the look's own marks ------------------------------------------------


def test_a_claim_the_look_marked_to_ask_is_not_compared() -> None:
    asked = _claim(
        "F1", "Base: fronts and base", "layout", (0, 0, 10, 10), ask="two differences in one"
    )
    found = _changes([asked])["F1"]
    assert (found.outcome, found.rule, found.why) == (
        "not_compared",
        "look_question",
        "two differences in one",
    )


@pytest.mark.parametrize(
    ("kind", "item", "vendor", "architect", "asked"),
    [
        ("layout", "Base cabinet: front layout and base", "x", "y", True),
        ("layout", "Credenza: door layout and count left of the bins", "x", "y", False),
        ("layout", "Upper cabinets: height", "x", "y", False),
        ("count", "Credenza: bins", "1 bin pull-out (BN-18)", "2 bin pull-outs", False),
        ("count", "Credenza: fronts", "2 doors + 1 bin", "4 fronts", True),
        ("count", "Credenza: fronts", "3 doors", "4 fronts", True),
        ("count", "Shelves", "1 shelf", "4 open shelves above the fridge", False),
    ],
)
def test_the_look_asks_two_differences_or_unlike_counts(
    kind: str, item: str, vendor: str, architect: str, asked: bool
) -> None:
    assert (whole_look._ask(kind, item, vendor, architect) is not None) is asked


def test_the_look_writes_the_pieces_spot_on_the_drawing_that_has_it() -> None:
    drawings = (
        whole_look.Drawing(file=REF, page=9, box=(100, 100, 300, 500), role="architect"),
        whole_look.Drawing(file=VENDOR, page=4, box=(0, 0, 400, 400), role="vendor"),
    )
    look = whole_look.Look(number=1, area="Run", drawings=drawings)
    piece = whole_look._piece_of(
        look, {"picture": 1, "x0": 50, "y0": 0, "x1": 100, "y1": 25, "tag": " TP-2 "}
    )
    assert piece == {
        "side": "architect",
        "file": REF,
        "page": 9,
        "box": [200.0, 100.0, 300.0, 200.0],
        "tag": "TP-2",
    }
    assert whole_look._piece_of(look, None) == {}
    assert whole_look._piece_of(look, {"picture": 3, "x0": 0, "y0": 0, "x1": 1, "y1": 1}) == {}
    assert whole_look._piece_of(look, {"picture": 1, "x0": 50, "y0": 0, "x1": 10, "y1": 9}) == {}
