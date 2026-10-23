"""Pairing a two-file upload on the item titles the set index read and code checked.

A real two-file upload (one vendor PDF, one architect PDF, no GV headings) had the AI page matcher
pick the right architect page for every vendor page, sure of each; code accepted 4 of 20, because
a title counted as evidence only when GV typed it. Now:

- the item title the set index read on both pages (each checked printed) is strong evidence when
  the two are the same after a fixed tidy-up (capitals, signs, the hand words: "SAMPLE VANITY - TYPE 1
  REVERSED UNITS" is "Vanity type-1"); only an exact match counts ("KITCHEN" is not
  "KITCHENETTE");
- two numbered unit types that differ ("TYPE 1", "TYPE 3") rule the pair out;
- a page the matcher is sure of and code cannot confirm is "pairing unsure", naming both
  candidate pages as a person finds them: never "drawing needed", never compared;
- no reason names the matcher's picture labels ("A5") or code's ids ("p1009").

Synthetic sets only: $0, no model call (the set index and the page matcher are fake answers).
"""

from __future__ import annotations

import re
from typing import Any

from app.review.assistant.chat_contract import ChatCoveragePageOut
from app.review.assistant.full_check_plan import (
    UNPAIRED_NOTE,
    UNSURE_NOTE,
    CrossPairs,
    unpaired_note_why,
    unpaired_words,
    unsure_words,
)
from app.review.assistant.full_check_roster import unpaired_note
from app.review.assistant.simple_check import worklist
from app.review.assistant.simple_check.pairing import (
    PAIRING_UNSURE,
    Facts,
    MatchAnswer,
    Scored,
    clear_winner,
    opposite_hand,
    rank,
    same_item,
    unit_title,
)
from app.review.assistant.simple_check.worklist import DRAWING_NEEDED, worklist_from
from tests.review.assistant.test_cross_file_projects import _entry, _label, _phrase, _read

SHOP, ARCH = "shop.pdf", "arch.pdf"

#: The vendor pages' item titles, as the set index read them.
VENDOR = {1: "SAMPLE VANITY - TYPE 1 REVERSED UNITS", 2: "KITCHEN", 3: "SAMPLE VANITY - TYPE 1"}
#: The architect pages' item titles.
ARCHITECT = {
    1: "SAMPLE VANITY - TYPE 3",
    2: "SAMPLE VANITY TYPE-1",
    3: "KITCHENETTE",
    4: "BENCH",
    5: "WARDROBE",
}
#: A private id or a matcher label in a reason.
_PRIVATE = re.compile(r"\bp\d{3,5}\b|\bA\d{1,3}\b")


def _sizes(side: str, sizes: tuple[int, int, int] = (63, 22, 34)) -> list[dict[str, Any]]:
    """Three sizes; the same on every page but the kitchenette's: sizes alone never tell the
    vanities apart."""
    w, d, h = sizes
    return [
        _label(1, f'{w}"', w, [300, 200, 320, 210], side, "h"),
        _label(2, f'{d}"', d, [130, 300, 140, 320], side, "v"),
        _label(3, f'{h}"', h, [500, 300, 510, 320], side, "v"),
    ]


