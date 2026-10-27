"""The simple full check's code verification (`simple_check/verify.py`): every rule that turns a
compared pair red, each reason that keeps one grey, and the items without a label. Made-up drawings
and values only (no client data); no PDF, no model call."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import classification, proof
from app.review.assistant.chat_trust import apply_checks
from app.review.assistant.full_check_advocate import code_checks
from app.review.assistant.simple_check import verify as v
from app.review.assistant.simple_check.verify import (
    BlindAnswer,
    Item,
    Pack,
    Region,
    end_kind,
    ends_match,
    verify,
    verify_answer,
    with_blind,
)
from app.review.assistant.vendor_text import internal

FILE = "set.pdf"
ARCH_HALF = [0.0, 0.0, 500.0, 400.0]
R1_HALF = [0.0, 420.0, 500.0, 800.0]
R2_HALF = [0.0, 0.0, 500.0, 800.0]
BLACK = (120, 0.0)


def _label(
    lid: str,
    text: str,
    inches: str,
    box: Sequence[float],
    side: str,
    orientation: str = "h",
    **more: Any,
) -> dict[str, Any]:
    return {
        "id": lid,
        "text": text,
        "inches": inches,
        "box": list(box),
        "side": side,
        "orientation": orientation,
        "route": more.pop("route", "text"),
        "mm": None,
        "units": more.pop("units", None),
        "dim_line": None,
        "extension": None,
        "span_pt": None,
        "scale_ok": None,
        **more,
    }


def _phrase(text: str, box: Sequence[float], side: str = "vendor") -> dict[str, Any]:
    return {"text": text, "box": list(box), "side": side}


def _documents() -> dict[str, Any]:
    """Page 1: the architect's kitchen elevation over the vendor's round 1; page 2: the vendor's
    round 2; page 3: the brand prototype of the same area; page 4: another area."""
    page1 = [
        _label("D1", "3' - 6\"", "42", (100, 100, 130, 110), "architect"),
        _label("D2", "2' - 9\"", "33", (20, 100, 30, 130), "architect", "v"),
        _label("D3", "1' - 0\"", "12", (200, 100, 230, 110), "architect"),
        _label("D4", "1' - 8\"", "20", (300, 100, 330, 110), "architect", "v"),
        _label("D5", '40 3/8"', "40.375", (100, 500, 130, 510), "vendor"),
        _label("D6", '34"', "34", (20, 500, 30, 530), "vendor", "v"),
        _label("D7", "305 [12]", "12", (200, 500, 230, 510), "vendor"),
        _label("D8", '120"', "120", (300, 500, 330, 510), "vendor"),
        _label("D9", '22"', "22", (400, 500, 430, 510), "vendor", units="disagrees"),
        _label("D10", '16"', "16", (300, 600, 310, 630), "vendor", "v"),
    ]
    page2 = [
        _label("D1", '45 1/4"', "45.25", (100, 100, 130, 110), "vendor"),
        _label("D2", '34"', "34", (20, 100, 30, 130), "vendor", "v"),
        _label("D3", '16"', "16", (300, 100, 310, 130), "vendor", "v"),
        _label("D4", '24"', "24", (200, 300, 230, 310), "vendor"),
        _label("D5", '18"', "18", (200, 340, 230, 350), "vendor"),
        _label("D6", '6"', "6", (200, 380, 230, 390), "vendor"),
        _label("D7", '120"', "120", (300, 500, 330, 510), "vendor"),
        _label("D8", '22"', "22", (400, 500, 430, 510), "vendor", units="disagrees"),
    ]
    page3 = [_label("D1", "3' - 8\"", "44", (100, 100, 130, 110), "brand")]
    page4 = [
        _label("D1", "3' - 0\"", "36", (100, 100, 130, 110), "architect"),
        _label("D2", '30"', "30", (100, 500, 130, 510), "vendor"),
    ]
    words = [
        _phrase("BASE CABINET", (50, 600, 120, 610)),
        _phrase("STONE TOP BY GV", (150, 600, 220, 610)),
        _phrase("FINISHED END PANEL", (250, 700, 320, 710)),
    ]
    words2 = [
        _phrase("BASE CABINET", (50, 600, 120, 610)),
        _phrase("STONE TOP", (150, 600, 220, 610)),
        _phrase("END PANEL", (250, 700, 320, 710)),
    ]
    arch_words = [_phrase("BACKSPLASH", (330, 150, 380, 160), "architect")]
    return {
        "dims": {
            "files": [FILE],
            "pages": [
                {"file": FILE, "page": 1, "labels": page1, "phrases": [*words, *arch_words]},
                {"file": FILE, "page": 2, "labels": page2, "phrases": words2},
                {"file": FILE, "page": 3, "labels": page3, "phrases": []},
                {"file": FILE, "page": 4, "labels": page4, "phrases": []},
            ],
        },
        "page_map": {
            "pages": [
                {
                    "file": FILE,
                    "page": 1,
                    "area": "1. Kitchen",
                    "area_from_page": None,
                    "halves": [
                        {"box": ARCH_HALF, "role": "architect", "round": None},
                        {"box": R1_HALF, "role": "vendor", "round": 1},
                    ],
                },
                {
                    "file": FILE,
                    "page": 2,
                    "area": "1. Kitchen",
                    "area_from_page": 1,
                    "halves": [{"box": R2_HALF, "role": "vendor", "round": 2}],
                },
                {
                    "file": FILE,
                    "page": 3,
                    "area": "1. Kitchen",
                    "area_from_page": 1,
                    "halves": [{"box": ARCH_HALF, "role": "brand_prototype", "round": None}],
                },
                {
                    "file": FILE,
                    "page": 4,
                    "area": "2. Lobby",
                    "area_from_page": None,
                    "halves": [
                        {"box": ARCH_HALF, "role": "architect", "round": None},
                        {"box": R1_HALF, "role": "vendor", "round": 1},
                    ],
                },
            ]
        },
        "pairs": {"pairs": []},
        "page_texts": {"pages": [{"file": FILE, "page": 2, "text": "BASE CABINET\nEND PANEL"}]},
    }


def _pack(**changes: Any) -> Pack:
    docs = _documents()
    docs.update(changes)
    return Pack.from_documents(main_file=FILE, **docs)


ROUND_2 = Item(
    "p2.v2",
    Region(FILE, 2, tuple(R2_HALF), 2),  # type: ignore[arg-type]
    (Region(FILE, 1, tuple(ARCH_HALF)),),  # type: ignore[arg-type]
    products=("countertop",),
)
ROUND_1 = Item(
    "p1.v1",
    Region(FILE, 1, tuple(R1_HALF), 1),  # type: ignore[arg-type]
    (Region(FILE, 1, tuple(ARCH_HALF)),),  # type: ignore[arg-type]
)


def _pair(
    vendor: str,
    architect: str | None,
    verdict: str = "different",
    ends: tuple[str, str, str, str] = ("left wall", "right wall", "left wall", "right wall"),
    measures: str = "overall counter width",
) -> dict[str, Any]:
    return {
        "vendor_label": vendor,
        "architect_label": architect,
        "measures": measures,
        "vendor_from": ends[0],
        "vendor_to": ends[1],
        "architect_from": ends[2],
        "architect_to": ends[3],
        "verdict": verdict,
        "note": "",
    }


def _one(
    pair: Mapping[str, Any],
    item: Item = ROUND_2,
    pack: Pack | None = None,
    ink: Any = lambda label: BLACK,
) -> v.Verified:
    result = {"work_item": item.id, "pairs": [pair], "other_items": [], "unaccounted_labels": []}
    found = verify_answer(result, item, pack or _pack(), ink=ink)
    assert len(found.items) == 1
    return found.items[0]


def _agrees(verified: v.Verified) -> BlindAnswer:
    assert verified.architect is not None
    ends = verified.ends
    return BlindAnswer(
        pick=f"p{verified.architect['page']}.{verified.architect['id']}",
        verdict="same_thing",
        vendor_from=ends["vendor_from"],
        vendor_to=ends["vendor_to"],
        architect_from=ends["architect_from"],
        architect_to=ends["architect_to"],
        vendor_ink_black="yes",
    )


def _stored(finding: Mapping[str, Any]) -> dict[str, Any]:
    """The finding as the finding book stores it: an id, code's checks (labels read), its critic
    and pairing kept (`chat_trust.apply_checks`)."""
    return apply_checks({"id": "F1", "created_by": v.CREATED_BY, **finding}, {}, None)


# ---- the red path -----------------------------------------------------------------------------


@pytest.mark.usefixtures("read_twice")
def test_a_verified_pair_the_blind_look_agrees_with_is_red_and_clouded() -> None:
    found = _one(_pair("p2.D1", "p1.D1"))
    assert found.outcome == "candidate" and found.needs_blind and found.verdict == "wrong"
    assert [c.name for c in found.checks if c.outcome != "pass"] == []
    red = with_blind(found, _agrees(found))
    assert red.outcome == "red"
    finding = red.finding
    assert finding["status"] == "different" and finding["source"] == "architect"
    assert finding["vendor"] == '45 1/4"' and finding["architect"] == "3' - 6\""
    assert finding["dims"]["vendor"]["id"] == "D1" and finding["dims"]["architect"]["page"] == 1
    assert finding["vs_page"] == 1 and finding["round"] == 2
    assert finding["critic"]["result"] == "passed"
    assert finding["item"].startswith("Kitchen: ")
    stored = _stored(finding)
    assert stored["witnesses"] == ["printed"]
    assert stored["critic"]["result"] == "passed"
    assert proof.proof_group(stored) == "clouded"
    assert "blind second pick" in proof.proof_line(stored)


def test_a_candidate_is_never_red_without_the_blind_look() -> None:
    found = _one(_pair("p2.D1", "p1.D1"))
    stored = _stored(found.finding)
    assert proof.proof_group(stored) == "not_compared"
    failed = with_blind(found, None, "the call failed")
    assert failed.outcome == "not_compared"
    assert "was not made (the call failed)" in failed.reason
    assert proof.proof_group(_stored(failed.finding)) == "not_compared"


@pytest.mark.parametrize(
    ("answer", "words"),
    [
        ({"pick": "p1.D3"}, "chose p1.D3, not p1.D1"),
        ({"pick": None}, "chose no architect dimension"),
        ({"verdict": "different_points"}, "measure different points"),
        ({"verdict": "unsure"}, "not sure"),
        ({"vendor_to": "underside of counter", "architect_to": "top of counter"}, "different"),
        ({"vendor_to": "ceiling"}, "room context"),
    ],
)
def test_a_blind_look_that_disagrees_keeps_the_pair_grey(
    answer: dict[str, Any], words: str
) -> None:
    found = _one(_pair("p2.D1", "p1.D1"))
    base = _agrees(found)
    fields = {**base.as_dict(), **answer}
    blind = BlindAnswer(
        pick=fields["pick"] if fields["pick"] != "none" else None,
        verdict=fields["verdict"],
        vendor_from=fields.get("vendor_from", base.vendor_from),
        vendor_to=fields.get("vendor_to", base.vendor_to),
        architect_from=fields.get("architect_from", base.architect_from),
        architect_to=fields.get("architect_to", base.architect_to),
        vendor_ink_black=fields["vendor_ink_black"],
    )
    grey = with_blind(found, blind)
    assert grey.outcome == "not_compared"
    assert words in grey.reason
    assert grey.finding["status"] == "check"
    assert proof.proof_group(_stored(grey.finding)) == "not_compared"


def test_a_bare_blind_pick_names_the_architect_page() -> None:
    found = _one(_pair("p2.D1", "p1.D1"))
    agrees = _agrees(found)
    bare = BlindAnswer(**{**_fields(agrees), "pick": "D1"})
    assert with_blind(found, bare).outcome == "red"


def _fields(answer: BlindAnswer) -> dict[str, Any]:
    return {
        name: getattr(answer, name)
        for name in (
            "pick",
            "verdict",
            "vendor_from",
            "vendor_to",
            "architect_from",
            "architect_to",
            "vendor_ink_black",
            "millwork",
        )
    }


# ---- each reason that blocks red --------------------------------------------------------------


@pytest.mark.parametrize(
    ("pair", "item", "reason", "code"),
    [
        # round 1, and round 2 prints another value
        (
            _pair("p1.D5", "p1.D1"),
            ROUND_1,
            "round 1: round 2 is the vendor's current",
            "not_proved",
        ),
        # a width against a height
        (_pair("p2.D1", "p1.D2"), ROUND_2, "a width and the other a height", "not_proved"),
        # a unit slip: 120" against 12"
        (_pair("p2.D7", "p1.D3"), ROUND_2, "10 times the other", "not_proved"),
        # millimetres and inches disagree
        (_pair("p2.D8", "p1.D1"), ROUND_2, "millimetres and inches disagree", "not_proved"),
        # an end on the ceiling (room context)
        (
            _pair("p2.D1", "p1.D1", ends=("floor", "ceiling", "floor", "ceiling")),
            ROUND_2,
            "room context",
            "not_proved",
        ),
        # what it measures is another trade's
        (
            _pair("p2.D1", "p1.D1", measures="wall covering panel width"),
            ROUND_2,
            "room context or another trade",
            "not_proved",
        ),
        # the ends name different points
        (
            _pair(
                "p2.D3",
                "p1.D4",
                ends=(
                    "top of splash",
                    "underside of upper cabinets",
                    "top of counter",
                    "underside of upper cabinets",
                ),
            ),
            ROUND_2,
            "measure different points",
            "different_reference",
        ),
        # the ends are words code cannot match
        (
            _pair("p2.D1", "p1.D1", ends=("end of trim", "wall", "left wall", "right wall")),
            ROUND_2,
            "code cannot tell",
            "not_proved",
        ),
        # the brand prototype is reference only
        (_pair("p2.D1", "p3.D1"), ROUND_2, "brand prototype", "prototype"),
    ],
)
def test_each_failed_check_keeps_a_pair_grey(
    pair: dict[str, Any], item: Item, reason: str, code: str
) -> None:
    found = _one(pair, item)
    assert found.outcome == "not_compared", found
    assert reason in found.reason
    assert found.reason_code == code
    assert found.finding["status"] == "check"
    stored = _stored(found.finding)
    assert proof.proof_group(stored) == "not_compared"
    assert proof.classified(stored).reason == code


def test_the_architect_label_must_be_on_the_items_architect_drawing() -> None:
    other_area = Item(
        "p1.v1",
        Region(FILE, 1, tuple(R1_HALF), 1),  # type: ignore[arg-type]
        (Region(FILE, 1, tuple(ARCH_HALF)),),  # type: ignore[arg-type]
    )
    found = _one(_pair("p1.D7", "p4.D1"), other_area)
    assert found.outcome == "not_compared"
    assert "page 4 is not one of this item's architect drawings" in found.reason
    without_list = Item("p1.v1", Region(FILE, 1, tuple(R1_HALF), 1))  # type: ignore[arg-type]
    found = _one(_pair("p1.D7", "p4.D1"), without_list)
    assert "other areas" in found.reason


def test_view_titles_never_block_the_ends_decide() -> None:
    """A drawing's titles do not say which view a dimension is in (a vendor drawing titled only
    "SECTION AT" holds a plan): they are information; different ends block instead."""
    plan = Item(
        "p2.v2",
        Region(FILE, 2, tuple(R2_HALF), 2, "plan"),  # type: ignore[arg-type]
        (Region(FILE, 1, tuple(ARCH_HALF), None, "elevation"),),  # type: ignore[arg-type]
    )
    found = _one(_pair("p2.D1", "p1.D1"), plan)
    view = next(c for c in found.checks if c.name == "view")
    assert found.outcome == "candidate" and view.why == "titled plan and elevation: the ends decide"
    depth = _one(
        _pair("p2.D3", "p1.D4", ends=("back wall", "front edge of top", "floor", "top of counter")),
        plan,
    )
    assert depth.outcome == "not_compared" and depth.reason_code in (
        "different_reference",
        "not_proved",
    )


def test_the_newest_round_printing_the_same_value_lets_round_1_through() -> None:
    found = _one(
        _pair("p1.D6", "p1.D2", ends=("floor", "top of counter", "floor", "top of counter")),
        ROUND_1,
    )
    assert found.outcome == "candidate"
    rounds = next(c for c in found.checks if c.name == "round")
    assert rounds.outcome == "pass" and "p2.D2" in rounds.why


def test_gv_ink_blocks_red_only_by_the_labels_own_ink() -> None:
    # A label code read from the vendor's black or grey layer is the vendor's, whatever GV drew
    # over it (a coloured note covering it looks coloured in a picture).
    covered = _one(_pair("p2.D1", "p1.D1"), ink=lambda label: (120, 1.0))
    assert covered.outcome == "candidate"
    # Any other label's own spot is looked at.
    docs = _documents()
    docs["dims"]["pages"][1]["labels"][0]["route"] = "derived"
    pack = Pack.from_documents(main_file=FILE, **docs)
    coloured = _one(_pair("p2.D1", "p1.D1"), pack=pack, ink=lambda label: (120, 0.6))
    assert coloured.outcome == "not_compared" and "GV's colour" in coloured.reason
    unknown = _one(_pair("p2.D1", "p1.D1"), pack=pack, ink=None)
    assert unknown.outcome == "not_compared" and "could not be looked at" in unknown.reason
    black = _one(_pair("p2.D1", "p1.D1"), pack=pack, ink=lambda label: BLACK)
    assert black.outcome == "candidate"


def test_a_label_inside_a_gv_note_box_is_no_longer_blocked() -> None:
    """The GV-note fix (PIPELINE-AUDIT.md: the old box check stopped four true items, no false
    one): a vendor value merely sitting inside a GV note's box is still checked; only its own ink
    decides whether it is GV's markup."""
    pack = _pack()
    label = pack.label("p2.D1", FILE, 2)
    assert label is not None
    note = {
        "page": 2,
        "file": FILE,
        "kind": "FreeText",
        "text": '45" + 6"',
        "box": [90, 95, 140, 115],
    }
    # The full check's own note check agrees now (2026-10-27): a note printing another value lies
    # on top of the vendor's own ink and does not stop it.
    full = code_checks(label, None, annotations=[note], vendor_ink=BLACK)
    assert any(c.name == "annotation" and c.outcome == "pass" for c in full)
    assert _one(_pair("p2.D1", "p1.D1"), pack=pack).outcome == "candidate"


