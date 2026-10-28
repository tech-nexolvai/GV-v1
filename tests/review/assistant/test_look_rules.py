"""Look rules (`look_rules`, 2026-10-27): the whole-drawing look's words-only claims checked by code.

- GV's framed symbol at an extra piece's vendor spot: GV's markup, withdrawn; a count there is
  Not compared.
- A vendor spot on line work flattened into the vendor's drawing as markup (an unsure overlay):
  Not compared, never raised as the vendor's.
- A missing or extra piece is a Likely problem only when the look places it inside the unit or
  code reads a millwork tag on it; otherwise it is asked (Not compared).
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


def _missing(fid: str, place: str | None, tag: str | None = None) -> dict[str, Any]:
    piece = {
        "side": "architect",
        "file": REF,
        "page": 9,
        "box": [320, 60, 360, 240],
        "tag": tag,
        "place": place,
    }
    return _claim(fid, "Run: tall end panel", "missing_piece", (0, 0, 10, 10), piece=piece)


def test_a_missing_piece_inside_the_unit_stays_a_likely_problem() -> None:
    assert _changes([_missing("F1", "inside")]) == {}


def test_a_missing_piece_beside_the_unit_with_no_tag_is_asked() -> None:
    beside = _missing("F1", "beside")
    found = _changes([beside])["F1"]
    assert found.outcome == "not_compared" and found.rule == "piece_place"
    assert "drawn beside the unit on the architect's drawing" in found.why
    held = {**beside, claim_rules.FIELD: found.record()}
    assert proof_group(held) == "not_compared"
    assert "part of this millwork unit" in str(classified(held).settle)
    assert "F1" in _changes([_missing("F1", None)])  # no place given: asked


def test_a_millwork_tag_code_reads_on_the_piece_keeps_it_wherever_it_is() -> None:
    beside = _missing("F1", "beside", tag="TP-2")
    phrases = {(REF, 9): [{"text": "TP-2", "box": [325, 120, 345, 128], "side": "architect"}]}
    assert _changes([beside], phrases=phrases) == {}
    unnamed = _missing("F1", "beside")
    assert _changes([unnamed], phrases=phrases) == {}  # a tag printed on it, named or not
    other = {(REF, 9): [{"text": "WD-1", "box": [325, 120, 345, 128], "side": "architect"}]}
    assert "F1" in _changes([beside], phrases=other)  # not the tag the look read
    far = {(REF, 9): [{"text": "TP-2", "box": [500, 120, 520, 128], "side": "architect"}]}
    assert "F1" in _changes([beside], phrases=far)


def test_an_older_look_is_left_and_a_look_with_no_spot_is_asked() -> None:
    older = _claim("F1", "Run: end panel", "missing_piece", (330, 60, 340, 70))  # no piece
    extra = _claim("F2", "Run: bracket", "extra_piece", (330, 60, 340, 70))
    unplaced = _claim("F3", "Run: end panel", "missing_piece", (330, 60, 340, 70), piece={})
    found = _changes([older, extra, unplaced])
    assert set(found) == {"F3"}
    assert "did not say where the piece is" in found["F3"].why


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
        "place": None,
    }
    assert whole_look._piece_of(look, None) == {}
    assert whole_look._piece_of(look, {"picture": 3, "x0": 0, "y0": 0, "x1": 1, "y1": 1}) == {}
    assert whole_look._piece_of(look, {"picture": 1, "x0": 50, "y0": 0, "x1": 10, "y1": 9}) == {}


# ---- 5. a size the look worked out (2026-10-28) ---------------------------------------------------


def _sized(fid: str, vendor: str, architect: str, *, labels: tuple[str, ...]) -> dict[str, Any]:
    """A size claim the look tied to fewer than two labels: a Likely problem until code says more.
    `labels`: the sides with a printed label code found."""
    claim = _claim(fid, "Kitchenette: counter height", "size", (40, 300, 60, 420))
    claim.update(vendor=vendor, architect=architect)
    claim["dims"] = {
        side: {"id": f"D{n}", "page": 4 if side == "vendor" else 9, "text": "x"}
        for n, side in enumerate(labels, start=1)
    }
    return claim


def test_a_height_the_look_added_up_itself_is_not_compared_through_the_escalation() -> None:
    # The real case, with made-up values: the look adds a vendor chain that runs past the counter
    # to the splash's top and sets the total against the architect's floor-to-counter height.
    summed = _sized(
        "F1",
        'about 42 1/2" (chain 127 [5] + 775 [30 1/2] + 25 [1] + 152 [6], floor to top of splash)',
        "3' - 1\"",
        labels=("architect",),
    )
    assert proof_group(summed) == "likely_problem"  # before the rules: a please-check
    result = escalate([summed], EscalationContext(main_file=VENDOR))
    held = next(f for f in result.changed if f["id"] == "F1")
    assert held[claim_rules.FIELD]["rule"] == "look_own_sum"
    assert proof_group(held) == "not_compared"  # never a please-check, never red
    why = classified(held)
    assert "the vendor's value is the whole-drawing look's own sum" in str(why.why)
    assert "printed on both drawings" in str(why.settle)
    assert "F1" not in result.absorbed  # still listed: GV can look


@pytest.mark.parametrize(
    "value",
    [
        '61" (48 1/2" + 12 1/2" top filler; no overall printed)',
        '19" plus the toe kick',
        '42" (the chain of the base, top and splash)',
        '58" (sum of the three bays)',
        "the parts add up to 4'-2\"",
    ],
)
def test_sums_on_a_side_with_no_label_are_not_compared(value: str) -> None:
    assert "F1" in _changes([_sized("F1", value, "2' - 6\"", labels=("architect",))])
    assert "F2" in _changes([_sized("F2", '30"', value, labels=("vendor",))])  # either side


@pytest.mark.parametrize(
    "value",
    [
        '30"',
        "762 [30]",
        "2' - 6\"",
        '30" high, 18" deep',
        "1'-10 1/2\"",
        # An estimate or a size by scale where the drawing prints none stays (GV asked for the
        # scale there, client meeting 2026-10-23; `scale_measure` measures such spots).
        "about 4'-8\" wide",
        '~30"',
        'approx. 18"',
        '27" by scale',
        'low splash, about 2" by scale',
    ],
)
def test_a_printed_or_scaled_size_stays_a_likely_problem(value: str) -> None:
    assert _changes([_sized("F1", value, "2' - 7\"", labels=("architect",))]) == {}
    assert _changes([_sized("F2", '30"', value, labels=("vendor",))]) == {}


def test_a_side_with_a_printed_label_is_not_judged_by_its_words() -> None:
    # The label prints the look's value (`whole_look._label` checks it): the words do not matter.
    labelled = _sized("F1", 'about 30"', "2' - 7\"", labels=("vendor",))
    assert _changes([labelled]) == {}


def test_only_a_size_claim_is_judged_for_its_sum() -> None:
    count = _claim("F1", "Credenza: fronts", "count", (40, 300, 60, 420))
    count.update(vendor="2 doors + 1 bin", architect="3 doors")
    assert _changes([count]) == {}