def _work(
    matches: dict[str, Any] | None,
    vendor: dict[int, str] | None = None,
    architect: dict[int, str] | None = None,
    sizes: dict[int, tuple[int, int, int]] | None = None,
    two_on: int | None = None,
    sheet_views: list[dict[str, Any]] | None = None,
) -> Any:
    """The synthetic set: `vendor` and `architect` titles by page; `sizes` an architect page's
    sizes (else the kitchenette's own, the rest the vendor's); `two_on` a vendor page holding two
    vendor drawings; `sheet_views` the set index's views of architect page 1."""
    vendor = vendor or VENDOR
    architect = architect or ARCHITECT
    sizes = sizes if sizes is not None else {3: (100, 40, 36)}
    pages = [_entry(SHOP, p, "vendor") for p in vendor] + [
        _entry(ARCH, p, "architect") for p in architect
    ]
    if two_on is not None:
        pages[two_on - 1]["halves"] = [
            {"box": [0.0, 0.0, 600.0, 800.0], "role": "vendor", "from_upload": True},
            {"box": [600.0, 0.0, 1200.0, 800.0], "role": "vendor", "from_upload": True},
        ]
    dims = {
        "pages": [
            *(
                {
                    "file": SHOP,
                    "page": p,
                    "labels": _sizes("vendor"),
                    "phrases": [_phrase(t, [700, 700, 900, 710])],
                }
                for p, t in vendor.items()
            ),
            *(
                {
                    "file": ARCH,
                    "page": p,
                    "labels": _sizes("architect", sizes.get(p, (63, 22, 34))),
                    "phrases": [
                        _phrase(t, [700, 700, 900, 710]),
                        # On a sheet of views, the plan names the piece (its title block, at the
                        # foot, is outside both views).
                        *(
                            [_phrase("SAMPLE VANITY", [100, 100, 300, 110])]
                            if sheet_views and p == 1
                            else []
                        ),
                    ],
                }
                for p, t in architect.items()
            ),
        ]
    }
    index = {
        "version": 1,
        "pages": {
            **{f"{SHOP}#{p}": _read(SHOP, p, item=t) for p, t in vendor.items()},
            **{
                f"{ARCH}#{p}": _read(
                    ARCH, p, item=t, views=sheet_views if sheet_views and p == 1 else []
                )
                for p, t in architect.items()
            },
        },
    }
    return worklist_from(
        {
            "files": [SHOP, ARCH],
            "upload_roles": {SHOP: "vendor", ARCH: "architect"},
            "pages": pages,
        },
        dims=dims,
        set_index=index,
        matches=matches,
        main_file=SHOP,
    )


def _across(work: Any, page: int) -> Any:
    return next(p for p in work.across if p.vendor.own_page == page)


def _ids(work: Any, page: int) -> dict[int, str]:
    """The architect pages' item ids, by their page in the architect's file."""
    return {c.page: c.id for c in _across(work, page).candidates}


def _matched(work: Any, picks: dict[int, int], reason: str = "the same piece") -> dict[str, Any]:
    """The matcher's saved answers: sure of architect page `picks[vendor page]`, shown every
    candidate under its own picture label (A1, A2, ...)."""
    answers = {}
    for vendor, arch in picks.items():
        pair = _across(work, vendor)
        shown = [c.id for c in pair.candidates]
        ids = _ids(work, vendor)
        answers[pair.vendor.ref] = {
            "candidates": shown,
            "matches": [ids[arch]],
            "confident": True,
            "reason": f"{reason}: the plan on A{shown.index(ids[arch]) + 1}",
            "labels": {f"A{n + 1}": ref for n, ref in enumerate(shown)},
            "prompt_version": 1,
        }
    return {"version": 1, "answers": answers}


def _paired_pages(pair: Any) -> list[int]:
    by = {c.id: c.page for c in pair.candidates}
    return sorted(by[c] for c in pair.chosen)


# ---- the tidy-up -----------------------------------------------------------------------------------


def test_titles_match_exactly_after_a_fixed_tidy_up() -> None:
    assert unit_title("Vanity - TYPE-1 Reversed Units") == ("vanity type 1", frozenset({"1"}))
    assert unit_title("KITCHEN REGULAR UNITS") == ("kitchen", frozenset())
    # A list of types is every one of them: TYPE 2 is on a "TYPE 1 & 2" page.
    assert unit_title("SAMPLE VANITY TYPE 1 & 2")[1] == frozenset({"1", "2"})
    # A mirror is an object, never tidied away as a hand.
    assert not same_item("GUEST BATH VANITY MIRROR", "GUEST BATH VANITY")
    assert same_item("SAMPLE VANITY - TYPE 1 REVERSED UNITS", "SAMPLE VANITY TYPE-1")
    assert same_item("ACCESSIBLE KITCHEN REVERSE UNITS", "Accessible Kitchen")
    # Only an exact match: a near name is a clue, never proof.
    assert not same_item("KITCHEN", "KITCHENETTE")
    assert not same_item("KITCHEN", "STANDARD KITCHEN")
    assert not same_item("SAMPLE VANITY - TYPE 1", "SAMPLE VANITY - TYPE 3")
    # Generic names repeat from one unit to the next: two words besides the unit type, or none.
    assert not same_item("UNIT", "UNIT")
    assert not same_item("KITCHEN", "KITCHEN")
    assert not same_item("UNIT TYPE 1", "UNIT TYPE 1")
    assert not same_item(None, "SAMPLE VANITY")
    # A hand word code does not read as a hand is never left out ("OPP" stays in).
    assert not same_item("SAMPLE KITCHEN OPP", "SAMPLE KITCHEN")


