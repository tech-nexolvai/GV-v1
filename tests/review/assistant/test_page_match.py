"""Cross-file page matching (#1798): code's score, the AI page matcher, the agreement rule, the
wiring into both full-check modes. Synthetic facts and PDFs only, $0, no model call (the matcher's
calls go to a fake asker).

A wrong pair can make false reds, so the rule is strict: a pair is made only when the matcher's
pick is code's top candidate (or one of code's top candidates and the matcher is sure), and code
neither contradicts it nor clearly prefers another.
"""

from __future__ import annotations

import io
import json
import threading
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader, PdfWriter

from app.review.assistant import page_match, set_pack
from app.review.assistant.full_check_plan import CrossPairs, plan_jobs, with_lenses
from app.review.assistant.simple_check.pairing import (
    UNPAIRED,
    Facts,
    MatchAnswer,
    Scored,
    agree,
    clear_winner,
    rank,
    section_key,
    tags_in,
    title_key,
    words_in,
)
from app.review.assistant.simple_check.schema import AcrossCandidate, AcrossPair, Drawing
from app.review.assistant.simple_check.worklist import MATCH_FILE, build_worklist
from tests.review.assistant.separate_files import Sheet, cover, plain_pdf
from tests.review.assistant.test_set_pack import _annotation
from tests.review.assistant.test_set_pack_pages import _listing, _pack, _read
from tests.review.assistant.test_set_pack_roles import ARCHITECT_KIND, SHOP_KIND

F = Fraction

# ---- normalising what is printed and typed ------------------------------------------------------


def test_titles_tags_and_words_are_normalised() -> None:
    assert title_key("4.Wardrobe") == "wardrobe"
    assert title_key("1. Wet Bar @Guest Lounge") == "wet bar at guest lounge"
    assert title_key("Library Shelf Wall w/ Pegs") == ("library shelf wall with pegs")
    assert section_key("7", "2. TALL UNITS -MILLWORK") == "7 / tall units millwork"
    assert section_key("7", None) is None
    # Item and finish tags; a view marker, a count and a size are not tags.
    assert tags_in(["EQ-512_ALT-X", "PL-07", "LM-007.C", "ID 9.4C", "X2", 'W17-5/16"']) == {
        "EQ512",
        "PL7",
        "LM7",
    }
    # Naming words: drawing words left out, a plural's S dropped.
    assert words_in(["WARDROBE ELEVATION", "Shoe Rack @ Dressing", "FRONT VIEW"]) == {
        "WARDROBE",
        "SHOE",
        "RACK",
        "DRESSING",
    }


# ---- code's score ------------------------------------------------------------------------------


def _facts(ref: str, **fields: Any) -> Facts:
    return Facts(ref, **fields)


def test_a_title_printed_on_the_vendors_sheet_is_strong_and_code_pairs_alone() -> None:
    vendor = _facts("p5.v0", text=title_key("PROJECT A Cabinets @ copy room CP-410.C"))
    items = [
        _facts("p1005", title=title_key("3. Cabinets @ copy room")),
        _facts("p1006", title=title_key("4. Cabinets @ Mail Room")),
    ]
    ranked = rank(vendor, items)
    assert ranked[0].ref == "p1005" and ranked[0].strong
    assert "is printed on the vendor's sheet" in ranked[0].evidence[0]
    found = clear_winner(ranked)
    assert found is not None and found.ref == "p1005"


def test_another_section_contradicts_and_the_only_item_of_a_section_is_strong() -> None:
    here = section_key("PROJECT A", "3. L-SHAPED RUNS")
    there = section_key("PROJECT A", "1. STRAIGHT RUNS")
    # The vendor's title block repeats another item's title (a copy-paste): the section wins.
    vendor = _facts("p15.v0", section=here, text=title_key("Cabinets @ copy room"))
    items = [
        _facts("p1005", section=there, title=title_key("3. Cabinets @ copy room")),
        _facts("p1015", section=here, title=title_key("4.Wardrobe")),
    ]
    ranked = rank(vendor, items)
    by = {s.ref: s for s in ranked}
    assert by["p1005"].contradicts is not None and "another section" in by["p1005"].contradicts
    assert by["p1015"].strong and "the only architect item in that section" in by["p1015"].evidence
    assert ranked[0].ref == "p1015"
    assert clear_winner(ranked) is not None and clear_winner(ranked).ref == "p1015"  # type: ignore[union-attr]


