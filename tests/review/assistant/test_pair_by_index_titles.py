"""Pairing a two-file upload on the item titles the set index read and code checked.

A real two-file upload (one vendor PDF, one architect PDF, no GV headings) had the AI page matcher
pick the right architect page for every vendor page, sure of each; code accepted 4 of 20, because
a title counted as evidence only when GV typed it. Now:

- the item title the set index read on both pages (each checked printed) is strong evidence when
  the two are the same after a fixed tidy-up (capitals, signs, the hand words: "VANITY - TYPE 1
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
    opposite_hand,
    rank,
    same_item,
    unit_title,
)
from app.review.assistant.simple_check.worklist import DRAWING_NEEDED, worklist_from
from tests.review.assistant.test_cross_file_projects import _entry, _label, _phrase, _read

SHOP, ARCH = "shop.pdf", "arch.pdf"

#: The vendor pages' item titles, as the set index read them.
VENDOR = {1: "VANITY - TYPE 1 REVERSED UNITS", 2: "KITCHEN", 3: "VANITY - TYPE 1"}
#: The architect pages' item titles.
ARCHITECT = {1: "VANITY - TYPE 3", 2: "VANITY TYPE-1", 3: "KITCHENETTE", 4: "BENCH", 5: "WARDROBE"}
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


def _work(matches: dict[str, Any] | None) -> Any:
    pages = [_entry(SHOP, p, "vendor") for p in VENDOR] + [
        _entry(ARCH, p, "architect") for p in ARCHITECT
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
                for p, t in VENDOR.items()
            ),
            *(
                {
                    "file": ARCH,
                    "page": p,
                    "labels": _sizes("architect", (100, 40, 36) if p == 3 else (63, 22, 34)),
                    "phrases": [_phrase(t, [700, 700, 900, 710])],
                }
                for p, t in ARCHITECT.items()
            ),
        ]
    }
    index = {
        "version": 1,
        "pages": {
            **{f"{SHOP}#{p}": _read(SHOP, p, item=t) for p, t in VENDOR.items()},
            **{f"{ARCH}#{p}": _read(ARCH, p, item=t) for p, t in ARCHITECT.items()},
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
    assert unit_title("Vanity - TYPE-1 Reversed Units") == ("vanity type 1", "1")
    assert unit_title("KITCHEN REGULAR UNITS") == ("kitchen", None)
    assert same_item("VANITY - TYPE 1 REVERSED UNITS", "VANITY TYPE-1")
    assert same_item("ACCESSIBLE KITCHEN REVERSE UNITS", "Accessible Kitchen")
    # Only an exact match: a near name is a clue, never proof.
    assert not same_item("KITCHEN", "KITCHENETTE")
    assert not same_item("KITCHEN", "STANDARD KITCHEN")
    assert not same_item("VANITY - TYPE 1", "VANITY - TYPE 3")
    assert not same_item("UNIT", "UNIT")  # says nothing of the item
    assert not same_item(None, "VANITY")


def test_type_1_never_pairs_with_type_3() -> None:
    vendor = Facts("p1.v0", index_title="VANITY - TYPE 1")
    ranked = {
        s.ref: s
        for s in rank(
            vendor,
            [Facts("a1", index_title="VANITY - TYPE 3"), Facts("a2", index_title="VANITY TYPE-1")],
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
    assert 'titled "VANITY TYPE-1"' in pair.why or 'is its "VANITY TYPE-1"' in pair.why
    # Reversed units against an unmarked page: compared without positions from an end.
    assert pair.opposite_hand is not None


def test_a_sure_matcher_pick_of_type_3_is_never_paired() -> None:
    unasked = _work(None)
    pair = _across(_work(_matched(unasked, {3: 1})), 3)
    # Code rules TYPE 3 out, so the matcher's saved pick no longer stands among the candidates
    # it would be shown now; code pairs the one page of the same title, never the TYPE 3 page.
    assert _paired_pages(pair) == [2], pair.why
    assert 1 not in _paired_pages(pair)


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
    assert 'the plan on architect page 2 ("VANITY TYPE-1")' in _across(work, 1).why


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
    ruled = (Scored("a2", 5.0, ("x",), strong=True), Scored("a1", 1.0, ("y",), contradicts="type"))
    assert worklist._unsure(ruled, "a2", sure) == ("a2", "a1")
    # Ruled out with no pick of code's own, a matcher not sure, or no answer: nothing to choose.
    assert worklist._unsure(ruled, None, sure) == ()
    assert worklist._unsure(ranked, None, MatchAnswer(("a1",), confident=False)) == ()
    assert worklist._unsure(ranked, None, None) == ()
