"""Every item has a real name in what goes to the vendor (#1634): never a record id, never
"Item on page N" when better words exist; two identical lines are named apart by their place; and
millimetres come after the inches. Made-up values only."""

from __future__ import annotations

from app.review.assistant import send_package
from app.review.assistant.chat_trust import recorded_from
from app.review.assistant.full_check_lenses import names_apart
from app.review.assistant.pair_findings import proved_changes
from app.review.assistant.proof_drawing import item_words
from app.review.assistant.vendor_text import inches_first, item_name, vendor_text

ID = "air:77ad9c93-22fd-41fa-9cee-75f53b37b061"


def test_millimetres_come_after_the_inches() -> None:
    assert inches_first("1828 [72]") == '72" (1828 mm)'
    assert inches_first("2735 [107 3/4] overall; parts 762 [30]") == (
        '107 3/4" (2735 mm) overall; parts 30" (762 mm)'
    )
    # Inches alone, a bracket alone and a page reference stay as they are.
    assert inches_first('72" and [10] on p3') == '72" and [10] on p3'
    assert vendor_text("Vendor shows 1828 [72]") == 'Vendor shows 72" (1828 mm)'


def test_an_item_named_by_its_record_id_is_named_by_its_words() -> None:
    assert (
        item_name({"item": ID, "why": "Width, end to end of unit"}) == "Width, end to end of unit"
    )
    assert item_name({"item": ID, "why": "outlet to wall; 16 vs 14. More."}) == "Outlet to wall"
    # Code's own sentences never name it; the vendor's printed dimension then does.
    coded = {"item": ID, "why": "Code paired these.", "dims": {"vendor": {"text": "1828 [72]"}}}
    assert item_name(coded) == 'Dimension 72" (1828 mm)'
    assert item_name({"item": ID}) is None
    assert item_name({"item": "Buffet: top depth"}) == "Buffet: top depth"


def test_the_letter_and_the_drawing_never_say_item_on_page_when_words_exist() -> None:
    finding = {"id": "F3", "page": 3, "item": ID, "why": "Width, end to end of unit"}
    assert send_package._item(finding) == "Width, end to end of unit"
    assert item_words(finding) == "Width, end to end of unit"
    assert send_package._item({"id": "F9", "page": 4, "item": ID}) == "Item on page 4"


def test_ai_page_review_rows_are_named_by_what_they_measure() -> None:
    document = {
        "ai_page_review": [
            {
                "page": 3,
                "different": [
                    {
                        "key": ID,
                        "measures": "width, end to end",
                        "architect_text": "3'-6\"",
                        "vendor_text": '39 1/2"',
                        "why": "w",
                        "verified_by": "evaluator",
                    },
                    {
                        "key": "air:00540b6f-5703-4f74-9bb8-bc8288a1298f",
                        "architect_text": "1'-2\"",
                        "vendor_text": "407 [16]",
                        "why": "Outlet to right wall; 16 vs 14",
                        "verified_by": "evaluator",
                    },
                ],
            }
        ]
    }
    names = [d["what"] for d in recorded_from(document).differences]
    assert names == ["Width, end to end", "Outlet to right wall"]


def test_an_item_with_only_a_record_id_takes_the_pairs_name() -> None:
    code = {
        "item": "Galley: outlet 1 to outlet 2",
        "architect": '9"',
        "vendor": "254 [10]",
        "status": "different",
        "dims": {},
        "why": "Code paired these.",
    }
    assert proved_changes(code, {"item": ID})["item"] == "Galley: outlet 1 to outlet 2"
    assert "item" not in proved_changes(code, {"item": "Galley: microwave outlets"})


def _string(finding_id: str, x0: float, vendor: str = '70 1/4"') -> dict:
    return {
        "id": finding_id,
        "page": 53,
        "item": "Dimension string (elevation)",
        "status": "different",
        "vendor": vendor,
        "architect": '68 1/2"',
        "vendor_box": {"page": 53, "x0": x0, "y0": 700.0, "x1": x0 + 150, "y1": 730.0},
    }


def test_two_identical_lines_are_named_apart_by_their_place() -> None:
    left, right = _string("F112", 314.0), _string("F113", 598.0)
    overall = {**_string("F111", 386.0, '166 1/2"'), "architect": '166"'}
    assert names_apart([right, overall, left]) == {
        "F112": "Dimension string (elevation, left)",
        "F113": "Dimension string (elevation, right)",
    }
    # Three across: left, middle, right; one above the other: upper, lower.
    middle = _string("F114", 450.0)
    assert names_apart([left, middle, right])["F114"] == "Dimension string (elevation, middle)"
    low = {
        **_string("F115", 314.0),
        "vendor_box": {"x0": 314.0, "y0": 900.0, "x1": 464.0, "y1": 930.0},
    }
    assert names_apart([left, low]) == {
        "F112": "Dimension string (elevation, upper)",
        "F115": "Dimension string (elevation, lower)",
    }
    # One per round: the round names them; two at one spot are one item, not renamed.
    rounds = [{**left, "round": 1}, {**right, "round": 2}]
    assert names_apart(rounds)["F113"] == "Dimension string (elevation, round 2)"
    assert names_apart([left, {**left, "id": "F120"}]) == {}
