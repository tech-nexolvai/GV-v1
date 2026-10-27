"""The $0 code pairing pass before the paid AI page matcher (final plan, changes 6 and 12).

On a real four-hotel two-file upload only 14 of 33 pairable vendor drawings paired, every pair cost
a paid matcher call, and code never paired on its own evidence. Now:

- code pairs a vendor drawing by itself when its best architect item holds two kinds of code
  evidence, one naming the item (its title, the vendor's title naming its views, a printed
  pointer), a clear margin over the next item, and no rival code also names; the matcher is then
  not asked (`page_match.to_ask`);
- the vendor's item title names an architect sheet's views ("Staff Lounge Casework" and
  "STAFF LOUNGE 3"); a room's views of one title are compared together;
- a matcher pick code names alone, clearly first, pairs even when the matcher is not sure;
- the vendor's own sheets of one item share its pair;
- a mirror is proved from the order of the widths (read backwards, never forwards): left and right
  swap when ends are compared;
- the pair-swap audit: two vendor drawings whose sizes fit each other's same-titled pages better
  are both "pairing unsure";
- the whole-drawing look is spent only where code read a size on both sides.

Synthetic sets only: $0, no model call (the set index and the matcher are fake answers).
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from app.review.assistant import page_match, whole_look
from app.review.assistant.simple_check.pairing import (
    PAIRING_UNSURE,
    Facts,
    MatchAnswer,
    Scored,
    agree,
    code_pass,
    mirror_proof,
    named_by,
    piece_words,
    plan_and_elevation,
    rank,
    room_views,
    swap_audit,
)
from app.review.assistant.simple_check.verify import ends_match, mirror_sides
from app.review.assistant.simple_check.worklist import DRAWING_NEEDED, worklist_from
from tests.review.assistant.test_cross_file_projects import _entry, _label, _phrase, _read

F = Fraction
SHOP, ARCH = "shop.pdf", "arch.pdf"


def _sizes(side: str, sizes: tuple[int, int, int]) -> list[dict[str, Any]]:
    w, d, h = sizes
    return [
        _label(1, f'{w}"', w, [300, 200, 320, 210], side, "h"),
        _label(2, f'{d}"', d, [130, 300, 140, 320], side, "v"),
        _label(3, f'{h}"', h, [500, 300, 510, 320], side, "v"),
    ]


def _work(
    vendor: dict[int, tuple[str, tuple[int, int, int]]],
    architect: dict[int, tuple[str, tuple[int, int, int]]],
    matches: dict[str, Any] | None = None,
) -> Any:
    """A two-file set: each page's set index item title and its three sizes (width, depth,
    height)."""
    pages = [_entry(SHOP, p, "vendor") for p in vendor] + [
        _entry(ARCH, p, "architect") for p in architect
    ]
    dims = {
        "pages": [
            *(
                {
                    "file": SHOP,
                    "page": p,
                    "labels": _sizes("vendor", sizes),
                    "phrases": [_phrase(title, [700, 700, 900, 710])],
                }
                for p, (title, sizes) in vendor.items()
            ),
            *(
                {
                    "file": ARCH,
                    "page": p,
                    "labels": _sizes("architect", sizes),
                    "phrases": [_phrase(title, [700, 700, 900, 710])],
                }
                for p, (title, sizes) in architect.items()
            ),
        ]
    }
    index = {
        "version": 1,
        "pages": {
            **{f"{SHOP}#{p}": _read(SHOP, p, item=t) for p, (t, _s) in vendor.items()},
            **{f"{ARCH}#{p}": _read(ARCH, p, item=t) for p, (t, _s) in architect.items()},
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


def _paired_pages(pair: Any) -> list[int]:
    by = {c.id: c.page for c in pair.candidates}
    return sorted(by[c] for c in pair.chosen)


#: Made-up sizes: the vanity's, and three other pieces'.
VANITY = (63, 22, 34)
OTHERS = {2: ("BENCH", (100, 40, 36)), 3: ("WARDROBE", (80, 30, 72)), 4: ("DESK", (50, 20, 30))}


# ---- the words of a title --------------------------------------------------------------------------


def test_a_titles_piece_words_and_who_names_a_view() -> None:
    assert piece_words("STAFF LOUNGE 3") >= {"STAFF", "LOUNGE"}
    # The place after "@" is not the piece; generic words name nothing.
    assert piece_words("Sample Juice & Water Station @ Foyer") >= {"JUICE", "WATER"}
    assert "FOYER" not in piece_words("Sample Juice & Water Station @ Foyer")
    assert piece_words("BLOCKING DETAIL @ WASHROOM") == frozenset()
    assert piece_words("TYPICAL ELEVATION") == frozenset()
    # Two words side by side are also one: a "WORK DESK" is a "Workdesk".
    mine = piece_words("Large Workdesk Casework @ Back Office")
    assert named_by("WORK DESK ELEVATION", mine)
    assert named_by("CONFERENCE ROOM ELEVATION", piece_words("Two Bay Conference Room Casework"))
    # A view naming more than the vendor's title (which washroom?) is not named by it.
    assert not named_by("EAST WASHROOM ELEVATION", piece_words("Washroom Vanity Left"))
    assert not named_by("SAMPLE GUEST BATH MIRROR", piece_words("SAMPLE GUEST BATH VANITY"))
    assert not named_by("PLAN", mine)


def test_the_vendors_title_naming_one_items_views_is_title_evidence() -> None:
    vendor = Facts("v", index_title="Staff Lounge Casework @ Back Office")
    ranked = {
        s.ref: s
        for s in rank(
            vendor,
            [
                Facts("a1", view_titles=("CONFERENCE ROOM ELEVATION", "STAFF LOUNGE 1")),
                Facts("a2", view_titles=("FOYER ELEVATION",)),
                Facts("a3", view_titles=("FITNESS ELEVATION",)),
            ],
        )
    }
    assert "title" in ranked["a1"].kinds and ranked["a1"].strong
    # Agree-only: it never makes `clear_winner`'s pick alone (an AI read the vendor's title).
    assert ranked["a1"].agree_only and ranked["a1"].view_titled > 0
    assert not ranked["a2"].kinds
    # A reflected ceiling plan never draws a piece: ruled out.
    ceiling = rank(
        vendor, [Facts("a1", view_titles=("1ST LEVEL REFLECTED CEILING PLAN", "LIGHTING LEGEND"))]
    )
    assert ceiling[0].contradicts is not None


# ---- the code pass ----------------------------------------------------------------------------------


def _scored(ref: str, score: float, kinds: set[str]) -> Scored:
    return Scored(ref, score, ("x",), strong=bool(kinds), kinds=frozenset(kinds))


def test_the_code_pass_needs_two_kinds_a_name_and_a_margin() -> None:
    other = _scored("a2", 1.0, set())
    assert code_pass((_scored("a1", 6.0, {"title", "sizes"}), other)).ref == "a1"  # type: ignore[union-attr]
    # One kind alone, however strong, is never enough.
    assert code_pass((_scored("a1", 9.0, {"title"}), other)) is None
    # Two kinds, but none names the item (sizes and a section are look-alike evidence).
    assert code_pass((_scored("a1", 9.0, {"sizes", "section"}), other)) is None
    # No clear margin.
    assert code_pass((_scored("a1", 2.5, {"title", "sizes"}), other)) is None
    # The next item is named by code too: a conflict, never a pair.
    named = _scored("a2", 1.0, {"pointer"})
    assert code_pass((_scored("a1", 6.0, {"title", "sizes"}), named)) is None
    # Any other item named by code (not only the next one) is a conflict.
    third = (
        _scored("a1", 6.0, {"index_title", "sizes"}),
        _scored("a3", 2.2, set()),
        _scored("a2", 2.0, {"pointer"}),
    )
    assert code_pass(third) is None
    # A ruled-out item does not count as the next one.
    ruled = Scored("a2", 5.9, ("x",), contradicts="another unit type", kinds=frozenset({"title"}))
    assert code_pass((_scored("a1", 6.0, {"title", "sizes"}), ruled)).ref == "a1"  # type: ignore[union-attr]


def test_a_matcher_pick_code_names_alone_clearly_first_pairs_even_when_unsure() -> None:
    ranked = (_scored("a1", 4.0, {"title"}), _scored("a2", 1.0, set()))
    chosen, why = agree(ranked, None, MatchAnswer(("a1",), False, "the lounge"))
    assert chosen == ("a1",) and "code's own evidence names it" in why
    # Not clearly first: still unsure.
    close = (_scored("a1", 2.0, {"title"}), _scored("a2", 1.0, set()))
    assert agree(close, None, MatchAnswer(("a1",), False))[0] == ()
    # The set index's title alone (an AI's read) and an unsure matcher: two AI reads, unsure.
    titled = (_scored("a1", 6.0, {"index_title"}), _scored("a2", 1.0, set()))
    assert agree(titled, None, MatchAnswer(("a1",), False))[0] == ()
    # Code names nothing: an unsure matcher pick stays unsure.
    weak = (_scored("a1", 4.0, set()), _scored("a2", 1.0, set()))
    assert agree(weak, None, MatchAnswer(("a1",), False))[0] == ()


def test_code_pairs_alone_on_the_same_title_and_its_own_width_and_the_matcher_is_not_asked() -> (
    None
):
    work = _work(
        {1: ("SAMPLE VANITY - TYPE 1", VANITY)},
        {1: ("SAMPLE VANITY TYPE-1", VANITY), **OTHERS},
    )
    pair = _across(work, 1)
    assert pair.code_paired and _paired_pages(pair) == [1], pair.why
    assert pair.answer is None and "no AI page matcher answer" in pair.why
    assert page_match.to_ask([pair], _NoFolder())[0] == []


def test_a_width_every_page_prints_is_no_witness_and_the_matcher_is_asked() -> None:
    work = _work(
        {1: ("SAMPLE VANITY - TYPE 1", VANITY)},
        {1: ("SAMPLE VANITY TYPE-1", VANITY), 2: ("BENCH", VANITY), 3: ("WARDROBE", VANITY)},
    )
    pair = _across(work, 1)
    # Not code's own pass: the matcher is asked (its answer may still refuse code's older pick).
    assert not pair.code_paired
    assert page_match.to_ask([pair], _NoFolder())[0] == [pair]


def test_a_saved_matcher_answer_naming_another_page_still_refuses_codes_pick() -> None:
    vendor = {1: ("SAMPLE VANITY - TYPE 1", VANITY)}
    architect = {1: ("SAMPLE VANITY TYPE-1", VANITY), **OTHERS}
    unasked = _across(_work(vendor, architect), 1)
    ids = {c.page: c.id for c in unasked.candidates}
    answer = {
        "version": 1,
        "answers": {
            unasked.vendor.ref: {
                "candidates": [c.id for c in unasked.candidates],
                "matches": [ids[3]],
                "confident": True,
                "reason": "the same piece",
                "prompt_version": 1,
            }
        },
    }
    pair = _across(_work(vendor, architect, answer), 1)
    assert pair.chosen == () and pair.unsure, pair.why
    assert f"{PAIRING_UNSURE}:" in pair.why and DRAWING_NEEDED not in pair.why


def test_the_vendors_sheets_of_one_item_share_its_pair() -> None:
    # Page 2 is the item's section sheet: it prints other sizes, so alone it has no size witness.
    work = _work(
        {
            1: ("SAMPLE PANTRY CABINET", VANITY),
            2: ("SAMPLE PANTRY CABINET", (24, 12, 84)),
        },
        {1: ("SAMPLE PANTRY CABINET", VANITY), **OTHERS},
    )
    first, second = _across(work, 1), _across(work, 2)
    assert first.code_paired and _paired_pages(first) == [1]
    assert _paired_pages(second) == [1], second.why
    assert "the vendor's sheets of one item" in second.why
    # Code's own pass did not pair it: the matcher is still asked, and its first pick must agree.
    assert not second.code_paired
    assert page_match.to_ask([first, second], _NoFolder())[0] == [second]


# ---- the room's views ------------------------------------------------------------------------------


def test_a_rooms_views_of_one_title_are_compared_together() -> None:
    vendor = Facts("v", index_title="Two Bay Conference Room Casework @ Conference Room")
    views = [
        Facts("s.view1", view_titles=("CONFERENCE ROOM ELEVATION",)),
        Facts("s.view2", view_titles=("CONFERENCE ROOM ELEVATION",)),
        Facts("s.view3", view_titles=("WORK DESK ELEVATION",)),
    ]
    assert [s.ref for s in room_views(vendor, views)] == ["s.view1", "s.view2"]
    # Numbered views may be two rooms or two pieces: never grouped.
    numbered = [
        Facts("s.view1", view_titles=("STAFF PANTRY 1",)),
        Facts("s.view2", view_titles=("STAFF PANTRY 2",)),
    ]
    assert room_views(Facts("v", index_title="Staff Pantry Casework"), numbered) == ()
    types = [
        Facts("s.view1", view_titles=("SAMPLE VANITY ELEVATION",)),
        Facts("s.view2", view_titles=("SAMPLE VANITY ELEVATION",)),
    ]
    assert room_views(Facts("v", index_title="Sample Vanity Type 1"), types) == ()
    # Two rooms named alike (which washroom?): none.
    washroom = Facts("v", index_title="Washroom Vanity Left")
    two_rooms = [
        Facts("s.view1", view_titles=("EAST WASHROOM ELEVATION",)),
        Facts("s.view2", view_titles=("FEEAST WASHROOM ELEVATION",)),
    ]
    assert room_views(washroom, two_rooms) == ()
    # An accessible variant among the views: a marker still tells them apart (ruled out).
    marked = Facts("v", index_title="Conference Room Casework", markers=frozenset({"ADA"}))
    variants = [
        Facts("s.view1", view_titles=("CONFERENCE ROOM ELEVATION",)),
        Facts("s.view2", view_titles=("CONFERENCE ROOM ELEVATION",), markers=frozenset({"ADA"})),
    ]
    assert room_views(marked, variants) == ()


def test_a_sheets_one_plan_and_one_elevation_are_compared_together() -> None:
    """A real upload (2026-10-27): an architect sheet of one item, its views titled only "PLAN"
    and "ELEVATION"; the vendor's drawing of the item stayed "pairing unsure" (made-up names)."""
    vendor = Facts("v", index_title="Sample Kitchenette")
    views = [
        Facts("s.view1", view_titles=("ELEVATION",)),
        Facts("s.view2", view_titles=("PLAN",)),
    ]
    found = plan_and_elevation(vendor, views)
    assert [s.ref for s in found] == ["s.view1", "s.view2"]
    assert "one plan and one elevation" in found[0].evidence[0]
    # Anything else is left to `narrow` as before: two elevations, a third view, a named view.
    two = [
        Facts("s.view1", view_titles=("ELEVATION",)),
        Facts("s.view2", view_titles=("ELEVATION",)),
    ]
    assert plan_and_elevation(vendor, two) == ()
    three = [*views, Facts("s.view3", view_titles=("SECTION",))]
    assert plan_and_elevation(vendor, three) == ()
    named = [
        Facts("s.view1", view_titles=("PANTRY ELEVATION",)),
        Facts("s.view2", view_titles=("PLAN",)),
    ]
    assert plan_and_elevation(vendor, named) == ()
    assert plan_and_elevation(vendor, [views[0]]) == ()
    # An accessible variant on one view: a marker still tells them apart (ruled out).
    marked = Facts("v", index_title="Sample Kitchenette", markers=frozenset({"ADA"}))
    variants = [views[0], Facts("s.view2", view_titles=("PLAN",), markers=frozenset({"ADA"}))]
    assert plan_and_elevation(marked, variants) == ()