def test_finish_tags_and_naming_words_rank_but_never_pair_alone() -> None:
    vendor = _facts("p5.v0", tags=frozenset({"PL7", "SS2", "MX5"}), words=frozenset({"MAPLE"}))
    items = [
        _facts("p1005"),
        _facts("p1006"),
        _facts("p1007", tags=frozenset({"PL7", "SS2", "MX5"}), words=frozenset({"MAPLE"})),
    ]
    ranked = rank(vendor, items)
    assert ranked[0].ref == "p1007" and not ranked[0].strong
    assert clear_winner(ranked) is None
    # An item tag (a number of 100 or more) printed on both is strong.
    tagged = rank(
        _facts("p3.v0", tags=frozenset({"EQ512", "EQ318"})),
        [
            _facts("p1005", tags=frozenset({"EQ512", "EQ318"})),
            *(_facts(f"p100{n}") for n in (6, 7, 8, 9)),
        ],
    )
    assert tagged[0].ref == "p1005" and tagged[0].strong
    assert clear_winner(tagged) is not None


def test_a_bare_sheet_or_tag_number_is_no_reference() -> None:
    """`EQ-310` printed on the vendor's sheet and on one of two plans naming it is a product tag,
    not a reference to that plan."""
    vendor = _facts("p5.v0", references=frozenset({"EQ310"}), tags=frozenset({"EQ310"}))
    items = [
        _facts("p1005", references=frozenset({"EQ310"}), tags=frozenset({"EQ310"})),
        _facts("p1007", tags=frozenset({"EQ310"})),
        _facts("p1009"),
    ]
    ranked = rank(vendor, items)
    assert ranked[0].score == ranked[1].score  # the two plans naming the tag tie
    assert clear_winner(ranked) is None


def test_sizes_count_within_a_tolerance_but_alone_are_not_strong() -> None:
    vendor = _facts("p1.v0", oriented=(("h", F(47, 1) + F(1, 2)), ("v", F(30))))
    items = [
        _facts("p1001", oriented=(("h", F(48)), ("v", F(29)))),
        _facts("p1002", oriented=(("h", F(96)), ("v", F(90)))),
    ]
    ranked = rank(vendor, items)
    assert ranked[0].ref == "p1001" and ranked[0].score == 2.0 and not ranked[0].strong
    assert any("overall width" in e for e in ranked[0].evidence)


# ---- the agreement rule ------------------------------------------------------------------------


def _scored(
    *pairs: tuple[str, float], strong: str | tuple[str, ...] = (), out: str | None = None
) -> tuple[Scored, ...]:
    strong_refs = (strong,) if isinstance(strong, str) else strong
    return tuple(
        Scored(
            ref,
            score,
            (f"evidence for {ref}",) if score else (),
            "another section" if ref == out else None,
            ref in strong_refs,
        )
        for ref, score in pairs
    )


def test_no_answer_pairs_only_on_codes_own_clear_pick() -> None:
    ranked = _scored(("p1005", 5.0), ("p1006", 1.0), strong="p1005")
    chosen, why = agree(ranked, "p1005", None)
    assert chosen == ("p1005",) and why.startswith("code paired it with p1005")
    chosen, why = agree(ranked, None, None)
    assert chosen == () and why.startswith(UNPAIRED)


