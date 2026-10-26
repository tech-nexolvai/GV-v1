"""The swap check: same-titled architect pages told apart by their printed sizes.

A real two-file upload drew one kitchen on four architect sheets of one title (its plan and three
elevations), and two kitchenettes on two sheets each. The AI page matcher was right and sure of
every vendor page, but a title counts only when exactly one page carries it (#1913), so 8 of 20
vendor pages stayed "pairing unsure". Other tools pair the sheets, then re-check look-alikes by
their content (a "pair-swap audit"). Now code compares the vendor's printed sizes with each page of
the matcher's pick's title:

- the vendor's overall size printed on the page counts most, one dimension line's parts adding up
  to it next, the vendor's own parts in the same order on one line (or backwards: a mirrored
  unit) last;
- the pick must match clearly better than every other page of its title; a tie, or a pick that
  matches nothing, stays "pairing unsure";
- it only agrees with a sure matcher: never pairs alone, never against a contradiction (Type 1
  against Type 3, accessible against standard).

Synthetic sets only: $0, no model call (the set index and the page matcher are fake answers).
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from app.review.assistant.simple_check import worklist
from app.review.assistant.simple_check.pairing import (
    Facts,
    MatchAnswer,
    Scored,
    agree,
    size_match,
    size_rows,
    size_words,
    swap_check,
    with_sizes,
)
from app.review.assistant.simple_check.worklist import DRAWING_NEEDED
from tests.review.assistant.test_pair_by_index_titles import (
    _across,
    _matched,
    _paired_pages,
    _work,
)

F = Fraction

#: The vendor's page, and five architect pages: two of one title side by side.
VENDOR = {1: "SAMPLE PANTRY CABINET"}
SHEETS = {
    1: "BENCH",
    2: "SAMPLE PANTRY CABINET",
    3: "SAMPLE PANTRY CABINET",
    4: "WARDROBE",
    5: "DESK",
}
#: Sizes no vendor drawing prints (the vendor's: 63" wide, 22" and 34").
OTHER = (100, 40, 36)
#: Page 3 prints the vendor's overall 63"; page 2 a height within 1" of the vendor's 34": to the
#: older score they look alike (one close size each), so the sure matcher's pick stayed unsure.
LOOK_ALIKE = {"p2": (100, 40, 35), "p3": (63, 40, 36)}


def _sizes(**pages: tuple[int, int, int]) -> dict[int, tuple[int, int, int]]:
    """Every architect page prints `OTHER`, but the pages named (`p3=(63, 22, 34)`)."""
    out = dict.fromkeys(SHEETS, OTHER)
    out.update({int(name[1:]): sizes for name, sizes in pages.items()})
    return out


def _pair(
    pick: int, sizes: dict[int, tuple[int, int, int]], sheets: dict[int, str] = SHEETS
) -> Any:
    unasked = _work(None, VENDOR, sheets, sizes)
    return _across(_work(_matched(unasked, {1: pick}), VENDOR, sheets, sizes), 1)


def _line(n: int, inches: float, x0: float, x1: float, y: float, axis: str = "h") -> dict[str, Any]:
    """A label on a dimension line from `x0` to `x1` at `y` (across) or the other way (`v`)."""
    line = [[x0, y], [x1, y]] if axis == "h" else [[y, x0], [y, x1]]
    return {"id": f"D{n}", "inches": str(inches), "dim_line": line, "orientation": axis}


# ---- the sizes ---------------------------------------------------------------------------------


def test_dimension_rows_are_read_in_order_along_each_line() -> None:
    labels = [
        _line(1, 27, 120, 220, 50),
        _line(2, 0.75, 100, 120, 50.8),
        _line(3, 32.25, 220, 340, 49.5),
        _line(4, 60, 100, 340, 80),  # alone on its line: no row of parts
        _line(5, 30, 10, 100, 400, "v"),
        _line(6, 4, 100, 120, 400.5, "v"),
        {"id": "D7", "inches": "12", "dim_line": None},
        # On the same line but past a gap: another string (another view drawn beside it).
        _line(8, 18, 380, 450, 50),
        _line(9, 18, 450, 520, 50),
    ]
    assert size_rows(labels) == (
        ("h", (F(3, 4), F(27), F(129, 4))),
        ("h", (F(18), F(18))),
        ("v", (F(30), F(4))),
    )
    assert [size_words(v) for v in (F(111, 2), F(60), F(3, 4))] == ['55 1/2"', '60"', '3/4"']


def test_the_overall_printed_beats_one_added_up_and_parts_read_backwards_are_mirrored() -> None:
    vendor = Facts(
        "v",
        values=(F(60), F(51, 2), F(15), F(30)),
        size_rows=(("h", (F(15), F(30), F(15))), ("h", (F(10), F(20)))),
    )
    printed = Facts("a1", values=(F(60), F(24)))
    added = Facts("a2", values=(F(3, 4), F(27)), size_rows=(("h", (F(3, 4), F(27), F(129, 4))),))
    backwards = Facts("a3", values=(F(20), F(10)), size_rows=(("h", (F(20), F(10))),))
    assert size_match(vendor, printed).printed == (F(60),)
    found = size_match(vendor, added)
    assert found.added == ((F(60), (F(3, 4), F(27), F(129, 4))),) and found.points == 1
    assert 'adds up to the vendor\'s overall 60" (3/4" + 27" + 32 1/4")' in found.words()
    mirror = size_match(vendor, backwards)
    assert mirror.parts == (F(10), F(20)) and mirror.mirrored
    assert "in reverse order (a mirrored unit)" in mirror.words()
    # The same parts both ways (15, 30, 15) are no mirror.
    same = Facts("a4", size_rows=(("h", (F(15), F(30), F(15))),))
    assert size_match(vendor, same).parts and not size_match(vendor, same).mirrored
    # An up-and-down string read backwards is upside down, never a mirror.
    upright = Facts("v3", values=(F(60),), size_rows=(("v", (F(10), F(20))),))
    flipped = Facts("a6", size_rows=(("v", (F(20), F(10))),))
    assert not size_match(upright, flipped).mirrored and not size_match(upright, flipped).parts
    # An overall under 24" is never added up (12" + 12" is too common), only printed.
    short = Facts("v4", values=(F(20), F(15)))
    assert size_match(short, Facts("a7", size_rows=(("h", (F(10), F(10))),))).points == 0
    # Two small parts in a row (under a foot together) say nothing.
    small = Facts("v2", values=(F(60),), size_rows=(("h", (F(3, 2), F(3, 2))),))
    assert not size_match(small, Facts("a5", size_rows=(("h", (F(3, 2), F(3, 2))),))).parts


def test_the_pick_must_match_clearly_better_than_every_page_of_its_title() -> None:
    vendor = Facts("v", values=(F(60), F(51, 2), F(12)))
    pick = Facts("a1", values=(F(60), F(25)))
    sibling = Facts("a2", values=(F(36),))
    match, why = swap_check(vendor, pick, [sibling])
    assert match is not None and not why
    # A sibling printing the overall too: a tie, nobody wins.
    twin = Facts("a3", values=(F(60),))
    match, why = swap_check(vendor, pick, [sibling, twin])
    assert match is None and "a1 and a3 match its sizes alike" in why
    # One added up against one printed: the printed one is clearly better, never the other way.
    chain = Facts("a4", size_rows=(("v", (F(30), F(30))),))
    assert swap_check(vendor, pick, [chain])[0] is not None
    assert swap_check(vendor, chain, [pick])[0] is None
    # Won only by a sum against a sibling whose numbers code could not read: unsure (its
    # "nothing" may be "not read", never "does not match").
    unread = Facts("a6", values=(F(30),))
    match, why = swap_check(vendor, chain, [unread])
    assert match is None and "read too few sizes on a6" in why
    read = Facts("a7", values=(F(36), F(18), F(15)), size_rows=(("h", (F(18), F(15))),))
    assert swap_check(vendor, chain, [read])[0] is not None
    # A pick matching none of the vendor's overall sizes never wins, whatever its siblings.
    assert swap_check(vendor, sibling, [Facts("a5")])[0] is None


def test_the_sizes_only_agree_with_a_sure_matcher_never_against_a_contradiction() -> None:
    ranked = (
        Scored("a2", 3.0, ("both print PANTRY",)),
        Scored("a1", 2.5, ("both print PANTRY",)),
        Scored("a3", 1.0, (), contradicts="another unit type"),
    )
    match = size_match(Facts("v", values=(F(60),)), Facts("a1", values=(F(60),)))
    sized = with_sizes(ranked, "a1", match, ["a2"], "SAMPLE PANTRY")
    first = sized[0]
    assert first.ref == "a1" and first.strong and first.agree_only
    assert first.evidence[0].startswith('of the 2 pages titled "SAMPLE PANTRY", its sizes match')
    # Never pairs without the matcher, nor with a matcher that is not sure.
    assert agree(sized, None, None)[0] == ()
    assert agree(sized, None, MatchAnswer(("a1",), confident=False))[0] == ()
    assert agree(sized, None, MatchAnswer(("a1",), confident=True))[0] == ("a1",)
    # A contradicted pick is never helped.
    assert with_sizes(ranked, "a3", match, ["a2"], "X")[2] == ranked[2]
    # The sizes never outweigh another page's own strong evidence (a printed view reference):
    # the bonus is not code's own evidence, as the title's is not.
    strong = (Scored("a2", 4.0, ("printed reference 2/A5 on both",), strong=True), *ranked[1:])
    sized = with_sizes(strong, "a1", match, ["a4"], "SAMPLE PANTRY")
    assert next(s for s in sized if s.ref == "a1").own == 2.5
    assert agree(sized, None, MatchAnswer(("a1",), confident=True))[0] == ()


def test_the_vendors_own_title_must_share_a_word_with_the_picks() -> None:
    assert worklist._shares_a_word("STANDARD STUDIO KITCHEN", "STANDARD STUDIO KITCHENETTE")
    assert worklist._shares_a_word("KITCHEN", "STANDARD KITCHEN")
    assert not worklist._shares_a_word("BENCH", "STANDARD KITCHEN")
    # A shared generic word is not enough: every naming word of the vendor's must be there.
    assert not worklist._shares_a_word("STANDARD VANITY", "STANDARD KITCHEN")
    assert not worklist._shares_a_word("TYPE A", "TYPE A")


# ---- a whole set -------------------------------------------------------------------------------


def test_two_pages_of_one_title_the_one_printing_the_vendors_sizes_pairs() -> None:
    pair = _pair(3, _sizes(**LOOK_ALIKE))
    assert _paired_pages(pair) == [3], pair.why
    assert 'of the 2 pages titled "SAMPLE PANTRY CABINET", its sizes match clearly best' in pair.why
    assert "prints the vendor's overall 63\"" in pair.why
    assert not pair.unsure


def test_the_matcher_sure_of_the_page_that_matches_less_is_never_paired() -> None:
    pair = _pair(2, _sizes(**LOOK_ALIKE))
    assert pair.chosen == (), pair.why
    assert pair.unsure and DRAWING_NEEDED not in pair.why
    assert "the swap check among the 2 pages titled" in pair.why


def test_two_pages_of_one_title_matching_alike_stay_pairing_unsure() -> None:
    pair = _pair(3, _sizes(p2=(63, 40, 36), p3=(63, 40, 36)))
    assert pair.chosen == (), pair.why
    assert pair.unsure and "match its sizes alike" in pair.why


def test_the_sizes_never_pair_type_1_with_type_3() -> None:
    sheets = {**SHEETS, 2: "SAMPLE VANITY - TYPE 3", 3: "SAMPLE VANITY - TYPE 3"}
    vendor = {1: "SAMPLE VANITY - TYPE 1"}
    sizes = _sizes(**LOOK_ALIKE)
    unasked = _work(None, vendor, sheets, sizes)
    pair = _across(_work(_matched(unasked, {1: 3}), vendor, sheets, sizes), 1)
    assert pair.chosen == (), pair.why
    assert "match clearly best" not in pair.why


def test_a_view_of_a_paired_sheet_is_picked_by_its_sizes_when_nothing_else_tells() -> None:
    vendor = Facts("v", values=(F(111, 2), F(51, 2)))
    plan = Facts("s.v1", values=(F(111, 2), F(25)))
    elevation = Facts("s.v2", values=(F(36), F(30)))
    best = worklist._view_by_sizes(vendor, [elevation, plan])
    assert best is not None and best.ref == "s.v1"
    assert "prints the vendor's overall 55 1/2\"" in best.evidence[0]
    # Two views printing it alike: none.
    assert worklist._view_by_sizes(vendor, [plan, Facts("s.v3", values=(F(111, 2),))]) is None
    # Only added up on the view, never printed: none.
    added = Facts("s.v4", size_rows=(("h", (F(27), F(57, 2))),))
    assert worklist._view_by_sizes(vendor, [elevation, added]) is None
    # A view holding strong evidence (its item tag) keeps narrowing's refusal: sizes never
    # override it.
    tagged = Facts("s.v5", tags=frozenset({"XX810"}), values=(F(36),))
    me = Facts("v", values=vendor.values, tags=frozenset({"XX810"}), item_tags=frozenset({"XX810"}))
    assert worklist._view_by_sizes(me, [tagged, plan]) is None
