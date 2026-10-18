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

ID = "air:0000aaaa-1111-2222-3333-444455556666"


def test_millimetres_come_after_the_inches() -> None:
    assert inches_first("1219 [48]") == '48" (1219 mm)'
    assert inches_first("2007 [79 1/32] overall; parts 610 [24]") == (
        '79 1/32" (2007 mm) overall; parts 24" (610 mm)'
    )
    # Inches alone, a bracket alone and a page reference stay as they are.
    assert inches_first('72" and [10] on p3') == '72" and [10] on p3'
    assert vendor_text("Vendor shows 1219 [48]") == 'Vendor shows 48" (1219 mm)'


def test_an_item_named_by_its_record_id_is_named_by_its_words() -> None:
    assert (
        item_name({"item": ID, "why": "Shelf depth, front to back"}) == "Shelf depth, front to back"
    )
    assert item_name({"item": ID, "why": "drawer to wall; 5 vs 3. More."}) == "Drawer to wall"
    # Code's own sentences never name it; the vendor's printed dimension then does.
    coded = {"item": ID, "why": "Code paired these.", "dims": {"vendor": {"text": "1219 [48]"}}}
    assert item_name(coded) == 'Dimension 48" (1219 mm)'
    assert item_name({"item": ID}) is None
    assert item_name({"item": "Pantry: top depth"}) == "Pantry: top depth"


def test_the_letter_and_the_drawing_never_say_item_on_page_when_words_exist() -> None:
    finding = {"id": "F3", "page": 3, "item": ID, "why": "Shelf depth, front to back"}
    assert send_package._item(finding) == "Shelf depth, front to back"
    assert item_words(finding) == "Shelf depth, front to back"
    assert send_package._item({"id": "F9", "page": 4, "item": ID}) == "Item on page 4"


def test_ai_page_review_rows_are_named_by_what_they_measure() -> None:
    document = {
        "ai_page_review": [
            {
                "page": 3,
                "different": [
                    {
                        "key": ID,
                        "measures": "shelf depth, front to back",
                        "architect_text": "1'-5\"",
                        "vendor_text": '17 1/4"',
                        "why": "w",
                        "verified_by": "evaluator",
                    },
                    {
                        "key": "air:9999bbbb-1111-2222-3333-444455556666",
                        "architect_text": "1'-0\"",
                        "vendor_text": "279 [11]",
                        "why": "Drawer to right wall; 11 vs 12",
                        "verified_by": "evaluator",
                    },
                ],
            }
        ]
    }
    names = [d["what"] for d in recorded_from(document).differences]
    assert names == ["Shelf depth, front to back", "Drawer to right wall"]


def test_an_item_with_only_a_record_id_takes_the_pairs_name() -> None:
    code = {
        "item": "Galley: outlet 1 to outlet 2",
        "architect": '9"',
        "vendor": "203 [8]",
        "status": "different",
        "dims": {},
        "why": "Code paired these.",
    }
    assert proved_changes(code, {"item": ID})["item"] == "Galley: outlet 1 to outlet 2"
    assert "item" not in proved_changes(code, {"item": "Galley: microwave outlets"})


def _string(finding_id: str, x0: float, vendor: str = '31 3/8"') -> dict:
    return {
        "id": finding_id,
        "page": 12,
        "item": "Dimension string (elevation)",
        "status": "different",
        "vendor": vendor,
        "architect": '30 5/8"',
        "vendor_box": {"page": 12, "x0": x0, "y0": 300.0, "x1": x0 + 150, "y1": 330.0},
    }


def test_two_identical_lines_are_named_apart_by_their_place() -> None:
    left, right = _string("F42", 100.0), _string("F43", 400.0)
    overall = {**_string("F41", 200.0, '94 1/8"'), "architect": '93"'}
    assert names_apart([right, overall, left]) == {
        "F42": "Dimension string (elevation, left)",
        "F43": "Dimension string (elevation, right)",
    }
    # Three across: left, middle, right; one above the other: upper, lower.
    middle = _string("F44", 250.0)
    assert names_apart([left, middle, right])["F44"] == "Dimension string (elevation, middle)"
    low = {
        **_string("F45", 100.0),
        "vendor_box": {"x0": 100.0, "y0": 600.0, "x1": 250.0, "y1": 630.0},
    }
    assert names_apart([left, low]) == {
        "F42": "Dimension string (elevation, upper)",
        "F45": "Dimension string (elevation, lower)",
    }
    # One per round: the round names them; two at one spot are one item, not renamed.
    rounds = [{**left, "round": 1}, {**right, "round": 2}]
    assert names_apart(rounds)["F43"] == "Dimension string (elevation, round 2)"
    assert names_apart([left, {**left, "id": "F50"}]) == {}