def test_a_sure_pick_needs_strong_code_evidence_or_a_clear_lead() -> None:
    ranked = _scored(("p1005", 2.0), ("p1006", 0.5), ("p1007", 0.0), strong="p1005")
    chosen, why = agree(ranked, None, MatchAnswer(("p1005",), True, "same three-door base run"))
    assert chosen == ("p1005",) and "code holds strong evidence for it" in why
    # Not sure, and not code's own clear pick: no pair, even with strong code evidence.
    chosen, why = agree(ranked, None, MatchAnswer(("p1005",), confident=False))
    assert chosen == () and "is not sure" in why
    # Sure, strong evidence for the pick, but another item clearly ahead: no pair.
    ahead = _scored(("p1024", 4.0), ("p1026", 5.5), strong=("p1024",))
    chosen, why = agree(ahead, None, MatchAnswer(("p1024",), confident=True))
    assert chosen == () and "code ranks p1026 higher" in why
    # Weak evidence only: the pick must be code's first by `WEAK_LEAD`.
    clear = _scored(("p1001", 1.0), ("p1002", 0.25), ("p1003", 0.0))
    chosen, _why = agree(clear, None, MatchAnswer(("p1001",), confident=True))
    assert chosen == ("p1001",)


def test_a_sure_pick_among_look_alikes_code_cannot_tell_apart_is_not_paired() -> None:
    """The paid test (2026-10-20): the matcher was sure of a look-alike that code ranked first by
    one close size only, and of a run code ranked second; neither may pair."""
    narrow = _scored(("p1010", 1.0), ("p1009", 0.75), ("p1001", 0.25))
    chosen, why = agree(narrow, None, MatchAnswer(("p1010",), True, "same base run"))
    assert chosen == () and "does not put it clearly first" in why
    second = _scored(("p1026", 6.7), ("p1023", 4.7), ("p1003", 1.5))
    chosen, why = agree(second, None, MatchAnswer(("p1023",), confident=True))
    assert chosen == () and "p1026" in why
    twins = _scored(("p1011", 0.0), ("p1012", 0.0), ("p1013", 0.0))
    assert agree(twins, None, MatchAnswer(("p1012",), True, "wine racks"))[0] == ()
    # A product tag two plans print is no strong evidence for either (`rank`).
    tied = rank(
        _facts("p6.v0", tags=frozenset({"EQ310"})),
        [
            _facts("p1005", tags=frozenset({"EQ310"})),
            _facts("p1007", tags=frozenset({"EQ310"})),
            *(_facts(f"p100{n}") for n in (6, 8, 9)),
        ],
    )
    assert not any(s.strong for s in tied)
    assert agree(tied, None, MatchAnswer(("p1007",), confident=False))[0] == ()


def test_codes_own_pick_pairs_with_an_unsure_matcher() -> None:
    ranked = _scored(("p1012", 8.5), ("p1011", 2.8), strong="p1012")
    chosen, why = agree(ranked, "p1012", MatchAnswer(("p1012",), confident=False))
    assert chosen == ("p1012",) and "code's own pick" in why


def test_disagreement_and_contradiction_make_no_pair() -> None:
    ranked = _scored(("p1015", 7.5), ("p1005", 4.0), strong="p1015", out="p1005")
    # The matcher picks what code rules out.
    chosen, why = agree(ranked, "p1015", MatchAnswer(("p1005",), confident=True))
    assert chosen == () and "another section" in why
    # The matcher picks another item than code's clear pick.
    ranked2 = _scored(("p1003", 8.0), ("p1004", 3.0), strong="p1003")
    chosen, why = agree(ranked2, "p1003", MatchAnswer(("p1004",), confident=True))
    assert chosen == () and "so they disagree" in why
    # The matcher finds none while code chose one.
    chosen, why = agree(
        ranked2, "p1003", MatchAnswer((), confident=True, reason="a different unit")
    )
    assert chosen == () and "found no architect drawing of the same item" in why