def test_type_1_never_pairs_with_type_3() -> None:
    vendor = Facts("p1.v0", index_title="SAMPLE VANITY - TYPE 1")
    ranked = {
        s.ref: s
        for s in rank(
            vendor,
            [
                Facts("a1", index_title="SAMPLE VANITY - TYPE 3"),
                Facts("a2", index_title="SAMPLE VANITY TYPE-1"),
            ],
        )
    }
    assert ranked["a1"].contradicts is not None and "another unit type" in ranked["a1"].contradicts
    assert ranked["a2"].contradicts is None and ranked["a2"].strong


# ---- the work list --------------------------------------------------------------------------------


def test_the_same_checked_title_pairs_and_type_3_is_ruled_out() -> None:
    unasked = _work(None)
    work = _work(_matched(unasked, {1: 2}))
    pair = _across(work, 1)
    assert _paired_pages(pair) == [2], pair.why
    by = {c.page: c for c in pair.candidates}
    assert by[1].contradicts is not None  # TYPE 3
    # Same sizes everywhere: before, only the matcher pointed at page 2, and code refused it.
    assert (
        'titled "SAMPLE VANITY TYPE-1"' in pair.why or 'is its "SAMPLE VANITY TYPE-1"' in pair.why
    )
    # Reversed units against an unmarked page: compared without positions from an end.
    assert pair.opposite_hand is not None


def test_a_sure_matcher_pick_of_type_3_is_never_paired() -> None:
    unasked = _work(None)
    pair = _across(_work(_matched(unasked, {3: 1})), 3)
    # Code rules TYPE 3 out, and the title alone never pairs without the matcher: a person
    # confirms, offered the page of the same title first, never "no drawing".
    assert pair.chosen == (), pair.why
    ids = _ids(unasked, 3)
    assert pair.unsure == (ids[2], ids[1])
    assert DRAWING_NEEDED not in pair.why


def test_matcher_sure_but_code_unsure_is_pairing_unsure_never_drawing_needed() -> None:
    unasked = _work(None)
    work = _work(_matched(unasked, {2: 3}))
    pair = _across(work, 2)
    assert pair.chosen == () and not pair.needed
    assert pair.unsure and pair.unsure[0] == _ids(unasked, 2)[3]
    assert f'{PAIRING_UNSURE}: architect page 3 ("KITCHENETTE") or architect page ' in pair.why
    assert DRAWING_NEEDED not in pair.why
    # The run's pages: unpaired with that reason; the screen and the files name both pages.
    cross = CrossPairs.of(work)
    assert 2 in cross.unpaired and 2 not in cross.pairs
    words = unpaired_words(cross.unpaired[2])
    assert words.startswith('pairing unsure: architect page 3 ("KITCHENETTE") or architect page')
    assert words.endswith("?")
    note = UNSURE_NOTE.format(why=unsure_words(cross.unpaired[2]))
    page = ChatCoveragePageOut(page=2, status="not_comparable", note=note)
    assert page.pairing_unsure and not page.drawing_needed
    assert page.pairing_unsure_why == unsure_words(cross.unpaired[2])
    assert unpaired_note_why(note) is None and unpaired_note(note)
    # An ordinary unpaired page stays "drawing needed", as before.
    plain = ChatCoveragePageOut(
        page=4, status="not_comparable", note=UNPAIRED_NOTE.format(why="no drawing")
    )
    assert plain.drawing_needed and not plain.pairing_unsure


def test_no_matcher_label_or_private_id_in_any_reason() -> None:
    unasked = _work(None)
    work = _work(_matched(unasked, {1: 2, 2: 3, 3: 1}))
    for pair in work.across:
        assert not _PRIVATE.search(pair.why), pair.why
        assert "architect page" in pair.why
    for item in work.items:
        assert not _PRIVATE.search(item.paired_by or ""), item.paired_by
    for skipped in work.skipped:
        assert not _PRIVATE.search(skipped.reason), skipped.reason
    # The matcher's "the plan on A2" is said as the page it stands for.
    assert 'the plan on architect page 2 ("SAMPLE VANITY TYPE-1")' in _across(work, 1).why


