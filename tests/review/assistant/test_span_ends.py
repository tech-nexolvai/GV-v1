"""A dimension's two ends are an unordered pair (2026-10-27), $0, made-up values only.

A blind pick answers where each dimension's ends land as `{from, to}`; the schema gives the two
no order. On a real upload the pick wrote the vendor's ends as wall -> centre line and the
architect's as centre line -> wall: the same two ends. Code compared the objects in order, said
"the ends land in different places", and the item fell to Not compared instead of a please-check.

- the helpers: the same two ends in either order agree; different ends do not;
- the devil's advocate: reversed ends agree (a cheap-pick pairing then becomes a please-check,
  never red without code's geometry); truly different ends still stop it; two picks whose ends
  are only reversed agree;
- the card's wording names a different reference only when the ends really differ, and then the
  end each side names that the other does not.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.review.assistant import classification
from app.review.assistant.full_check_advocate import (
    ENDS_NOT_CONFIRMED,
    STRONG_PICK,
    Check,
    decide,
)
from app.review.assistant.span_ends import end_pair, ends_apart, same_ends

WALL_TO_CENTRE = {"from": "wall_face", "to": "centre_line"}
CENTRE_TO_WALL = {"from": "centre_line", "to": "wall_face"}
FLOOR_TO_CENTRE = {"from": "floor", "to": "centre_line"}
FLOOR_TO_TOP = {"from": "floor", "to": "top_of_counter"}


# ---- the helpers --------------------------------------------------------------------------------


def test_the_same_two_ends_in_either_order_are_the_same_ends() -> None:
    assert same_ends(WALL_TO_CENTRE, CENTRE_TO_WALL)
    assert end_pair(WALL_TO_CENTRE) == end_pair(CENTRE_TO_WALL)
    assert ends_apart(WALL_TO_CENTRE, CENTRE_TO_WALL) is None


def test_different_ends_stay_different() -> None:
    assert not same_ends(WALL_TO_CENTRE, FLOOR_TO_CENTRE)
    assert not same_ends(WALL_TO_CENTRE, FLOOR_TO_TOP)
    # Both ends at one place is not the same as one end there and one elsewhere.
    assert not same_ends({"from": "floor", "to": "floor"}, FLOOR_TO_TOP)


def test_anything_that_is_not_an_ends_answer_compares_as_before() -> None:
    assert same_ends(None, None)
    assert not same_ends(None, WALL_TO_CENTRE)
    assert not same_ends(WALL_TO_CENTRE, "wall_face to centre_line")
    assert end_pair(None) is None
    assert ends_apart(None, WALL_TO_CENTRE) is None


def test_the_end_that_differs_is_named_on_each_side() -> None:
    # One shared end (the centre line): each side's other end is the difference.
    assert ends_apart(WALL_TO_CENTRE, FLOOR_TO_CENTRE) == ("wall_face", "floor")
    assert ends_apart(CENTRE_TO_WALL, FLOOR_TO_CENTRE) == ("wall_face", "floor")
    # Nothing shared: the `to` ends, as the wording always used.
    assert ends_apart(WALL_TO_CENTRE, FLOOR_TO_TOP) == ("centre_line", "top_of_counter")


# ---- the devil's advocate -----------------------------------------------------------------------

_CODE_PASS = [Check(name, "pass") for name in ("ink", "annotation", "roles", "axis", "round")]
_COMMON: dict[str, Any] = {
    "mode": "code_ends",
    "checks": _CODE_PASS,
    "printed": True,
    "helper_pick": "p4.D2",
    "code_paired": False,
}
_CANNOT = ("cannot_tell", "a dimension line's ends were not found")
_SAME = ("same_extent", "both ends land on the same drawn lines")


def _pick(vendor: dict[str, str], architect: dict[str, str]) -> dict[str, Any]:
    return {
        "architect_pick": "D2",
        "pick": "p4.D2",
        "vendor_spans": vendor,
        "architect_spans": architect,
        "vendor_ink_black": "yes",
        "note": "",
        STRONG_PICK: True,
    }


def test_reversed_ends_agree_and_a_pick_alone_is_a_please_check_never_red() -> None:
    verdict = decide(advocate=_pick(WALL_TO_CENTRE, CENTRE_TO_WALL), extent=_CANNOT, **_COMMON)
    assert verdict.answers["spans"] == "agree"
    # Code's geometry could not tell: held back as "ends not confirmed", never passed to red.
    assert verdict.result == "objects"
    assert verdict.answers["pairing"] == ENDS_NOT_CONFIRMED


def test_a_cheap_pick_pairing_with_reversed_ends_still_needs_code_to_prove_the_ends() -> None:
    verdict = decide(
        advocate=_pick(WALL_TO_CENTRE, CENTRE_TO_WALL),
        extent=_CANNOT,
        code_ends=True,
        **_COMMON,
    )
    assert verdict.result == "objects" and "code proves both" in verdict.reason


def test_reversed_ends_pass_only_where_code_proves_the_same_ends() -> None:
    verdict = decide(advocate=_pick(WALL_TO_CENTRE, CENTRE_TO_WALL), extent=_SAME, **_COMMON)
    assert verdict.result == "passed" and verdict.answers["pairing"] == "proved"


def test_truly_different_ends_still_stop_the_pairing() -> None:
    verdict = decide(advocate=_pick(WALL_TO_CENTRE, FLOOR_TO_CENTRE), extent=_SAME, **_COMMON)
    assert verdict.result == "objects" and verdict.answers["spans"] == "differ"
    assert "ends land in different places" in verdict.reason


def test_two_picks_whose_ends_are_only_reversed_agree() -> None:
    verdict = decide(
        advocate=_pick(WALL_TO_CENTRE, WALL_TO_CENTRE),
        extent=_SAME,
        first_spans=(CENTRE_TO_WALL, CENTRE_TO_WALL),
        **_COMMON,
    )
    assert verdict.result == "passed"
    other = decide(
        advocate=_pick(WALL_TO_CENTRE, WALL_TO_CENTRE),
        extent=_SAME,
        first_spans=(FLOOR_TO_CENTRE, FLOOR_TO_CENTRE),
        **_COMMON,
    )
    assert other.result == "objects" and other.answers["spans"] == "picks_differ"


# ---- the card's wording -------------------------------------------------------------------------


def _finding(vendor: dict[str, str], architect: dict[str, str]) -> dict[str, Any]:
    return {
        "id": "F1",
        "page": 3,
        "item": "Base cabinet: width",
        "status": "different",
        "vendor": '18"',
        "architect": "2'-0\"",
        "checks": {"printed_vendor": "match", "printed_architect": "match"},
        "pairing": {"advocate": {"vendor_spans": vendor, "architect_spans": architect}},
    }


@pytest.mark.parametrize(
    ("vendor", "architect", "words"),
    [
        (WALL_TO_CENTRE, CENTRE_TO_WALL, "could not prove both dimensions run between"),
        (WALL_TO_CENTRE, FLOOR_TO_CENTRE, "vendor dimensions to the wall, architect to the floor"),
    ],
)
def test_the_card_names_a_different_reference_only_when_the_ends_differ(
    vendor: dict[str, str], architect: dict[str, str], words: str
) -> None:
    said = classification.classify(_finding(vendor, architect), proved=False)
    assert said.outcome == "not_compared"
    assert words in said.why, said.why