def test_a_second_pick_is_kept_only_with_strong_code_evidence() -> None:
    ranked = _scored(("p1001", 0.5), ("p1002", 0.0), ("p1006", 3.0), strong="p1006")
    chosen, _why = agree(ranked, None, MatchAnswer(("p1006", "p1001"), confident=True))
    assert chosen == ("p1006",)
    both = _scored(("p1001", 2.5), ("p1002", 0.0), ("p1006", 3.0), strong=("p1006", "p1001"))
    chosen, _why = agree(both, None, MatchAnswer(("p1006", "p1001"), confident=True))
    assert chosen == ("p1006", "p1001")


# ---- the matcher's call ------------------------------------------------------------------------


def test_the_form_is_closed_and_fenced_json_is_read() -> None:
    schema = page_match.reply_schema(["A1", "A2"])
    assert schema["additionalProperties"] is False
    assert schema["properties"]["matches"]["items"]["enum"] == ["A1", "A2"]
    fenced = '```json\n{"matches": ["A2"], "confidence": "high", "reason": "same desk"}\n```'
    answer, problem = page_match.parse(fenced, ["A1", "A2"])
    assert problem is None and answer == MatchAnswer(("A2",), True, "same desk")
    assert page_match.parse('{"matches": ["A9"], "confidence": "high", "reason": ""}', ["A1"])[1]
    assert page_match.parse('{"matches": [], "confidence": "high", "reason": "", "x": 1}', ["A1"])[
        1
    ]
    assert page_match.parse("I think A1", ["A1"])[0] is None


def _drawing(ref: str, file: str, page: int, own: int | None = None) -> Drawing:
    box = (0.0, 0.0, 600.0, 400.0)
    return Drawing(
        ref=ref,
        file=file,
        page=page,
        file_page=own,
        half=0,
        role="architect" if ".a" in ref else "vendor",
        box=box,
        crop_box=box,
    )


def _pair(ref: str = "p1.v0", asked: tuple[str, ...] = ("p1001", "p1002")) -> AcrossPair:
    items = tuple(
        AcrossCandidate(
            id=item,
            file="arch.pdf",
            page=int(item[1:]) - 1000,
            drawings=(_drawing(f"{item}.a1", "arch.pdf", int(item[1:]), int(item[1:]) - 1000),),
            title=f"Item {item}",
        )
        for item in asked
    )
    return AcrossPair(
        vendor=_drawing(ref, "shop.pdf", int(ref[1 : ref.index(".")])),
        candidates=items,
        asked=asked,
        why="",
    )


class _FakePictures:
    def __init__(self) -> None:
        self.drawn = 0

    def picture(self, drawings: Any, tokens: int) -> tuple[bytes, int, int]:
        self.drawn += 1
        return b"\x89PNG fake", 280, 280


class _Asker:
    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.calls: list[list[dict[str, Any]]] = []

    def __call__(self, messages: list[dict[str, Any]], schema: Any) -> page_match.AskReply:
        self.calls.append(messages)
        return page_match.AskReply(
            self.texts.pop(0),
            "end_turn",
            {"input_tokens": 1000, "output_tokens": 40},
            "claude-sonnet-5-5",
            0.01,
        )


def test_one_repair_turn_then_ids_not_labels_are_saved() -> None:
    ask = _Asker("A2 looks right", '{"matches": ["A2"], "confidence": "high", "reason": "same"}')
    entry, why, cost = page_match.ask_one(_pair(), ask, _FakePictures())
    assert why == "" and entry is not None
    assert entry["matches"] == ["p1002"] and entry["repaired"] and entry["confident"]
    assert entry["candidates"] == ["p1001", "p1002"] and cost == pytest.approx(0.02)
    assert len(ask.calls) == 2 and ask.calls[1][1]["role"] == "assistant"
    # The architect items' pictures carry the cache breakpoint; the vendor's does not.
    content = ask.calls[0][0]["content"]
    marked = [n for n, block in enumerate(content) if "cache_control" in block]
    assert len(marked) == 1 and content[marked[0]]["type"] == "image"
    assert content[marked[0] + 1]["text"].startswith("The vendor drawing")
    still = _Asker("no", "still no")
    entry, why, _cost = page_match.ask_one(_pair(), still, _FakePictures())
    assert entry is None and "after one repair turn" in why