def test_geometry_blocks_only_on_different_points(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(v, "same_extent", lambda *a: ("cannot_tell", "too few dimensions"))
    found = _one(_pair("p2.D1", "p1.D1"))
    geometry = next(c for c in found.checks if c.name == "geometry")
    assert found.outcome == "candidate" and geometry.outcome == "pass"
    assert "cannot tell" in geometry.why
    monkeypatch.setattr(v, "same_extent", lambda *a: ("different_extent", "the ends land apart"))
    found = _one(_pair("p2.D1", "p1.D1"))
    assert found.outcome == "not_compared" and found.reason_code == "different_reference"
    assert "different points" in found.reason


# ---- the model's own verdicts -----------------------------------------------------------------


def test_different_points_unsure_and_no_architect_value() -> None:
    points = _one(
        _pair(
            "p2.D1",
            "p1.D1",
            "different_points",
            ("floor", "top of counter", "floor", "underside of counter"),
        )
    )
    assert points.outcome == "not_compared" and points.reason == v.DIFFERENT_POINTS
    assert proof.classified(_stored(points.finding)).reason == "different_reference"
    unsure = _one(_pair("p2.D1", "p1.D1", "unsure"))
    assert unsure.outcome == "not_compared" and unsure.reason == v.UNSURE
    assert proof.classified(_stored(unsure.finding)).reason == "not_proved"
    nothing = _one(_pair("p2.D1", None, "no_architect_value"))
    assert nothing.outcome == "listed" and nothing.reason_code == "nothing_to_compare"
    assert nothing.finding == {} and nothing.verdict == "not_compared"


def test_a_different_points_its_own_ends_contradict_goes_to_the_blind_look() -> None:
    """The AI's "no" is checked too (DeepSeekMath-V2's lesson): "different points" with end words
    that name the same two points on both drawings is not supported, so the pair is verified like
    any other and waits for the blind look; red still needs the blind look to agree."""
    found = _one(_pair("p2.D1", "p1.D1", "different_points"))
    assert found.outcome == "candidate" and found.needs_blind and found.blind == "pair"
    objection = next(c for c in found.checks if c.name == v.OBJECTION)
    assert objection.outcome == "pass" and "its own end words are the same" in objection.why
    assert [c.name for c in found.checks if c.outcome != "pass"] == []
    assert with_blind(found, _agrees(found)).outcome == "red"
    refused = with_blind(found, None, "the call failed")
    assert refused.outcome == "not_compared"
    disagrees = replace(_agrees(found), verdict="different_points")
    assert with_blind(found, disagrees).outcome == "not_compared"


def test_a_different_points_code_geometry_never_overrules_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Code's drawn geometry is no evidence against the AI's "different points": `same_extent`
    lets two lines' ends differ by the values' own difference, which is what a top against an
    underside looks like. With end words naming different points, it stays grey whatever the
    geometry says, for "different points" and for vague end words alike."""
    monkeypatch.setattr(v, "same_extent", lambda *a: ("same_extent", "both ends on one line"))
    for ends in (
        ("floor", "top of counter", "floor", "underside of counter"),
        ("", "", "", ""),
    ):
        found = _one(_pair("p2.D1", "p1.D1", "different_points", ends))
        assert found.outcome == "not_compared" and not found.needs_blind
        assert found.reason == v.DIFFERENT_POINTS
        assert all(c.name != v.OBJECTION for c in found.checks)


@pytest.mark.parametrize(
    "ends",
    [
        # two different outlets: the prompt tells the AI to call these different points
        (
            "wall face (left)",
            "centre line (outlet A)",
            "wall face (left)",
            "centre line (outlet B)",
        ),
        # the same kind of end on two different units
        ("floor", "top of unit (base)", "floor", "top of unit (tall)"),
        ("cabinet", "cabinet", "filler", "panel"),
        ("centre of sink", "left wall", "centre of faucet", "left wall"),
        ("left wall", "cabinet side (front)", "left wall", "cabinet side (front of door)"),
    ],
)
def test_a_different_points_with_ends_of_one_kind_but_other_words_stays_grey(
    monkeypatch: pytest.MonkeyPatch, ends: tuple[str, str, str, str]
) -> None:
    """Only end words that are the same words contradict the AI's "different points" (#1773
    review): two ends of one rough kind (two outlets, two units' tops) are not the same point, so
    the "no" stands and nothing goes to the blind look."""
    monkeypatch.setattr(v, "same_extent", lambda *a: ("cannot_tell", "too few dimensions"))
    found = _one(_pair("p2.D1", "p1.D1", "different_points", ends))
    assert found.outcome == "not_compared" and not found.needs_blind
    assert found.reason == v.DIFFERENT_POINTS


def test_a_different_points_with_a_note_stays_grey(monkeypatch: pytest.MonkeyPatch) -> None:
    """The AI's note is where it says why the points differ: with a note, its "no" stands even
    when its end words are the same."""
    monkeypatch.setattr(v, "same_extent", lambda *a: ("cannot_tell", "too few dimensions"))
    pair = _pair("p2.D1", "p1.D1", "different_points")
    pair["note"] = "the vendor's runs to the stone's underside, the architect's to its top"
    found = _one(pair)
    assert found.outcome == "not_compared" and not found.needs_blind
    assert found.reason == v.DIFFERENT_POINTS


def test_a_different_points_same_words_in_the_other_order_goes_to_the_blind_look(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(v, "same_extent", lambda *a: ("cannot_tell", "too few dimensions"))
    ends = ("left wall", "right wall", "Right_Wall", "left wall")
    found = _one(_pair("p2.D1", "p1.D1", "different_points", ends))
    assert found.outcome == "candidate" and found.needs_blind


@pytest.mark.parametrize(
    ("pair", "item", "geometry"),
    [
        # its own ends name different points and code cannot tell: the "no" stands
        (
            _pair(
                "p2.D1",
                "p1.D1",
                "different_points",
                ("floor", "top of counter", "floor", "underside of counter"),
            ),
            ROUND_2,
            ("cannot_tell", "too few dimensions"),
        ),
        # code's geometry shows different points
        (_pair("p2.D1", "p1.D1", "different_points"), ROUND_2, ("different_extent", "apart")),
        # an end at room context (another trade)
        (
            _pair("p2.D1", "p1.D1", "different_points", ("floor", "ceiling", "floor", "ceiling")),
            ROUND_2,
            ("cannot_tell", "too few dimensions"),
        ),
        # the values code reads are equal
        (
            _pair(
                "p1.D7",
                "p1.D3",
                "different_points",
                ("wall", "outlet centre line", "wall", "outlet centre line"),
            ),
            ROUND_1,
            ("cannot_tell", "too few dimensions"),
        ),
        # an older round: round 2 is the vendor's current drawing
        (_pair("p1.D5", "p1.D1", "different_points"), ROUND_1, ("cannot_tell", "few")),
        # a width against a height
        (_pair("p2.D2", "p1.D1", "different_points"), ROUND_2, ("cannot_tell", "few")),
    ],
)
def test_a_different_points_code_does_not_contradict_stays_grey(
    monkeypatch: pytest.MonkeyPatch,
    pair: dict[str, Any],
    item: Item,
    geometry: tuple[str, str],
) -> None:
    monkeypatch.setattr(v, "same_extent", lambda *a: geometry)
    found = _one(pair, item)
    assert found.outcome == "not_compared" and not found.needs_blind
    assert found.reason == v.DIFFERENT_POINTS and found.reason_code == "different_reference"
    assert proof.classified(_stored(found.finding)).reason == "different_reference"


def test_unsure_with_no_architect_label_has_its_own_reason() -> None:
    """An "unsure" answer that names no architect label is not "architect gives no size": the
    architect may well print it. It is listed with its own reason (a $0 app run, 2026-10-19,
    listed every unsure label as "architect gives no size")."""
    for verdict in ("unsure", "maybe"):
        unsure = _one(_pair("p2.D1", None, verdict))
        assert unsure.outcome == "listed" and unsure.reason_code == "ai_unsure"
        assert unsure.reason == v.AI_UNSURE
        assert unsure.reason == "the AI was unsure and no printed value was found"
        assert unsure.reason_code in classification.REASONS
        # The list goes to the vendor: its words for the reason carry no "AI".
        words = classification.REASON_WORDS["ai_unsure"]
        assert not internal(words) and "no size" not in words
    # "No architect value" stays the architect's silence.
    nothing = _one(_pair("p2.D1", None, "no_architect_value"))
    assert nothing.reason_code == "nothing_to_compare"


def test_same_is_right_only_when_code_reads_the_values_equal() -> None:
    right = _one(
        _pair("p1.D7", "p1.D3", "same", ("wall", "outlet CL", "wall", "outlet centre line")),
        ROUND_1,
    )
    assert right.outcome == "right" and right.finding["status"] == "same"
    assert proof.proof_group(_stored(right.finding)) == "right"
    differs = _one(_pair("p2.D1", "p1.D1", "same"))
    assert differs.outcome == "not_compared" and "called the same, but code reads" in differs.reason
    equal = _one(
        _pair(
            "p1.D7",
            "p1.D3",
            "different",
            ("cabinet side", "outlet centre line", "cabinet side", "outlet CL"),
        ),
        ROUND_1,
    )
    assert equal.outcome == "right" and "code reads them equal" in equal.finding["why"]


def test_a_same_on_different_points_is_not_right() -> None:
    found = _one(
        _pair(
            "p1.D7", "p1.D3", "same", ("floor", "top of counter", "floor", "underside of counter")
        ),
        ROUND_1,
    )
    assert found.outcome == "not_compared" and found.reason_code == "different_reference"


def test_a_vendor_label_code_paired_itself_is_left_to_codes_own_finding() -> None:
    pack = _pack(
        pairs={"pairs": [{"vendor": "p1.D7 305 [12]", "architect": "p1.D3 1'", "status": "same"}]}
    )
    found = _one(_pair("p1.D7", "p1.D1"), ROUND_1, pack)
    assert found.outcome == "covered" and "code paired p1.D7 with p1.D3" in found.reason


def test_a_label_paired_twice_or_unknown_is_never_red() -> None:
    result = {
        "work_item": "p2.v2",
        "pairs": [_pair("p2.D1", "p1.D1"), _pair("p2.D1", "p1.D3"), _pair("p1.D5", "p1.D1")],
        "other_items": [],
        "unaccounted_labels": ["p2.D5"],
    }
    found = verify_answer(result, ROUND_2, _pack(), ink=lambda label: BLACK)
    assert [i.outcome for i in found.items] == ["not_compared"]
    assert "more than once" in found.items[0].reason
    assert found.unknown_labels == ["p1.D5"]
    assert found.unaccounted == ["p2.D5"]
    assert found.missing_labels == ["p2.D2", "p2.D3", "p2.D4", "p2.D6", "p2.D7", "p2.D8"]


def test_a_pair_naming_no_architect_label_is_grey() -> None:
    found = _one(_pair("p2.D1", None, "different"))
    assert found.outcome == "not_compared" and "names no architect label" in found.reason


# ---- items missing on the vendor's drawing ----------------------------------------------------


def _other(**fields: Any) -> dict[str, Any]:
    base = {
        "kind": "missing_on_vendor",
        "what": "outlet in the backsplash, centre line to the right wall",
        "vendor": None,
        "architect": "1' - 8\"",
        "vendor_box": None,
        "architect_box": [295, 95, 335, 115],
        "architect_page": 1,
        "verdict": "different",
        "millwork": True,
    }
    return {**base, **fields}


def _item(
    entry: dict[str, Any], item: Item = ROUND_2, pack: Pack | None = None, read: Any = None
) -> v.Verified:
    result = {"work_item": item.id, "pairs": [], "other_items": [entry], "unaccounted_labels": []}
    found = verify_answer(result, item, pack or _pack(), read=read)
    return found.items[0]


def test_a_millwork_item_no_vendor_sheet_prints_is_a_candidate_then_red() -> None:
    found = _item(_other())
    assert found.outcome == "candidate" and found.blind == "absence"
    assert found.finding["status"] == "different" and found.finding["paired_by"] == "code"
    agree = BlindAnswer(
        pick=None,
        verdict="absent",
        millwork=True,
        architect_from="outlet CL",
        architect_to="right wall",
    )
    red = with_blind(found, agree)
    assert red.outcome == "red"
    stored = _stored(red.finding)
    assert "text" in stored["witnesses"]
    assert proof.proof_group(stored) == "clouded"


@pytest.mark.parametrize(
    ("blind", "words"),
    [
        (BlindAnswer(pick="p2.D3", verdict="shown", millwork=True), "finds it on the vendor"),
        (BlindAnswer(pick=None, verdict="unsure", millwork=True), "not sure"),
        (BlindAnswer(pick=None, verdict="absent", millwork=False), "not millwork"),
        (
            BlindAnswer(pick=None, verdict="absent", millwork=True, architect_to="soffit"),
            "room context",
        ),
    ],
)
def test_an_absence_the_blind_look_does_not_confirm_is_grey(blind: BlindAnswer, words: str) -> None:
    grey = with_blind(_item(_other()), blind)
    assert grey.outcome == "not_compared" and words in grey.reason
    stored = _stored(grey.finding)
    assert "text" not in stored["witnesses"]
    assert proof.proof_group(stored) == "not_compared"


@pytest.mark.parametrize(
    ("fields", "words"),
    [
        ({"millwork": False}, "not millwork, by the AI's reading"),
        ({"what": "floor to ceiling"}, "room context or another trade's work (ceiling)"),
        ({"what": "floor to door head"}, "(door head)"),
        ({"what": "light switch centre line to the left wall"}, "(light switch)"),
        # no product word in its words, none printed beside it on the architect's drawing
        (
            {"what": "receptacle to the right wall", "architect_box": [100, 300, 130, 310]},
            "no millwork word",
        ),
    ],
)
def test_the_code_scope_gate_keeps_room_context_out(fields: dict[str, Any], words: str) -> None:
    found = _item(_other(**fields))
    assert found.outcome == "listed" and words in found.reason


def test_the_scope_gate_reads_the_architects_words_beside_the_item() -> None:
    found = _item(_other(what="receptacle to the right wall"))  # BACKSPLASH is printed beside it
    assert found.outcome == "candidate"


def test_an_absence_code_cannot_prove_is_grey() -> None:
    printed = _item(_other(architect='16"', architect_box=[295, 95, 335, 115]))
    assert printed.outcome == "not_compared"
    assert 'code finds no 16" printed' in printed.reason
    shown = _item(
        _other(
            architect="1' - 0\"",
            architect_box=[195, 95, 235, 115],
            what="backsplash outlet spacing",
        )
    )
    assert shown.outcome == "not_compared" and "the vendor prints it on page 1" in shown.reason
    docs = _documents()
    docs["dims"]["pages"][1]["phrases"] = []
    blind_sheet = _item(_other(), pack=Pack.from_documents(main_file=FILE, **docs))
    assert blind_sheet.outcome == "not_compared" and "no readable text layer" in blind_sheet.reason


# ---- a dimension code has no label for --------------------------------------------------------


def _reader(results: Mapping[tuple[int, str], str], route: str = "drawn") -> Any:
    def read(file: str, page: int, box: Any, expected: str) -> dict[str, Any]:
        result = results.get((page, expected), "cannot_read")
        return {"result": result, "route": route if result == "match" else "none", "read": expected}

    return read


def _unlabelled(**fields: Any) -> dict[str, Any]:
    base = {
        "kind": "dimension_without_label",
        "what": "floor to outlet centre line",
        "vendor": '46"',
        "architect": "3' - 1\"",
        "vendor_box": [440, 300, 450, 320],
        "architect_box": [440, 200, 450, 220],
        "architect_page": 1,
        "verdict": "different",
        "millwork": True,
    }
    return {**base, **fields}


def test_an_unlabelled_pair_code_reads_at_both_boxes_is_a_candidate_then_red() -> None:
    read = _reader({(2, '46"'): "match", (1, "3' - 1\""): "match"})
    found = _item(_unlabelled(), read=read)
    assert found.outcome == "candidate" and found.blind == "values"
    assert found.reads["verify_vendor"]["result"] == "match"
    blind = BlindAnswer(
        pick=None,
        verdict="same_thing",
        vendor_from="floor",
        vendor_to="outlet CL",
        architect_from="floor",
        architect_to="outlet centre line",
        vendor_ink_black="yes",
    )
    red = with_blind(found, blind)
    assert red.outcome == "red" and red.finding["critic"]["result"] == "passed"


@pytest.mark.parametrize(
    ("read", "fields", "words", "code"),
    [
        (None, {}, "value could not be read by code", "unreadable"),
        (_reader({(2, '46"'): "match"}), {}, "could not confirm the architect value", "unreadable"),
        (
            _reader({(1, "3' - 1\""): "match"}),
            {},
            "could not confirm the vendor value",
            "unreadable",
        ),
        # code's labels at the two spots run different ways (a height against a width)
        (
            _reader({(2, '34"'): "match", (1, "3' - 6\""): "match"}),
            {
                "vendor": '34"',
                "vendor_box": [15, 95, 35, 135],
                "architect": "3' - 6\"",
                "architect_box": [95, 95, 135, 115],
            },
            "a width and the other a height",
            "not_proved",
        ),
        # a value that is not one size
        (None, {"architect": '39 1/4" + 6"'}, "not one size", "unreadable"),
        # the architect box on the brand prototype's page
        (
            _reader({(2, '46"'): "match", (3, "3' - 1\""): "match"}),
            {"architect_page": 3},
            "the reference is the brand prototype",
            "prototype",
        ),
    ],
)
def test_an_unlabelled_pair_code_cannot_read_is_not_compared(
    read: Any, fields: dict[str, Any], words: str, code: str
) -> None:
    found = _item(_unlabelled(**fields), read=read)
    assert found.outcome == "not_compared" and words in found.reason, found.reason
    assert found.reason_code == code


@pytest.mark.parametrize(
    ("ends", "outcome"),
    [
        (("floor", "outlet CL", "floor", "outlet centre line"), "red"),
        (("cabinet side", "cabinet side", "cabinet side", "cabinet side"), "not_compared"),
    ],
)
def test_without_labels_the_blind_looks_ends_say_which_way_they_run(
    ends: tuple[str, str, str, str], outcome: str
) -> None:
    read = _reader({(2, '46"'): "match", (1, "3' - 1\""): "match"})
    found = _item(_unlabelled(), read=read)
    blind = BlindAnswer(
        pick=None,
        verdict="same_thing",
        vendor_from=ends[0],
        vendor_to=ends[1],
        architect_from=ends[2],
        architect_to=ends[3],
        vendor_ink_black="yes",
    )
    after = with_blind(found, blind)
    assert after.outcome == outcome
    if outcome != "red":
        assert "run the same way" in after.reason


def test_an_unlabelled_pair_on_round_1_is_not_red() -> None:
    read = _reader({(1, '46"'): "match", (1, "3' - 1\""): "match"})
    found = _item(_unlabelled(vendor_box=[440, 600, 450, 620]), ROUND_1, read=read)
    assert found.outcome == "not_compared" and "round 1" in found.reason


# ---- notes, tags, layouts, strings ------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "reason_code"),
    [
        ("note", "not_proved"),
        ("tag", "not_proved"),
        ("layout_count", "not_proved"),
        ("added_on_vendor", "not_proved"),
    ],
)
def test_items_code_cannot_check_are_not_compared(kind: str, reason_code: str) -> None:
    found = _item(
        {
            "kind": kind,
            "what": 'vendor string 24" + 18" + 6" does not add up to the printed 50"',
            "vendor": '50"',
            "architect": '48"',
            "vendor_box": [190, 290, 240, 400],
            "architect_box": None,
            "architect_page": None,
            "verdict": "different",
            "millwork": True,
        }
    )
    assert found.outcome == "not_compared" and found.reason_code == reason_code
    assert proof.proof_group(_stored(found.finding)) == "not_compared"


def test_an_ai_string_is_never_red_and_a_layout_is_said_so() -> None:
    layout = _item({**_unlabelled(), "kind": "layout_count", "what": "drawers against doors"})
    stored = _stored(layout.finding)
    assert proof.classified(stored).why == (
        "the layout differs by the readings, but code could not prove the counts"
    )
    same = _item({**_unlabelled(), "kind": "note", "verdict": "same"})
    assert same.outcome == "covered" and same.finding == {}


# ---- where each end lands ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("words", "kind"),
    [
        ("top_of_counter", "counter_top"),
        ("underside_of_counter", "counter_under"),
        ("centre_line (fridge outlet)", "outlet"),
        ("ceiling_soffit", "room"),
        ("box_above_unit", "room"),
        ("underside_of_upper", "upper_under"),
        ("top_of_unit", "unit_top"),
        ("wall_face (left)", "wall"),
        ("cabinet door edge", "cabinet"),
        ("floor to door head", "room"),
        ("", "none"),
        ("trim", "other"),
        # A side splash's top is not a back splash's; the splash's face is not the wall.
        ("top_of_splash", "splash_top"),
        ("top_of_side_splash", "side_splash_top"),
        ("face_of_splash (front)", "splash_face"),
        ("back splash face", "splash_face"),
    ],
)
def test_end_words(words: str, kind: str) -> None:
    assert end_kind(words)[0] == kind


def test_a_back_splash_is_never_a_side_splash_or_the_wall() -> None:
    from app.review.assistant.simple_check.verify import ends_match

    _, why = ends_match("top_of_counter", "top_of_splash", "top_of_counter", "top_of_side_splash")
    found, _ = ends_match("top_of_counter", "top_of_splash", "top_of_counter", "top_of_side_splash")
    assert found == "different", why
    found, _ = ends_match("face_of_splash", "wall_face", "wall_face", "cabinet_side")
    assert found != "same"


@pytest.mark.parametrize(
    ("ends", "state"),
    [
        (("left wall", "right wall", "right wall", "left wall"), "same"),
        (("floor", "CL", "floor", "outlet centre line"), "same"),
        (("left wall", "right wall", "left wall", "left wall"), "different"),
        (("floor", "top of counter", "floor", "underside of counter"), "different"),
        (("sink centre line", "wall", "left wall", "right wall"), "different"),
        (("cabinet side", "wall", "wall", "wall"), "unclear"),
        (("floor", "", "floor", "top of counter"), "unclear"),
        (("floor", "top of bulkhead", "floor", "top of unit"), "room"),
    ],
)
def test_ends_match(ends: tuple[str, str, str, str], state: str) -> None:
    assert ends_match(*ends)[0] == state


# ---- the harness entry ------------------------------------------------------------------------


def test_verify_works_out_the_work_item_from_its_id(tmp_path: Path) -> None:
    import json

    docs = _documents()
    for name, key in (
        ("dims.json", "dims"),
        ("page_map.json", "page_map"),
        ("pairs.json", "pairs"),
    ):
        (tmp_path / name).write_text(json.dumps(docs[key]))
    (tmp_path / "page_texts.json").write_text(json.dumps(docs["page_texts"]))
    result = {
        "work_item": "p2.v2",
        "pairs": [_pair("p2.D1", "p1.D1"), _pair("p2.D2", None, "no_architect_value")],
        "other_items": [],
        "unaccounted_labels": [],
    }
    rows = verify(result, tmp_path, ink=lambda label: BLACK)
    assert [(r["verdict"], r["needs_blind"]) for r in rows] == [
        ("wrong", True),
        ("not_compared", False),
    ]
    unknown = verify({**result, "work_item": "p9.v1"}, tmp_path)
    assert unknown[0]["verdict"] == "not_compared"
    assert unknown[0]["reason"] == "code does not know this work item"


# ---- the AI says only "same points"; code compares the numbers (#1775, C3) ----------------------


def test_same_points_with_values_code_reads_different_is_a_candidate() -> None:
    found = _one(_pair("p2.D1", "p1.D1", "same_points"))
    assert found.outcome == "candidate" and found.needs_blind
    assert with_blind(found, _agrees(found)).outcome == "red"
    # The older form's "same" on the very same pair stays Not compared (saved answers replay so).
    older = _one(_pair("p2.D1", "p1.D1", "same"))
    assert older.outcome == "not_compared"


def test_same_points_with_values_code_reads_equal_is_right() -> None:
    right = _one(
        _pair("p1.D7", "p1.D3", "same_points", ("wall", "outlet CL", "wall", "outlet centre line")),
        ROUND_1,
    )
    assert right.outcome == "right" and "answer said different" not in right.finding["why"]


def test_same_points_never_passes_a_failed_code_check() -> None:
    found = _one(
        _pair(
            "p2.D1",
            "p1.D1",
            "same_points",
            ("floor", "top of counter", "floor", "underside of counter"),
        )
    )
    assert found.outcome == "not_compared" and found.reason_code == "different_reference"
    unsure = _one(_pair("p2.D1", "p1.D1", "unsure"))
    assert unsure.outcome == "not_compared"


def test_same_points_on_an_end_the_ai_cannot_see_is_never_a_candidate() -> None:
    """The weak point only adds caution (C3): an end the AI says it cannot see makes it unsure."""
    doubtful = {
        **_pair("p2.D1", "p1.D1", "same_points"),
        "weak_point": "end_not_visible",
        "note": "the left end is faint",
    }
    found = _one(doubtful)
    assert found.outcome == "not_compared" and found.reason_code == "not_proved", found.reason
    assert "faint" in found.reason
    # Any other weak point leaves code's checks to decide, as before.
    assert _one({**doubtful, "weak_point": "views_differ"}).outcome == "candidate"


def test_same_points_naming_no_architect_label_is_grey() -> None:
    found = _one(_pair("p2.D1", None, "same_points"))
    assert found.outcome == "not_compared" and "names no architect label" in found.reason


def test_same_points_with_a_value_code_cannot_read_is_grey() -> None:
    docs = _documents()
    docs["dims"]["pages"][1]["labels"][0]["inches"] = None
    found = _one(_pair("p2.D1", "p1.D1", "same_points"), pack=_pack(dims=docs["dims"]))
    assert found.outcome == "not_compared" and found.reason_code == "unreadable", found.reason
