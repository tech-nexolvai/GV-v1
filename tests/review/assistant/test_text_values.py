"""Short text values (2026-11-04): sheet and revision numbers, callouts, tags and dates are text,
never sizes, and a one- or two-character value proves its place only in its labelled field.
Made-up values, phrases and pages; no client data.

A made-up title block on page 1 of each file: a street address that ends in a digit, a REV heading
with a value under it, and a SHEET NUMBER heading over a sheet number with its discipline letters.
"""

from __future__ import annotations

from typing import Any

from app.review.assistant import classification, proof, spot_locate, text_values
from app.review.assistant.chat_trust import code_read_both, judged
from app.review.assistant.escalation import EscalationContext, escalate

VENDOR = "vendor.pdf"
ARCHITECT = "architect.pdf"
ADDRESS = [100.0, 10.0, 240.0, 24.0]
ADDRESS_DIGIT = [232.0, 10.0, 240.0, 24.0]
REV_HEADING = [300.0, 100.0, 320.0, 110.0]
REV_VALUE = [305.0, 120.0, 315.0, 130.0]
SHEET_VALUE = [400.0, 150.0, 480.0, 170.0]
LONE = [600.0, 600.0, 606.0, 610.0]


def _phrase(text: str, box: list[float]) -> dict[str, Any]:
    return {"text": text, "box": list(box)}


def _title_block(rev: str = "4", sheet: str = "AB 4.2") -> list[dict[str, Any]]:
    return [
        _phrase("48 MADE-UP ROAD 6", ADDRESS),
        _phrase("REV", REV_HEADING),
        _phrase(rev, REV_VALUE),
        _phrase("SHEET NUMBER", [400.0, 100.0, 480.0, 110.0]),
        _phrase(sheet, SHEET_VALUE),
        _phrase("7", LONE),
    ]


def _box(file: str, box: list[float], page: int = 1) -> dict[str, Any]:
    return {"page": page, "file": file, "x0": box[0], "y0": box[1], "x1": box[2], "y1": box[3]}


def _finding(
    item: str,
    vendor: str,
    architect: str,
    status: str,
    vendor_at: list[float],
    architect_at: list[float],
) -> dict[str, Any]:
    """A helper's item as the full check stores it: both sides read as sizes by the old checks."""
    return {
        "id": "F1",
        "page": 1,
        "vs_page": 1,
        "item": item,
        "vendor": vendor,
        "architect": architect,
        "status": status,
        "source": "architect",
        "category": "other",
        "created_by": "helper:1",
        "compared": True,
        "vendor_box": _box(VENDOR, vendor_at),
        "arch_box": _box(ARCHITECT, architect_at),
        "inches": {"vendor": vendor, "architect": architect, "difference": "0"},
        "checks": {"printed_vendor": "match", "printed_architect": "match"},
        "witnesses": ["printed"],
        "dims": {
            "vendor": {"id": "D1", "page": 1, "file": VENDOR, "text": vendor, "route": "text"},
            "architect": {
                "id": "D1",
                "page": 1,
                "file": ARCHITECT,
                "text": architect,
                "route": "text",
            },
        },
    }


def _escalated(
    finding: dict[str, Any], vendor_page: list[dict[str, Any]], arch_page: list[dict[str, Any]]
) -> dict[str, Any]:
    ctx = EscalationContext(
        main_file=VENDOR, phrases={(VENDOR, 1): vendor_page, (ARCHITECT, 1): arch_page}
    )
    result = escalate([text_values.as_text(finding)], ctx)
    changed = {str(f["id"]): f for f in result.changed}
    return judged(changed.get("F1", finding))


# ---- what an identifier is ----------------------------------------------------------------------