def test_answers_are_saved_reused_at_no_cost_and_asked_again_when_candidates_change(
    tmp_path: Path,
) -> None:
    ask = _Asker(
        '{"matches": ["A1"], "confidence": "low", "reason": "same"}',
        '{"matches": [], "confidence": "high", "reason": "none"}',
    )
    pairs = [_pair("p1.v0"), _pair("p2.v0")]
    report = page_match.match_pairs(pairs, tmp_path, ask, _FakePictures())
    assert (report.asked, report.answered, report.reused) == (2, 2, 0)
    saved = page_match.load_answers(tmp_path)
    assert saved["p1.v0"]["matches"] == ["p1001"] and saved["p2.v0"]["matches"] == []
    again = page_match.match_pairs(pairs, tmp_path, _Asker(), _FakePictures())
    assert (again.asked, again.reused, again.cost_usd) == (0, 2, 0.0)
    changed = _pair("p1.v0", ("p1001", "p1002", "p1003"))
    third = _Asker('{"matches": ["A3"], "confidence": "high", "reason": "x"}')
    page_match.match_pairs([changed], tmp_path, third, _FakePictures())
    assert len(third.calls) == 1


def test_the_money_guard_and_a_stop_ask_nothing(tmp_path: Path) -> None:
    ask = _Asker()
    report = page_match.match_pairs(
        [_pair()],
        tmp_path,
        ask,
        _FakePictures(),
        fits=lambda tokens: False,
    )
    assert report.asked == 0 and report.skipped_for_money == 1 and not ask.calls
    stop = threading.Event()
    stop.set()
    report = page_match.match_pairs([_pair()], tmp_path, ask, _FakePictures(), stop=stop)
    assert report.asked == 0 and report.failed == [("p1.v0", "the check was stopped")]
    assert not (tmp_path / MATCH_FILE).exists()


# ---- the work list reads the saved answers ($0 replay) -----------------------------------------

ARCH = [
    cover(),
    Sheet("ARCH ELEVATION", (24, 30, 18)),
    Sheet("ARCH ELEVATION", (36, 36, 12)),
]
#: The vendor's sizes differ from both (no printed value in common): code cannot pair alone.
SHOP = [Sheet("VENDOR ELEVATION", (25, 29, 17))]


def _two_files(tmp_path: Path, arch: list[Sheet] = ARCH) -> Path:
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "shop.pdf").write_bytes(plain_pdf(SHOP))
    (uploads / "arch.pdf").write_bytes(plain_pdf(arch))
    listing = _listing(uploads, ("shop.pdf", SHOP_KIND), ("arch.pdf", ARCHITECT_KIND))
    return _pack(tmp_path, uploads, listing)


def _save(folder: Path, ref: str, candidates: list[str], matches: list[str], sure: bool) -> None:
    (folder / MATCH_FILE).write_text(
        json.dumps(
            {
                "version": 1,
                "answers": {
                    ref: {
                        "candidates": candidates,
                        "matches": matches,
                        "confident": sure,
                        "reason": "same three-cabinet run",
                        "prompt_version": page_match.PROMPT_VERSION,
                    }
                },
            }
        )
    )