def test_a_hand_named_on_both_sides_is_never_compared_by_its_positions() -> None:
    # An architect page listing its unit's reverse type beside the regular one says nothing of
    # which hand it draws: positions from an end are not compared.
    both = opposite_hand(
        Facts("p1.v0", hand=frozenset({"REVERSED UNITS"})), Facts("a1", hand=frozenset({"REVERSE"}))
    )
    assert both is not None and "not sure" in both
    assert opposite_hand(Facts("p1.v0"), Facts("a1")) is None


def test_the_reviewer_is_shown_the_two_pages_in_the_right_order() -> None:
    sure = MatchAnswer(("a1",), confident=True)
    ranked = (Scored("a2", 5.0, ("x",), strong=True), Scored("a1", 1.0, ("y",)))
    # Code could not confirm the matcher's pick: the matcher's first, then code's first other.
    assert worklist._unsure(ranked, None, sure) == ("a1", "a2")
    # Code rules the pick out and picked another: code's pick first.
    typed = "the vendor's sheet is X and this one is Y: another unit type"
    ruled = (Scored("a2", 5.0, ("x",), strong=True), Scored("a1", 1.0, ("y",), contradicts=typed))
    assert worklist._unsure(ruled, "a2", sure) == ("a2", "a1")
    # Ruled out by its unit type alone, no pick of code's own: the matcher's page, to a person.
    assert worklist._unsure(ruled, None, sure) == ("a1",)
    # Ruled out as another variant: no drawing of this item (it stays "drawing needed").
    variant = (Scored("a1", 1.0, ("y",), contradicts="it is marked ADA and the vendor's is not"),)
    assert worklist._unsure(variant, None, sure) == ()
    # The matcher's own next pick is the second page offered.
    both = MatchAnswer(("a1", "a3"), confident=True)
    three = (*ranked, Scored("a3", 0.5, ("z",)))
    assert worklist._unsure(three, None, both) == ("a1", "a3")
    # A matcher not sure, or no answer: nothing to choose.
    assert worklist._unsure(ranked, None, MatchAnswer(("a1",), confident=False)) == ()
    assert worklist._unsure(ranked, None, None) == ()


# ---- what the independent review found ---------------------------------------------------------

LOOK_ALIKE = {
    1: "SAMPLE PANTRY CABINET",
    2: "BENCH",
    3: "SAMPLE PANTRY CABINET",
    4: "WARDROBE",
    5: "DESK",
}
#: No size the vendor prints: only the titles tell the pages apart.
OTHER_SIZES = {p: (100, 40, 36) for p in LOOK_ALIKE}


def test_two_pages_of_one_title_far_apart_are_look_alikes_never_paired() -> None:
    vendor = {1: "SAMPLE PANTRY CABINET"}
    unasked = _work(None, vendor, LOOK_ALIKE, OTHER_SIZES)
    pair = _across(_work(_matched(unasked, {1: 3}), vendor, LOOK_ALIKE, OTHER_SIZES), 1)
    assert pair.chosen == (), pair.why
    assert pair.unsure and DRAWING_NEEDED not in pair.why
    # One item drawn on two pages in a row (its sheets): paired with the matcher's pick.
    sheets = {**LOOK_ALIKE, 2: "SAMPLE PANTRY CABINET", 3: "BENCH"}
    unasked = _work(None, vendor, sheets, OTHER_SIZES)
    pair = _across(_work(_matched(unasked, {1: 2}), vendor, sheets, OTHER_SIZES), 1)
    assert _paired_pages(pair) == [2], pair.why


def test_a_vendor_page_of_two_drawings_gives_neither_its_title() -> None:
    vendor = {1: "SAMPLE PANTRY CABINET"}
    architect = {**LOOK_ALIKE, 3: "SAMPLE DESK"}
    unasked = _work(None, vendor, architect, OTHER_SIZES, two_on=1)
    for pair in unasked.across:
        assert all("titled" not in " ".join(c.evidence) for c in pair.candidates), pair.why


