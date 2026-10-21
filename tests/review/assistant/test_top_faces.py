"""A top's upper face against its underside is never red on picks; pages with no vendor drawing
never block a complete check (#1837). Made-up values only; no network, no paid call.

- **Words (`face_doubt`).** A finding whose own item or notes name the underside of a top (or the
  carcass top under it, a top's thickness) is never proved by blind picks or the anchor fit (it
  allows the claimed difference: the band itself); only code's own pairing proves it. The roster
  asks no pick for it.
- **Drawn faces (`same_ends.other_face`).** Where the ends cannot tell, a drawn line exactly where
  the other drawing's longer value would end, beyond the dimension's own end, means different
  points: a top's upper face drawn right where the architect's height to the counter would end.
- **Pages with no vendor drawing** (`full_check_plan.no_vendor_pages`): never a page "not
  compared", never sent on again, said "no vendor drawing to compare" by code.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import full_check_roster
from app.review.assistant import same_ends as se
from app.review.assistant.full_check_advocate import (
    STRONG_PICK,
    Check,
    Extent,
    decide,
    face_doubt,
    finding_words,
)
from app.review.assistant.full_check_plan import NO_VENDOR_NOTE, no_vendor_pages
from tests.review.assistant.test_full_check_fanout import FILE, _divider, _half, _page, _paired
from tests.review.assistant.test_full_check_roster import _clean_ink, _Roster, _roster
from tests.review.assistant.test_same_ends import Elevation

# ---- the words -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "words",
    [
        "Washstand 9: floor to the underside of the slab / top of the carcass",
        "architect floor to counter vs vendor floor to under-side",
        "Bar 2: floor to U/S of top",
        "counter to bottom of the upper cabinet",
        "height includes the top thickness",
        "vendor_to: underside_of_counter",
    ],
)
def test_words_naming_an_underside_face_are_doubted(words: str) -> None:
    assert face_doubt(item=words) is not None
    assert face_doubt(item="Area 3: height", words=words) is not None


@pytest.mark.parametrize(
    "words",
    [
        "Area 3: floor to top of counter, overall height",
        "Buffet: floor to upper outlet centreline",
        "Run: depth, back to front of top",
        "Base cabinet: width",
    ],
)
def test_other_words_are_not_doubted(words: str) -> None:
    assert face_doubt(item=words) is None


_UNDER = {"from": "floor", "to": "underside_of_counter"}
_PICK = {
    "pick": "p9.D4",
    "vendor_spans": _UNDER,
    "architect_spans": _UNDER,
    "vendor_ink_black": "yes",
    STRONG_PICK: True,
}
_CANNOT: tuple[Extent, str] = ("cannot_tell", "no place is tied on both drawings")
_ANCHORS: tuple[Extent, str] = ("same_extent", "the ends land together (3 of 9 pt allowed)")
_COMMON: dict[str, Any] = {
    "mode": "ai_pair_plus_code",
    "checks": [Check(n, "pass") for n in ("ink", "annotation", "roles", "axis", "round")],
    "printed": True,
    "helper_pick": "p9.D4",
}
_FINDING = {
    "item": "Washstand 9: floor to the underside of the slab / top of the carcass",
    "why": 'architect floor to counter (2\'-7") vs vendor floor to underside 30 1/4"',
}


@pytest.mark.parametrize("extent", [_CANNOT, _ANCHORS])
def test_an_underside_item_is_never_red_on_agreeing_picks(extent: tuple[Extent, str]) -> None:
    """The paid run's false red, with made-up values: two picks agreed on the label and both said
    "underside" (the second was wrong about the architect's end). Neither the picks nor the
    anchor fit (it allows the claimed difference) show which face each end stops at."""
    verdict = decide(
        extent=extent,
        item=_FINDING["item"],
        words=finding_words(_FINDING),
        advocate=_PICK,
        code_paired=False,
        **_COMMON,
    )
    assert verdict.result == "objects"
    assert "different faces" in verdict.reason
    assert verdict.answers["faces"] == "fail"
    # Without a pick (none is asked for it) it is not "Not verified within the fixed price":
    # the escalation never asks again.
    unasked = decide(
        extent=extent,
        item=_FINDING["item"],
        words=finding_words(_FINDING),
        advocate=None,
        code_paired=False,
        **_COMMON,
    )
    assert unasked.result == "objects" and "different faces" in unasked.reason


def test_code_pairing_both_ends_still_proves_an_underside_item() -> None:
    verdict = decide(
        extent=None, item=_FINDING["item"], words="", advocate=None, code_paired=True, **_COMMON
    )
    assert verdict.result == "passed" and verdict.answers["pairing"] == "code"


def test_a_top_of_counter_item_still_passes_on_the_strong_pick() -> None:
    """The true Wrong of this shape (floor to the top of the counter on both) keeps its red."""
    top = {"from": "floor", "to": "top_of_counter"}
    verdict = decide(
        extent=_CANNOT,
        item="Area 3: floor to top of counter, overall height",
        words="floor to top of counter both sides",
        advocate={**_PICK, "vendor_spans": top, "architect_spans": top},
        code_paired=False,
        **_COMMON,
    )
    assert verdict.result == "passed" and verdict.answers["pairing"] == "proved_by_two_picks"


# ---- the drawn faces ---------------------------------------------------------------------------------


def _band(scale: float = 4.0) -> tuple[Elevation, Elevation, str, str]:
    """No floor and no shared value, so no end is tied: the vendor's 30 1/4" runs from a base
    line to the underside of a 3/4" top whose upper face is drawn; the architect's 31" runs to
    the top of the counter."""
    arch = Elevation("architect", 100, 300, 2.0, 1)
    vendor = Elevation("vendor", 400, 760, scale, 30)
    arch.up(70, -30)
    a_id = arch.up(31, -10)
    vendor.up(64, -30)
    vendor.shelf(30.25, -12, 40)
    vendor.shelf(31, -12, 40)  # the top's upper face
    v_id = vendor.up(30.25, -10)
    return arch, vendor, a_id, v_id


def test_a_drawn_face_where_the_other_value_ends_is_different_points() -> None:
    arch, vendor, a_id, v_id = _band()
    state, why = se.prove(vendor.side_of(), arch.side_of(), v_id, a_id)
    assert state == "different_extent", why
    assert "vendor's drawing" in why and "different faces" in why and '3/4"' in why


def test_with_nothing_drawn_there_it_still_cannot_tell() -> None:
    arch = Elevation("architect", 100, 300, 2.0, 1)
    vendor = Elevation("vendor", 400, 760, 4.0, 30)
    arch.up(70, -30)
    a_id = arch.up(31, -10)
    vendor.up(64, -30)
    vendor.shelf(30.25, -12, 40)
    v_id = vendor.up(30.25, -10)
    state, _why = se.prove(vendor.side_of(), arch.side_of(), v_id, a_id)
    assert state == "cannot_tell"


def test_a_line_inside_the_dimension_is_no_sign() -> None:
    """Only a longer value is looked at, beyond the line's own end: a unit's parts are drawn
    inside every dimension (a cabinet under a top)."""
    _arch, vendor, _a, v_id = _band()
    found = vendor.side_of()
    line = found.lines[v_id]
    assert se.other_face(found, line, Fraction(29)) is None  # shorter: inside, never looked at
    assert se.other_face(found, line, Fraction(31)) is not None


def test_a_small_scale_never_calls_a_face() -> None:
    """Below `FACE_MIN_PPI` a floor's thickness or hatching sits a point or two from the end."""
    _arch, vendor, _a, v_id = _band(scale=2.0)
    found = vendor.side_of()
    assert se.other_face(found, found.lines[v_id], Fraction(31)) is None


# ---- pages with no vendor drawing ------------------------------------------------------------------


def _architect_only(page: int, area: str) -> dict[str, Any]:
    return _page(page, [_half("architect"), _half("brand_prototype")], area)


def test_pages_with_only_architect_or_prototype_drawings_have_no_vendor_drawing() -> None:
    page_map = {
        "version": 1,
        "files": [FILE],
        "pages": [
            _divider(1),
            _paired(2, "1. Area 1"),
            _architect_only(3, "1. Area 1"),
            _page(4, [_half("brand_prototype")], "1. Area 1"),
            _page(5, [_half("architect"), _half("unknown")], "1. Area 1"),
        ],
    }
    assert no_vendor_pages(page_map, FILE) == {3, 4}
    assert no_vendor_pages(None, FILE) == frozenset()


def test_an_architect_only_page_never_blocks_a_complete_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Page 3 holds only the architect's and the brand prototype's drawings; its helper marks it
    not comparable. It is never "not compared", never sent on again, and the check is complete."""
    _clean_ink(monkeypatch)
    script = _Roster(unpaired=3)
    harness = _roster(tmp_path, script)
    folder = harness.engine.tools.workspace.set_pack
    assert folder is not None
    page_map = json.loads((folder / "page_map.json").read_text())
    page_map["pages"] = [
        _architect_only(3, entry["area"]) if entry["page"] == 3 else entry
        for entry in page_map["pages"]
    ]
    (folder / "page_map.json").write_text(json.dumps(page_map))

    outcome = harness.run()

    assert "Check incomplete" not in outcome.text
    assert full_check_roster.NOT_SENDABLE_NEXT not in outcome.text
    assert "more looks" not in outcome.text
    pages = {
        int(p["page"]): p
        for p in [b for b in outcome.blocks if b["type"] == "coverage"][-1]["coverage"]["pages"]
    }
    assert pages[3]["status"] == "not_comparable"
    assert pages[3]["note"] == NO_VENDOR_NOTE
    assert pages[3]["by"] == "code"
    # No second look was sent for it: every numbers brief names page 3 once at most.
    briefs = [str(p) for p in script.params["numbers"]]
    assert sum("page not compared yet" in brief for brief in briefs) == 0