def test_the_work_list_pairs_where_code_and_the_saved_answer_agree(tmp_path: Path) -> None:
    folder = _two_files(tmp_path)
    work = build_worklist(folder, "shop.pdf")
    assert work.items == ()
    across = work.across[0]
    assert across.vendor.ref == "p1.v0" and set(across.asked) == {"p1002", "p1003"}
    assert across.code_pick is None and across.why.startswith(UNPAIRED)
    # The matcher's saved answer, sure, and code's clear first (on weak evidence): paired, at $0.
    _save(folder, "p1.v0", ["p1002", "p1003"], ["p1002"], sure=True)
    work = build_worklist(folder, "shop.pdf")
    assert [(i.id, [d.ref for d in i.architect]) for i in work.items] == [("p1.v0", ["p1002.a1"])]
    assert "the AI page matcher is sure" in str(work.items[0].paired_by)
    # The roster's planning gets the same pair.
    found = CrossPairs.of(work)
    assert found.pairs == {1: (("arch.pdf", 2),)} and found.unpaired == {}
    # An answer to other candidates (the set changed) is not used.
    _save(folder, "p1.v0", ["p1002"], ["p1002"], sure=True)
    assert build_worklist(folder, "shop.pdf").items == ()


def test_no_answer_among_twins_is_paired(tmp_path: Path) -> None:
    twins = [cover(), Sheet("ARCH ELEVATION", (24, 30, 18)), Sheet("ARCH ELEVATION", (24, 30, 18))]
    folder = _two_files(tmp_path, twins)
    scores = [c.score for c in build_worklist(folder, "shop.pdf").across[0].candidates]
    assert scores[0] == scores[1]  # code cannot tell the twins apart
    _save(folder, "p1.v0", ["p1002", "p1003"], ["p1003"], sure=False)
    work = build_worklist(folder, "shop.pdf")
    assert work.items == ()
    unpaired = CrossPairs.of(work).unpaired
    assert 1 in unpaired and "is not sure" in unpaired[1]
    # Sure is not enough when code cannot tell the twins apart (a wrong pair makes false reds).
    _save(folder, "p1.v0", ["p1002", "p1003"], ["p1003"], sure=True)
    work = build_worklist(folder, "shop.pdf")
    assert work.items == () and "does not put it clearly first" in CrossPairs.of(work).unpaired[1]


# ---- the roster's planning hands each helper its pairs ------------------------------------------


def _page_map() -> dict[str, Any]:
    whole = [0.0, 0.0, 600.0, 400.0]

    def entry(file: str, page: int, role: str) -> dict[str, Any]:
        return {
            "file": file,
            "page": page,
            "role": role,
            "halves": [{"box": whole, "role": role, "from_upload": True}],
            "area": None,
            "section": None,
        }

    return {
        "files": ["shop.pdf", "arch.pdf"],
        "upload_roles": {"shop.pdf": "vendor", "arch.pdf": "architect"},
        "pages": [
            *(entry("shop.pdf", n, "vendor") for n in (1, 2, 3)),
            *(entry("arch.pdf", n, "architect") for n in (1, 2)),
        ],
    }


def test_roster_jobs_carry_the_pairs_and_the_unpaired_pages() -> None:
    across = CrossPairs(
        pairs={1: (("arch.pdf", 2),), 3: (("arch.pdf", 1), ("arch.pdf", 2))},
        unpaired={2: f"{UNPAIRED}: the AI page matcher picked p1001 but is not sure"},
    )
    plan = plan_jobs(_page_map(), "shop.pdf", 1, across=across)
    assert plan is not None and len(plan.jobs) == 1
    job = plan.jobs[0]
    assert job.pairs == ((1, (("arch.pdf", 2),)), (3, (("arch.pdf", 1), ("arch.pdf", 2))))
    assert job.unpaired == ((2, across.unpaired[2]),)
    assert ("arch.pdf", 1) in job.reference and ("arch.pdf", 2) in job.reference
    lensed = with_lenses(plan)
    assert all(j.pairs == job.pairs and j.unpaired == job.unpaired for j in lensed.jobs)
    # Without pairs (a combined sheet set), nothing changes.
    plain = plan_jobs(_page_map(), "shop.pdf", 1)
    assert plain is not None and plain.jobs[0].pairs == () and plain.jobs[0].reference == ()