# ---- the mirror and the swap audit -----------------------------------------------------------------


def _rows(*widths: int) -> tuple[tuple[str, tuple[Fraction, ...]], ...]:
    return (("h", tuple(F(w) for w in widths)),)


def test_a_mirror_is_proved_by_widths_read_backwards_never_forwards() -> None:
    up = frozenset({"elevation"})

    def drawn(ref: str, *widths: int, views: frozenset[str] = up) -> Facts:
        return Facts(ref, values=tuple(F(w) for w in widths), size_rows=_rows(*widths), views=views)

    vendor = drawn("v", 18, 30, 36)
    proof = mirror_proof(vendor, [drawn("a", 36, 30, 18)])
    assert proof is not None and proof.parts == (F(18), F(30), F(36))
    # The same order: no mirror.
    assert mirror_proof(vendor, [drawn("a", 18, 30, 36)]) is None
    # A run that reads the same both ways proves nothing.
    assert mirror_proof(drawn("v", 30, 18, 30), [drawn("a", 30, 18, 30)]) is None
    # Two parts are too few.
    assert mirror_proof(drawn("v", 18, 30), [drawn("a", 30, 18)]) is None
    # A plan may be drawn turned on its sheet: only an elevation against an elevation.
    assert mirror_proof(vendor, [drawn("a", 36, 30, 18, views=frozenset({"plan"}))]) is None
    # A proof on one of several drawings says nothing of the others.
    assert mirror_proof(vendor, [drawn("a", 36, 30, 18), drawn("b", 60, 24)]) is None
    # Left and right swap when ends are compared.
    assert mirror_sides("left wall") == "right wall" and mirror_sides("Right end") == "left end"
    assert ends_match(mirror_sides("left wall"), "sink centre", "right wall", "sink centre")[0] == (
        "same"
    )