def test_the_whole_sheet_is_never_compared_when_code_rules_a_view_out() -> None:
    standard = Facts("p1.v0", index_title="SAMPLE VANITY TOP")
    views = [Facts("v1"), Facts("v2", markers=frozenset({"ACCESSIBLE"}))]
    assert worklist._view_ruled_out(standard, views, "SAMPLE VANITY TOP")
    # The sheet's own title says accessible: every view on it is the accessible one.
    marked = Facts("p1.v0", index_title="ACCESSIBLE VANITY TOP", markers=frozenset({"ACCESSIBLE"}))
    plain_views = [Facts("v1", markers=frozenset({"ACCESSIBLE"})), Facts("v2")]
    assert not worklist._view_ruled_out(marked, plain_views, "ACCESSIBLE VANITY TOP")
    assert worklist._view_ruled_out(marked, plain_views, "VANITY TOP")


def test_a_label_that_could_be_a_printed_name_drops_the_matchers_reason() -> None:
    labels = {"A1": "p1001", "A2": "p1002"}
    assert worklist._without_labels("the plan on A2.", labels) == "the plan on p1002."
    # "UNIT A2" is printed on the drawings: which page is meant is not known.
    assert worklist._without_labels("matches Unit A2 kitchen", labels) == ""
    assert worklist._without_labels("see sheet A501", labels) == ""
    assert worklist._without_labels("no label at all", labels) == "no label at all"


def test_the_title_alone_never_pairs_without_the_matcher() -> None:
    # An AI read the titles: a second witness, looking at the drawings, must agree.
    vendor = {1: "SAMPLE PANTRY CABINET"}
    architect = {**LOOK_ALIKE, 3: "SAMPLE DESK"}
    pair = _across(_work(None, vendor, architect, OTHER_SIZES), 1)
    assert pair.code_pick is None and pair.chosen == (), pair.why
    unasked = _work(None, vendor, architect, OTHER_SIZES)
    agreed = _across(_work(_matched(unasked, {1: 1}), vendor, architect, OTHER_SIZES), 1)
    assert _paired_pages(agreed) == [1], agreed.why


def _sheet_of_two_views(title: str) -> Any:
    """Vendor page 1 "SAMPLE VANITY TOP"; architect page 1 a sheet of a PLAN and an ELEVATION
    titled `title`; the matcher sure of it."""
    vendor = {1: "SAMPLE VANITY TOP"}
    architect = {1: title, 2: "BENCH", 3: "WARDROBE", 4: "DESK"}

    views = [
        {
            "title": "PLAN",
            "number": "",
            "kind": "plan",
            "prints_sizes": True,
            "box": [0.0, 0.0, 600.0, 650.0],
            "variant_words": [],
            "hand_words": [],
        },
        {
            "title": "ELEVATION",
            "number": "",
            "kind": "elevation",
            "prints_sizes": True,
            "box": [600.0, 0.0, 1200.0, 650.0],
            "variant_words": [],
            "hand_words": [],
        },
    ]
    sizes = {p: (100, 40, 36) for p in architect}
    unasked = _work(None, vendor, architect, sizes, sheet_views=views)
    return _across(_work(_matched(unasked, {1: 1}), vendor, architect, sizes, sheet_views=views), 1)


def test_a_reversed_sheet_title_flags_the_pair_whatever_view_it_is_narrowed_to() -> None:
    pair = _sheet_of_two_views("SAMPLE VANITY TOP REVERSED UNITS")
    assert pair.chosen, pair.why
    assert pair.opposite_hand is not None, pair.why
    plain = _sheet_of_two_views("SAMPLE VANITY TOP")
    assert plain.chosen and plain.opposite_hand is None, plain.why


def test_code_never_picks_on_the_title_weight() -> None:
    # A title plus sizes too few to pick on: code's own pick stands on code's evidence alone.
    titled = Scored("a1", 5.33, ("t", "s"), strong=True, titled=4.0)
    other = Scored("a2", 1.0, ("w",))
    assert clear_winner((titled, other)) is None
    assert clear_winner((Scored("a1", 5.33, ("s",), strong=True), other)) is not None