def test_an_item_naming_an_identifier_is_one_and_a_size_never_is() -> None:
    def kind(item: str, vendor: str = "4", architect: str = "4") -> str | None:
        return text_values.kind({"item": item, "vendor": vendor, "architect": architect})

    assert kind("Key plan: revision number") == "revision"
    assert kind("Plan: sheet number", "4.2", "4.5") == "sheet"
    assert kind("Elevation: detail callout", "9.9X", "9.9Y") == "callout"
    assert kind("Room number", "512", "512") == "number"
    assert kind("Title block: issue date", "03/14/2026", "03/21/2026") == "date"
    assert kind("Something", "REV 2", "REV 3") == "revision"  # its own field word
    assert kind("Detail 3: top width") is None  # a size, whatever else it names
    assert kind("Sink cut-out detail: front offset") is None
    assert kind("Revision cloud at the sink") is None  # a mark on the drawing, not a number
    assert kind("Vanity: top tag", "not shown", "AB-12") is None  # an absence keeps its path
    assert kind("Title block: item number", "see the drawing", "n/a") is None
    assert kind("Cabinet tag", "AB-12", "AB-12") is None  # the tag reader's
    assert (
        text_values.kind(
            {
                "item": "Plan: sheet number",
                "vendor": "4",
                "architect": "5",
                "source": "vendor_arithmetic",
            }
        )
        is None
    )


def test_values_compare_as_text_never_as_sizes() -> None:
    assert text_values.normal("REV. NO. 2") == "2"
    assert text_values.normal("A-101") == text_values.normal("a 101") == "A101"
    assert text_values.normal("03/14/2026") == text_values.normal("2026/03/14") == "2026-03-14"
    assert text_values.same_text("4.2", "4.2")
    assert not text_values.same_text("4.2", "4.20")  # equal as numbers, not as sheet numbers
    assert text_values.short("6") and text_values.short("B2") and not text_values.short("4.2")


# ---- proof of place ------------------------------------------------------------------------------


def test_a_digit_inside_an_address_is_no_value_of_its_own() -> None:
    found = text_values.place(_title_block(), ADDRESS_DIGIT, "6", "revision")
    assert not found.printed and not found.placed
    assert "part of the printed words" in found.why


def test_a_short_value_under_its_heading_is_in_its_place() -> None:
    found = text_values.place(_title_block(), REV_VALUE, "4", "revision")
    assert found.placed and found.field == "REV"


def test_a_short_value_with_its_own_field_word_is_in_its_place() -> None:
    page = [_phrase("REV 4", REV_VALUE)]
    assert text_values.place(page, REV_VALUE, "4", "revision").placed


def test_a_lone_short_value_with_no_heading_proves_nothing() -> None:
    found = text_values.place(_title_block(), LONE, "7", "revision")
    assert found.printed and not found.placed
    assert "labelled field" in found.why


def test_a_short_value_under_another_kind_of_heading_proves_nothing() -> None:
    page = [_phrase("SCALE", REV_HEADING), _phrase("4", REV_VALUE)]
    assert not text_values.place(page, REV_VALUE, "4", "revision").placed


def test_a_sheet_number_read_without_its_discipline_letters_is_found_whole() -> None:
    found = text_values.place(_title_block(), SHEET_VALUE, "4.2", "sheet")
    assert found.placed and found.read == "AB4.2"


def test_nothing_printed_at_the_spot_is_said_so() -> None:
    assert "no printed text" in text_values.place([], LONE, "7", "revision").why


# ---- never a size --------------------------------------------------------------------------------


def test_an_identifier_holds_no_inches_and_no_printed_size_match() -> None:
    item = _finding("Plan: sheet number", "4.2", "4.5", "different", SHEET_VALUE, SHEET_VALUE)
    found = text_values.as_text(item)
    assert found["inches"] == {"architect": None, "vendor": None, "difference": None}
    assert found["checks"]["printed_vendor"] == "not_a_number"
    assert "printed" not in found["witnesses"]
    size = {**item, "item": "Top: width"}
    assert text_values.as_text(size) == size


def test_a_sheet_number_difference_never_speaks_in_inches() -> None:
    item = _finding("Plan: sheet number", "4.2", "4.5", "different", SHEET_VALUE, SHEET_VALUE)
    for stored in (item, text_values.as_text(item)):
        found = proof.proof_of(stored)
        assert found["group"] != "clouded"
        assert "4 3/16" not in str(found) and "5/16" not in str(found)  # 4.2 and 4.5 as inches
    assert not proof.code_proved(item)


# ---- the outcome ---------------------------------------------------------------------------------