def test_the_swap_audit_flags_two_pairs_whose_sizes_fit_crossed() -> None:
    one = Facts("v1", values=(F(72), F(25)), size_rows=())
    two = Facts("v2", values=(F(60), F(25)), size_rows=())
    page_72 = Facts("a72", values=(F(72), F(25)))
    page_60 = Facts("a60", values=(F(60), F(25)))
    assert swap_audit((one, [page_72]), (two, [page_60])) is None
    assert swap_audit((one, [page_60]), (two, [page_72])) == (8, 4)


# ---- the whole-drawing look's money ---------------------------------------------------------------


class _Labels:
    def __init__(self, sides: dict[int, list[str]]) -> None:
        self.sides = sides

    def on_page(self, file: str, page: int, read_now: bool = False) -> list[dict] | None:
        if page == 404:
            return None
        return [{"side": side, "box": [1, 1, 2, 2]} for side in self.sides.get(page, [])]


def test_the_whole_look_runs_only_where_code_read_a_size_on_both_sides() -> None:
    look = whole_look.Look(
        1,
        "piece",
        (
            whole_look.Drawing(ARCH, 9, (0, 0, 100, 100), "architect", pasted=False),
            whole_look.Drawing(SHOP, 1, (0, 0, 100, 100), "vendor", pasted=False),
        ),
    )
    assert whole_look.comparable(_Labels({9: ["architect"], 1: ["vendor"]}), look)
    assert not whole_look.comparable(_Labels({1: ["vendor"]}), look)
    assert not whole_look.comparable(_Labels({9: ["architect"]}), look)
    # Not known (no label list, or a page not read yet): the look runs.
    assert whole_look.comparable(None, look)
    unread = whole_look.Look(
        2,
        "piece",
        (
            whole_look.Drawing(ARCH, 404, (0, 0, 100, 100), "architect", pasted=False),
            whole_look.Drawing(SHOP, 1, (0, 0, 100, 100), "vendor", pasted=False),
        ),
    )
    assert whole_look.comparable(_Labels({1: ["vendor"]}), unread)


class _NoFolder:
    """A pack folder with no saved matcher answers."""

    def __truediv__(self, name: str) -> Any:
        from pathlib import Path

        return Path("/nonexistent-pack") / name