def test_the_helper_brief_names_the_pairs(tmp_path: Path) -> None:
    from app.review.assistant.chat_engine import ChatEngine

    across = CrossPairs(pairs={1: (("arch.pdf", 2),)}, unpaired={2: f"{UNPAIRED}: no clear one"})
    plan = plan_jobs(_page_map(), "shop.pdf", 1, across=across)
    assert plan is not None
    job = plan.jobs[0]

    class _Book:
        main_file = "shop.pdf"

    class _Engine:
        settings = type("S", (), {"products": ()})()
        _lock = threading.Lock()
        _recorded_document = None
        book = _Book()

        def _dim_labels(self, *_a: Any, **_k: Any) -> dict[int, Any]:
            return {}

    brief = ChatEngine._job_brief(_Engine(), job, "", 1, True, _page_map())  # type: ignore[arg-type]
    assert (
        "PAIRED ACROSS THE UPLOADS" in brief
        and "your p.1 with" in brief
        and "uploads/arch.pdf p.2" in brief
    )
    assert "NOT PAIRED" in brief and "p.2: not paired" in brief


# ---- a vendor's CAD page under GV's pasted snapshot and heading ---------------------------------


def test_a_vendor_cad_page_with_gvs_snapshot_and_heading_is_the_vendors_drawing(
    tmp_path: Path,
) -> None:
    """GV pastes a small reference snapshot on the vendor's own CAD page and types "VENDOR SHOP
    DRAWING" over it: the page (which prints its dimensions itself) is the vendor's drawing, the
    snapshot is GV's."""
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    shop = plain_pdf([Sheet("VENDOR ELEVATION", (24, 30, 18))])
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(shop)))
    page = writer.pages[0]
    _annotation(writer, page, "Stamp", (1000.0, 600.0, 1060.0, 660.0))
    _annotation(
        writer, page, "FreeText", (980.0, 670.0, 1180.0, 700.0), text_="VENDOR SHOP DRAWING"
    )
    out = io.BytesIO()
    writer.write(out)
    (uploads / "shop.pdf").write_bytes(out.getvalue())
    (uploads / "arch.pdf").write_bytes(plain_pdf([Sheet("ARCH ELEVATION", (24, 30, 18))]))
    listing = _listing(uploads, ("shop.pdf", SHOP_KIND), ("arch.pdf", ARCHITECT_KIND))
    page_map = _read(_pack(tmp_path, uploads, listing), "page_map.json")
    first = page_map["pages"][0]
    assert first["file"] == "shop.pdf" and len(first["halves"]) == 1
    half = first["halves"][0]
    assert half["role"] == "vendor" and half["from_upload"] is True
    assert half["box"] == [0.0, 0.0, 1224.0, 792.0]
    assert set_pack.PACK_VERSION >= 9


def test_the_engine_hands_the_roster_its_cross_file_pairs(tmp_path: Path) -> None:
    """`ChatEngine.cross_file_pairs` (both full-check modes plan with it): the pack's saved answer
    and code's score give the vendor page its architect page; the matcher itself is off here."""
    from app.review.assistant.chat_engine import ChatEngine

    folder = _two_files(tmp_path)
    _save(folder, "p1.v0", ["p1002", "p1003"], ["p1002"], sure=True)

    class _Engine:
        settings = type("S", (), {"page_match_model": None})()
        tools = type("T", (), {"workspace": type("W", (), {"set_pack": folder})()})()
        book = type("B", (), {"main_file": "shop.pdf"})()
        cancel = threading.Event()
        _cross = None

    engine = _Engine()
    page_map = _read(folder, "page_map.json")
    found = ChatEngine.cross_file_pairs(engine, page_map)  # type: ignore[arg-type]
    assert found is not None and found.pairs == {1: (("arch.pdf", 2),)}
    assert engine._cross is found
    # A combined-sheet set (no separate architect's upload): nothing to pair across.
    alone = {**page_map, "upload_roles": {"shop.pdf": "vendor"}}
    assert ChatEngine.cross_file_pairs(engine, alone) is None