def test_an_address_digit_called_the_same_revision_is_never_right() -> None:
    item = _finding("Key plan: revision number", "6", "6", "same", ADDRESS_DIGIT, ADDRESS_DIGIT)
    assert proof.proof_group(item) == "not_compared"  # stored, no record: not proved
    assert not code_read_both(item)
    found = _escalated(item, _title_block(), _title_block())
    assert proof.proof_group(found) == "not_compared"
    assert "part of the printed words" in proof.proof_of(found)["reason"]


def test_two_revision_numbers_are_never_right_or_wrong() -> None:
    """PROPOSED 2026-11-04: each drawing's own numbering proves nothing about the drawings."""
    for vendor, architect, status in (("4", "4", "same"), ("4", "5", "different")):
        item = _finding(
            "Key plan: revision number", vendor, architect, status, REV_VALUE, REV_VALUE
        )
        found = _escalated(item, _title_block(rev=vendor), _title_block(rev=architect))
        assert found[text_values.FIELD]["vendor"]["placed"]
        assert proof.proof_group(found) == "not_compared"
        assert proof.proof_of(found)["reason"] == text_values.OWN_NUMBERING


def test_the_same_sheet_number_read_at_both_spots_is_right() -> None:
    item = _finding("Plan: sheet number", "4.2", "AB 4.2", "same", SHEET_VALUE, SHEET_VALUE)
    found = _escalated(item, _title_block(), _title_block())
    assert proof.proof_group(found) == "right"
    assert code_read_both(found)


def test_sheet_numbers_that_differ_at_both_spots_are_a_please_check_in_words() -> None:
    item = _finding("Plan: sheet number", "4.2", "4.5", "different", SHEET_VALUE, SHEET_VALUE)
    found = _escalated(item, _title_block(sheet="AB 4.2"), _title_block(sheet="AB 4.5"))
    assert proof.proof_group(found) == "likely_problem"
    words = proof.proof_of(found)["likely_problem"]["words"]
    assert '"AB 4.2"' in words and '"AB 4.5"' in words and "differ" in words


def test_a_short_tag_with_no_labelled_field_is_not_proved() -> None:
    item = _finding("Plan: room type tag", "7", "7", "same", LONE, LONE)
    found = _escalated(item, _title_block(), _title_block())
    assert proof.proof_group(found) == "not_compared"
    assert classification.classify(found, proved=False).reason == "not_proved"


def test_a_stale_record_counts_for_nothing() -> None:
    item = _finding("Plan: sheet number", "4.2", "AB 4.2", "same", SHEET_VALUE, SHEET_VALUE)
    found = _escalated(item, _title_block(), _title_block())
    moved = {**found, "arch_box": _box(ARCHITECT, LONE)}
    assert proof.proof_group(moved) == "not_compared"


def test_an_identifier_is_never_placed_on_a_dimension_label_elsewhere() -> None:
    item = {
        "id": "F1",
        "page": 1,
        "item": "Key plan: revision number",
        "vendor": "6",
        "architect": "6",
        "status": "same",
    }
    labels = [{"id": "D9", "text": "6", "inches": "6", "box": ADDRESS_DIGIT, "side": "vendor"}]
    found = spot_locate.relocate(item, labels, file=VENDOR, sides=("vendor",))
    assert "vendor_box" not in found
    assert found[spot_locate.SPOT_SEARCH]["vendor"]["why"] == spot_locate.IDENTIFIER_WHY


def test_an_unsure_pairing_compares_identifiers_as_text() -> None:
    item = {
        "item": "Plan: sheet number",
        "vendor": "4.2",
        "architect": "4.20",
        "status": "check",
        "check_reason": "pairing_not_certain",
        "created_by": "helper:1",
    }
    assert classification.pairing_unsure(item)  # differ as text, though equal as numbers


def test_an_answer_stored_before_reads_its_identifiers_as_text() -> None:
    sheet = _finding("Plan: sheet number", "4.2", "4.5", "different", SHEET_VALUE, SHEET_VALUE)
    size = {**sheet, "id": "F2", "item": "Top: width"}
    blocks = [{"type": "finding", "finding": sheet}, {"type": "finding", "finding": size}]
    shown = text_values.blocks_as_text(blocks) or []
    assert shown[0]["finding"]["inches"]["vendor"] is None
    assert shown[1]["finding"] == size
    assert text_values.blocks_as_text(None) is None
