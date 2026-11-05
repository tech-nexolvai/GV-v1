"""Pairing per drawing, not per sheet (rebuild plan Step 3, 2026-11-05).

An architect sheet often holds two room types (a standard and an accessible kitchen, or a studio's
and a one-bedroom's), so pairing a vendor drawing with the whole sheet blurred them. These tests
hold the pieces the per-drawing pairing rests on. Invented sets and made-up names only: $0, no
model call.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Any

from app.review.assistant import page_match, set_pack
from app.review.assistant.simple_check import pairing, signature, views, worklist
from app.review.assistant.simple_check.schema import AcrossCandidate, AcrossPair, Drawing

F = Fraction

# ---- the words code reads ------------------------------------------------------------------------


def test_a_sheet_number_with_a_letter_after_it_is_a_sheet_number() -> None:
    assert pairing.references_in(["SEE SHEET Q-401A"]) == {"Q401A"}
    assert pairing.references_in(["2/Q-401A"]) == {"2/Q401A", "Q401A"}
    assert pairing.references_in(["Q7.10B"]) == {"Q7.10B"}
    assert set_pack._sheet_numbers("SHEET NO. A-401A") == ["A-401A"]
    assert set_pack._sheet_numbers("A-401A") == ["A-401A"]


def test_a_keynote_code_is_not_a_sheet_number() -> None:
    assert set_pack._sheet_numbers("KEYNOTE D-388 SEE M500") == []
    assert set_pack._sheet_numbers("A-501 and MW-101") == ["A-501", "MW-101"]
    # A pack made before the page map left them out: pairing drops them when it reads the pack.
    assert views.keynote_code("D-388") and views.keynote_code("M500")
    assert not views.keynote_code("A-501") and not views.keynote_code("MW-101")


def test_acc_and_a_misspelt_accessible_mark_the_accessible_drawing() -> None:
    assert pairing.markers_in(["ACC KITCHEN PLAN"])[0] == {"ACC"}
    assert pairing.markers_in(["ACC. KITCHEN ELEVATION"])[0] == {"ACC"}
    assert pairing.markers_in(["ACCESIBLE VANITY ELEVATION"])[0] == {"ACCESIBLE"}
    # An item's accessories after its tag, and other words starting ACC, are no marker.
    assert pairing.markers_in(["XX-213-ACC", "ACCESSORIES", "ACC-1"])[0] == frozenset()


# ---- the page signature ----------------------------------------------------------------------------


def test_room_words_and_unit_types_are_read_from_titles_only() -> None:
    assert signature.room_words(["STANDARD KITCHENETTE TYPE-2"]) == {"KITCHEN"}
    assert signature.room_words(["ACME BRAND WORDS"]) == frozenset()
    assert signature.unit_type(["1 BDRM KITCHEN PLAN"]) == "1-BEDROOM"
    assert signature.unit_type(["ONE BEDROOM ACC KITCHEN"]) == "1-BEDROOM"
    assert signature.unit_type(["KITCHENETTE K1BR"]) == "1-BEDROOM"
    assert signature.unit_type(["STUDIO KITCHEN"]) == "STUDIO"
    assert signature.unit_type(["KITCHEN PLAN"]) is None
    # Two unit types (a schedule) name none.
    assert signature.unit_type(["STUDIO AND 2 BEDROOM"]) is None


def test_a_vendors_own_type_code_takes_the_unit_type_its_other_titles_give() -> None:
    titles = {
        "a": "Type-7(Right) Sample Kitchenette 2BR",
        "b": "Sample Kitchenette Type-7-Left",
        "c": "XY-123-Z Type-7(left)",
        "d": "Sample Vanity Type-7",
        "e": "Sample Kitchenette Type-9",
    }
    # The kitchenette's code 7 is printed beside "2BR"; a vanity of code 7 is another room, and
    # a code never printed beside a unit type gives none.
    assert signature.unit_aliases(titles) == {"b": "2-BEDROOM", "c": "2-BEDROOM"}
    # A code printed with a look-alike dash (a minus sign) is the same code.
    assert signature.unit_aliases({"a": "Sample Kitchenette Type\u22127 2BR", "b": "Type-7"}) == {
        "b": "2-BEDROOM"
    }


def test_fixtures_are_read_left_to_right_and_notes_are_not_fixtures() -> None:
    order = signature.fixture_order(
        [
            ("SINK", [500, 0, 520, 10]),
            ("COOKTOP", [100, 0, 140, 10]),
            ("REFRIGERATOR", [800, 0, 860, 10]),
            ("WHEN THE SINK IS LEFT OF THE COOKTOP IT IS A LEFT KITCHEN", [0, 50, 400, 60]),
            ("REF. ELEV 2/Q-501", [900, 0, 960, 10]),
        ]
    )
    assert order == ("RANGE", "SINK", "REF")
    assert signature.order_match(order, ("RANGE", "SINK", "REF")) == (3, 1)
    assert signature.order_match(order, ("REF", "SINK", "RANGE")) == (1, 3)
    # Fuzzy: a fixture one drawing leaves out is skipped.
    assert signature.order_match(order, ("RANGE", "DW", "REF")) == (2, 1)


def _sig(ref: str, **fields: Any) -> signature.Signature:
    return signature.Signature(ref, **fields)


def test_another_unit_type_or_accessible_marker_rules_a_drawing_out() -> None:
    vendor = _sig("v", rooms=frozenset({"KITCHEN"}), unit="1-BEDROOM")
    same = signature.match(vendor, _sig("a", rooms=frozenset({"KITCHEN"}), unit="1-BEDROOM"), {}, 0)
    assert (
        same.contradicts is None
        and same.bonus > 0
        and "both draw a 1 bedroom unit" in same.evidence
    )
    other = signature.match(vendor, _sig("b", rooms=frozenset({"KITCHEN"}), unit="STUDIO"), {}, 0)
    assert other.contradicts is not None and "another unit type" in other.contradicts
    marked = signature.match(
        vendor, _sig("c", rooms=frozenset({"KITCHEN"}), accessible=True), {}, 0
    )
    assert marked.contradicts == "this drawing is marked accessible and the vendor's is not"
    # A unit type on one side only lowers the rank, never rules out.
    plain = signature.match(vendor, _sig("d", rooms=frozenset({"KITCHEN"})), {}, 0)
    assert plain.contradicts is None and plain.bonus < same.bonus


def test_mirrored_fixture_order_matches_with_a_hand_note() -> None:
    vendor = _sig("v", order=("SINK", "RANGE", "REF"))
    mirrored = signature.match(vendor, _sig("a", order=("REF", "RANGE", "SINK")), {}, 0)
    assert mirrored.mirrored and mirrored.bonus == signature.W_ORDER
    assert any("reverse order" in e and "opposite-hand" in e for e in mirrored.evidence)
    forward = signature.match(vendor, _sig("b", order=("SINK", "RANGE", "REF")), {}, 0)
    assert not forward.mirrored and forward.bonus == signature.W_ORDER
    signatures = {"v": vendor, "a": _sig("a", order=("REF", "RANGE", "SINK"))}
    note = worklist._hand_by_fixtures("v", ["a"], signatures)
    assert note is not None and "reverse order" in note and "opposite-hand" in note
    signatures["b"] = _sig("b", order=("SINK", "RANGE", "REF"))
    assert worklist._hand_by_fixtures("v", ["b"], signatures) is None


def test_sizes_alone_never_decide_a_pair() -> None:
    # Three look-alike drawings with no code evidence: one shares the vendor's overall widths.
    vendor = _sig("v", widths=(F(96), F(60), F(30)))
    ranked = tuple(pairing.Scored(ref, 0.0, ()) for ref in ("a", "b", "c", "d"))
    targets = {
        "a": _sig("a", widths=(F(96), F(60), F(30))),
        "b": _sig("b", widths=(F(120),)),
        "c": _sig("c"),
        "d": _sig("d"),
    }
    reranked = signature.rerank(ranked, vendor, targets)
    assert reranked[0].ref == "a" and 0 < reranked[0].signed <= signature.MAX_WIDTHS
    assert signature.MAX_WIDTHS < pairing.WEAK_LEAD
    # The width witness never makes code's own pick, nor the agreement rule's lead: a sure matcher
    # pick of the size-matching drawing is still not paired.
    assert pairing.code_pass(reranked) is None and pairing.clear_winner(reranked) is None
    chosen, why = pairing.agree(reranked, None, pairing.MatchAnswer(("a",), True))
    assert chosen == () and why.startswith(pairing.UNPAIRED)


def test_the_matcher_sees_at_most_five_drawings_and_two_of_one_sheet() -> None:
    ranked = [
        pairing.Scored(f"p{page}.a1.view{n}", 10.0 - page - n / 10, ())
        for page in (1, 2, 3, 4)
        for n in (1, 2, 3)
    ]
    page_of = {s.ref: s.ref.split(".")[0] for s in ranked}
    ranked.append(pairing.Scored("p9.a1.view1", 99.0, (), contradicts="another unit type"))
    shown = worklist._matcher_targets(ranked, page_of)
    assert len(shown) == worklist.MATCH_TARGETS
    assert shown == ("p1.a1.view1", "p1.a1.view2", "p2.a1.view1", "p2.a1.view2", "p3.a1.view1")
    assert "p9.a1.view1" not in shown  # ruled out: never shown


# ---- the work list: per-drawing targets ---------------------------------------------------------

SHOP, ARCH = "shop.pdf", "arch.pdf"
PAGE = [0.0, 0.0, 1200.0, 800.0]


def _entry(file: str, page: int, role: str) -> dict[str, Any]:
    return {
        "file": file,
        "page": page,
        "role": role,
        "halves": [{"box": PAGE, "role": role, "from_upload": True}],
    }


def _label(n: int, inches: int, box: list[float], side: str, axis: str = "h") -> dict[str, Any]:
    return {
        "id": f"D{n}",
        "text": f'{inches}"',
        "inches": str(inches),
        "box": box,
        "side": side,
        "orientation": axis,
        "route": "text",
    }


def _phrase(text: str, box: list[float]) -> dict[str, Any]:
    return {"text": text, "box": box}


def _view(number: str, title: str, x: float) -> list[dict[str, Any]]:
    """A titled view the architect's way: its number bubble, its title, the sheet under the
    bubble (`views.find_views`)."""
    return [
        _phrase(number, [x, 380, x + 10, 395]),
        _phrase(title, [x + 30, 380, x + 300, 395]),
        _phrase("Q5.0", [x - 5, 398, x + 15, 406]),
    ]


def _read(file: str, page: int, item: str = "", **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "file": file,
        "page": page,
        "projects": [],
        "set_kind": "unknown",
        "set_words": [],
        "introduces": False,
        "sheet": "",
        "item": item,
        "item_tags": [],
        "variant_words": [],
        "hand_words": [],
        "views": [],
        "dropped": [],
    }
    return {**base, **fields}


def _set(
    vendor_title: str,
    *,
    vendor_fixtures: tuple[tuple[str, float], ...] = (),
    standard_fixtures: tuple[tuple[str, float], ...] = (),
) -> dict[str, Any]:
    """An invented two-file set: vendor page 1 is one kitchenette (`vendor_title`); architect
    page 1 holds two views of two room types, "KITCHEN PLAN" and "ACC KITCHEN PLAN"; architect
    page 2 is a vanity sheet."""
    vendor_labels = [
        _label(1, 96, [300, 200, 320, 210], "vendor"),
        _label(2, 25, [130, 300, 140, 320], "vendor", "v"),
    ]
    sheet_labels = [
        _label(1, 96, [200, 200, 220, 210], "architect"),
        _label(2, 25, [150, 300, 160, 320], "architect", "v"),
        _label(3, 102, [800, 200, 820, 210], "architect"),
        _label(4, 25, [750, 300, 760, 320], "architect", "v"),
    ]
    vanity_labels = [_label(1, 48, [300, 200, 320, 210], "architect")]
    dims = {
        "pages": [
            {
                "file": SHOP,
                "page": 1,
                "labels": vendor_labels,
                "phrases": [
                    _phrase(vendor_title, [700, 700, 900, 710]),
                    *(_phrase(t, [x, 250, x + 40, 260]) for t, x in vendor_fixtures),
                ],
            },
            {
                "file": ARCH,
                "page": 1,
                "labels": sheet_labels,
                "phrases": [
                    *_view("1", "KITCHEN PLAN", 110),
                    *_view("2", "ACC KITCHEN PLAN", 710),
                    *(_phrase(t, [x, 250, x + 40, 260]) for t, x in standard_fixtures),
                ],
            },
            {
                "file": ARCH,
                "page": 2,
                "labels": vanity_labels,
                "phrases": [_phrase("SAMPLE VANITY ELEVATION", [700, 700, 900, 710])],
            },
        ]
    }
    index = {
        "version": 1,
        "pages": {
            f"{SHOP}#1": _read(SHOP, 1, vendor_title),
            f"{ARCH}#1": _read(ARCH, 1),
            f"{ARCH}#2": _read(ARCH, 2, "SAMPLE VANITY"),
        },
    }
    page_map = {
        "files": [SHOP, ARCH],
        "upload_roles": {SHOP: "vendor", ARCH: "architect"},
        "pages": [
            _entry(SHOP, 1, "vendor"),
            _entry(ARCH, 1, "architect"),
            _entry(ARCH, 2, "architect"),
        ],
    }
    return {"page_map": page_map, "dims": dims, "index": index}


def _work(
    found: dict[str, Any], matches: Any = None, *, per_view: bool = True, flag: bool = False
) -> Any:
    return worklist.worklist_from(
        found["page_map"],
        dims=found["dims"],
        set_index=found["index"],
        matches=matches,
        main_file=SHOP,
        per_view=per_view,
        flag=flag,
    )


def _pair(work: Any) -> Any:
    return next(p for p in work.across if p.vendor.own_page == 1)


STANDARD, ACCESSIBLE = "p1001.a1.view1", "p1001.a1.view2"


def test_two_room_types_on_one_sheet_are_separate_targets() -> None:
    pair = _pair(_work(_set("SAMPLE KITCHENETTE")))
    targets = {t.id: t for t in pair.targets}
    # The sheet's two views are two drawings, each linked to its page, with what code reads on it.
    assert STANDARD in targets and targets[STANDARD].page_id == "p1001"
    assert targets[STANDARD].signature == ("kitchen",)
    # Its views are chosen with the page: the standard kitchen's, never the accessible one.
    assert dict(pair.page_views)["p1001"] == (STANDARD,)
    assert pair.asked == tuple(t.id for t in pair.targets)


def test_an_accessible_view_is_ruled_out_for_a_standard_vendor_drawing_and_back() -> None:
    standard = _work(_set("SAMPLE KITCHENETTE"))
    assert ACCESSIBLE not in _pair(standard).asked
    accessible = _pair(_work(_set("ADA SAMPLE KITCHENETTE")))
    assert STANDARD not in accessible.asked and ACCESSIBLE in accessible.asked
    assert dict(accessible.page_views)["p1001"] == (ACCESSIBLE,)


def test_whole_sheets_remain_behind_the_setting(monkeypatch: Any) -> None:
    page = _pair(_work(_set("SAMPLE KITCHENETTE"), per_view=False))
    assert page.targets == () and page.page_views == () and "p1001" in page.asked
    monkeypatch.setenv(worklist.PAIRING_TARGETS_ENV, "page")
    assert not worklist.per_view_pairing()
    monkeypatch.setenv(worklist.PAIRING_TARGETS_ENV, "view")
    assert worklist.per_view_pairing()
    monkeypatch.delenv(worklist.PAIRING_TARGETS_ENV)
    assert worklist.per_view_pairing()


def _page_answer(work: Any, pick: str, sure: bool = False) -> dict[str, Any]:
    """A saved answer to the whole-page question (made before per-drawing pairing)."""
    pair = _pair(work)
    shown = [c.id for c in pair.candidates]
    return {
        "version": 1,
        "answers": {
            pair.vendor.ref: {
                "candidates": shown,
                "matches": [pick],
                "confident": sure,
                "reason": "the same kitchen",
                "labels": {f"A{n + 1}": ref for n, ref in enumerate(shown)},
                "prompt_version": 1,
            }
        },
    }


def test_a_best_guess_is_narrowed_to_its_views_with_the_hand_checked() -> None:
    found = _set(
        "SAMPLE KITCHENETTE",
        vendor_fixtures=(("SINK", 100), ("COOKTOP", 400)),
        standard_fixtures=(("COOKTOP", 150), ("SINK", 280)),
    )
    unasked = _work(found, flag=True)
    pair = _pair(_work(found, _page_answer(unasked, "p1001"), flag=True))
    # The matcher's page is a best guess (code cannot confirm it), compared with the view of the
    # vendor's room type only, never the whole sheet.
    assert pair.guess and pair.chosen == ("p1001",) and pair.views == (STANDARD,)
    assert 'compared with its view "KITCHEN PLAN"' in pair.why
    # The fixtures read backwards on that view: compared opposite-hand.
    assert pair.opposite_hand is not None and "reverse order" in pair.opposite_hand
    # In whole-sheet mode the guess is the whole sheet, as before.
    whole = _pair(_work(found, _page_answer(unasked, "p1001"), per_view=False, flag=True))
    assert whole.guess and whole.views == ()


def test_an_answer_on_drawings_picks_the_view() -> None:
    found = _set("SAMPLE KITCHENETTE")
    pair = _pair(_work(found))
    answers = {
        "version": 1,
        "answers": {
            pair.vendor.ref: {
                "candidates": sorted(pair.asked),
                "matches": [STANDARD],
                "confident": True,
                "reason": "the same kitchen",
                "labels": {},
                "prompt_version": 2,
                "question": worklist.ON_DRAWINGS,
            }
        },
    }
    flagged = _pair(_work(found, answers, flag=True))
    assert flagged.answer == ("p1001",) and flagged.chosen == ("p1001",)
    assert flagged.views == (STANDARD,)


# ---- the AI page matcher on drawings (built and tested with fakes; its accuracy is unmeasured) ---


class _Pictures:
    def __init__(self) -> None:
        self.drawn: list[tuple[str, ...]] = []

    def picture(self, drawings: Any, tokens: int) -> tuple[bytes, int, int]:
        self.drawn.append(tuple(d.ref for d in drawings))
        return b"\x89PNG fake", 280, 280


class _Asker:
    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.calls: list[tuple[list[dict[str, Any]], Any]] = []

    def __call__(self, messages: list[dict[str, Any]], schema: Any) -> page_match.AskReply:
        self.calls.append((messages, schema))
        return page_match.AskReply(self.texts.pop(0), "end_turn", {}, "fake-model", 0.01)


CITING = (
    '{"matches": ["A1"], "confidence": "high", "reason": "the same kitchen run", '
    '"matched_on": {"room_or_tag": "KITCHEN PLAN", "overall_width": "96 in"}}'
)


def test_the_matcher_is_shown_single_view_crops_with_their_text_and_must_cite() -> None:
    pair = _pair(_work(_set("SAMPLE KITCHENETTE")))
    assert len(pair.asked) <= worklist.MATCH_TARGETS
    pictures = _Pictures()
    ask = _Asker('{"matches": ["A1"], "confidence": "high", "reason": "same"}', CITING)
    entry, why, _cost = page_match.ask_one(pair, ask, pictures)
    assert why == "" and entry is not None
    # Each picture is one drawing: a view's own crop, never its whole sheet (a page with no
    # titled views is its own drawing).
    targets = {t.id: t for t in pair.targets}
    assert pictures.drawn[:-1] == [
        tuple(d.ref for d in targets[ref].drawings) for ref in sorted(pair.asked)
    ]
    assert pictures.drawn[0] == (STANDARD,)
    messages, schema = ask.calls[0]
    texts = [b["text"] for b in messages[0]["content"] if b["type"] == "text"]
    caption = next(t for t in texts if t.startswith("Architect drawing A1"))
    assert 'its view titled "KITCHEN PLAN"' in caption and "code reads on it: kitchen" in caption
    assert "matched_on" in texts[-1] and schema["required"][-1] == "matched_on"
    # The first answer left out what it matched on: one repair turn, then the citing answer.
    assert entry["repaired"] and entry["matched_on"] == {
        "room_or_tag": "KITCHEN PLAN",
        "overall_width": "96 in",
    }
    assert entry["question"] == worklist.ON_DRAWINGS
    assert entry["prompt_version"] == page_match.DRAWINGS_PROMPT_VERSION
    assert entry["matches"] == [min(pair.asked)]


def test_an_answer_that_never_cites_is_refused_and_a_whole_page_answer_is_asked_again(
    tmp_path: Path,
) -> None:
    pair = _pair(_work(_set("SAMPLE KITCHENETTE")))
    plain = '{"matches": ["A1"], "confidence": "high", "reason": "same"}'
    entry, why, _cost = page_match.ask_one(pair, _Asker(plain, plain), _Pictures())
    assert entry is None and "after one repair turn" in why and "matched_on" in why
    refused = '{"matches": [], "confidence": "low", "reason": "none"}'
    assert page_match.parse(refused, ["A1"], cite=True)[0] is None
    # An answer to the whole-page question is not this question's: asked again.
    old = {
        "candidates": sorted(pair.asked),
        "matches": [],
        "confident": False,
        "prompt_version": page_match.PROMPT_VERSION,
    }
    assert not page_match._still_good(old, pair)
    new = {**old, "prompt_version": page_match.DRAWINGS_PROMPT_VERSION, "question": "drawings"}
    assert page_match._still_good(new, pair)


def _drawn(ref: str, file: str, role: str) -> Drawing:
    box = (0.0, 0.0, 600.0, 400.0)
    return Drawing(ref=ref, file=file, page=1, half=0, role=role, box=box, crop_box=box)


def test_two_crossed_best_guesses_swap_and_stay_guesses() -> None:
    """The swap audit on guesses: two look-alike vendor drawings whose sizes fit each other's
    guessed drawing better, on both sides, swap their guesses; both stay best guesses."""
    items = {
        page: AcrossCandidate(
            id=page,
            file=ARCH,
            page=n,
            drawings=(_drawn(f"{page}.a1", ARCH, "architect"),),
        )
        for n, page in ((1, "p1001"), (2, "p1002"))
    }
    sizes = {
        "p1.v0": (96, 30),
        "p2.v0": (60, 24),
        "p1001.a1": (60, 24),
        "p1002.a1": (96, 30),
    }

    def facts_of(drawing: Drawing) -> pairing.Facts:
        return pairing.Facts(drawing.ref, tuple(F(v) for v in sizes[drawing.ref]))

    pairs = {
        f"p{n}.v0": AcrossPair(
            vendor=_drawn(f"p{n}.v0", SHOP, "vendor"),
            candidates=tuple(items.values()),
            chosen=(page,),
            unsure=(page,),
            guess=True,
            why=f"best guess: architect page {n} (not confirmed): the matcher's pick",
        )
        for n, page in ((1, "p1001"), (2, "p1002"))
    }
    context = worklist._Across()
    look_alike = signature.Signature("x", rooms=frozenset({"KITCHEN"}))
    context.signatures = {"p1.v0": look_alike, "p2.v0": look_alike}
    worklist._swap_guesses(pairs, facts_of, items, context)
    assert pairs["p1.v0"].chosen == ("p1002",) and pairs["p2.v0"].chosen == ("p1001",)
    assert all(p.guess for p in pairs.values())
    assert pairs["p1.v0"].why.startswith("best guess: architect page 2 (not confirmed): the swap")
    # Now each fits its own guess better: the audit again changes nothing.
    again = dict(pairs)
    worklist._swap_guesses(again, facts_of, items, context)
    assert again == pairs
    # Drawings of unlike signatures (another room) are never swapped.
    context.signatures["p2.v0"] = signature.Signature("y", rooms=frozenset({"VANITY"}))
    kept = dict(pairs)
    worklist._swap_guesses(kept, facts_of, items, context)
    assert kept == pairs
