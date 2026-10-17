"""Code's trust checks on a chat finding (chat v3): inches by code, printed at the spot, arithmetic,
trust level. $0: no model, no network; the PDF is a small synthetic one built here, with text both in
the page's own content and inside a pasted `/Stamp` drawing, as on GV's combined sheets."""

from __future__ import annotations

import zlib
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant.finding_checks import (
    Checks,
    arithmetic_check,
    arithmetic_off_by,
    check_finding,
    printed_at_spot,
    to_inches,
    trust_level,
)

# ---- inches by code ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "inches"),
    [
        # feet and inches
        ("3'-1\"", "37"),
        ("3' - 1\"", "37"),
        ("3'-1 1/2\"", "37.5"),
        ("3' - 1 1/2\"", "37.5"),
        ("3'-1-1/2\"", "37.5"),
        ("10'-0\"", "120"),
        ("3'", "36"),
        ("3'1\"", "37"),
        ("3 ft", "36"),
        ("2’ -5”", "29"),
        ("3′-1″", "37"),
        ("3'–1\"", "37"),
        ("8'-6''", "102"),
        # inches, fractions, unicode fractions, decimals
        ('37"', "37"),
        ("37", "37"),
        ("37 in", "37"),
        ("37 inches", "37"),
        ('37 1/2"', "37.5"),
        ('37-1/2"', "37.5"),
        ('37½"', "37.5"),
        ('37 ½"', "37.5"),
        ("23¼", "23.25"),
        ('34¾"', "34.75"),
        ('⅛"', "0.125"),
        ('1/2"', "0.5"),
        ("36.25", "36.25"),
        ('36.5"', "36.5"),
        ("38 3/4", "38.75"),
        ('0"', "0"),
        ('  37"  ', "37"),
        # millimetres with inches: the inches count
        ("940 [37]", "37"),
        ('940 [37"]', "37"),
        ("940 (37)", "37"),
        ("984 [38 3/4]", "38.75"),
        ('984mm [38 3/4"]', "38.75"),
        # trailing words and notes
        ("2'-9\" AFF", "33"),
        ('36" TYP.', "36"),
        ('36" (VIF)', "36"),
        ("2'-9\" A.F.F.", "33"),
    ],
)
def test_written_values_read_as_exact_inches(text: str, inches: str) -> None:
    assert to_inches(text) == Decimal(inches)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "abc",
        "N/A",
        "940 mm",
        "940mm",
        "94 cm",
        "36 x 24",
        '39 1/4"+6"',
        "36-37",
        '163/8"',
        '1/3"',
        "3'-14\"",
        "3'-6 1/3\"",
        '-3"',
        "approx 36",
        "about thirty-six inches",
        "1/0",
        "x" * 60,
    ],
)
def test_text_that_is_not_one_sure_value_is_none(text: str) -> None:
    assert to_inches(text) is None


def test_to_inches_takes_only_text() -> None:
    assert to_inches(37) is None  # type: ignore[arg-type]
    assert to_inches(None) is None  # type: ignore[arg-type]


def test_the_same_value_written_three_ways_is_one_number() -> None:
    assert to_inches("3'-1\"") == to_inches('37"') == to_inches("940 [37]") == Decimal(37)


# ---- a synthetic combined sheet ------------------------------------------------------------------

PAGE_HEIGHT = 400


def _stream(body: bytes, extra: bytes = b"") -> bytes:
    compressed = zlib.compress(body)
    return (
        b"<< "
        + extra
        + b" /Filter /FlateDecode /Length "
        + str(len(compressed)).encode()
        + b" >>\nstream\n"
        + compressed
        + b"\nendstream"
    )


def _sheet() -> bytes:
    """Page 1: `3'-1"` in the page's own content at top-left (50, 38); a pasted drawing (a `/Stamp`)
    whose appearance prints `940 [46]` at (320, 238), `2' -5"` at (320, 198) and, in red as GV's
    markup would be, `38"` at (320, 298). Page 2: nothing at all."""
    font = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    page_content = b"BT /F1 12 Tf 50 350 Td (3'-1\") Tj ET"
    stamp_drawing = (
        b"BT /F1 12 Tf 20 100 Td (940 [46]) Tj ET "
        b"BT /F1 12 Tf 20 140 Td (2' -5\") Tj ET "
        b'1 0 0 rg BT /F1 12 Tf 20 40 Td (38") Tj ET'
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 8 0 R] /Count 2 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 400] /Resources << /Font << /F1 4 0 R"
            b" >> >> /Contents 5 0 R /Annots [6 0 R] >>"
        ),
        font,
        _stream(page_content),
        b"<< /Type /Annot /Subtype /Stamp /Rect [300 50 550 250] /AP << /N 7 0 R >> >>",
        _stream(
            stamp_drawing,
            b"/Type /XObject /Subtype /Form /FormType 1 /BBox [0 0 250 200] "
            b"/Resources << /Font << /F1 4 0 R >> >>",
        ),
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 400] /Contents 9 0 R >>",
        _stream(b""),
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(start).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


#: Where each value sits, in PDF points of the page as shown, origin top-left.
OWN_TEXT = {"page": 1, "x0": 40, "y0": 30, "x1": 110, "y1": 60}
STAMP_DUAL = {"page": 1, "x0": 310, "y0": 225, "x1": 420, "y1": 255}
STAMP_FEET = {"page": 1, "x0": 310, "y0": 185, "x1": 420, "y1": 215}
STAMP_RED = {"page": 1, "x0": 310, "y0": 285, "x1": 420, "y1": 315}
EMPTY = {"page": 1, "x0": 150, "y0": 300, "x1": 250, "y1": 380}


@pytest.fixture
def sheet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GV_READER_MISSING_SPACE_HEIGHTS", "0.1")
    path = tmp_path / "set.pdf"
    path.write_bytes(_sheet())
    return path


@pytest.mark.parametrize("value", ["3'-1\"", '37"', "37 in", "37", "940 [37]"])
def test_a_value_in_the_pages_own_text_matches_in_any_printed_form(sheet: Path, value: str) -> None:
    assert printed_at_spot(sheet, 1, OWN_TEXT, value) == "match"


@pytest.mark.parametrize("value", ['46"', "3'-10\"", "940 (46)", "46"])
def test_a_value_inside_a_pasted_stamp_drawing_is_found(sheet: Path, value: str) -> None:
    assert printed_at_spot(sheet, 1, STAMP_DUAL, value) == "match"


def test_feet_and_inches_inside_a_stamp_match_their_inches(sheet: Path) -> None:
    assert printed_at_spot(sheet, 1, STAMP_FEET, '29"') == "match"
    assert printed_at_spot(sheet, 1, STAMP_FEET, "2'-5\"") == "match"


def test_another_value_at_the_spot_is_not_found(sheet: Path) -> None:
    assert printed_at_spot(sheet, 1, OWN_TEXT, '38"') == "not_found"
    assert printed_at_spot(sheet, 1, STAMP_DUAL, '37"') == "not_found"


def test_a_value_printed_elsewhere_on_the_page_does_not_match(sheet: Path) -> None:
    assert printed_at_spot(sheet, 1, STAMP_DUAL, "3'-1\"") == "not_found"


def test_a_box_with_no_words_is_no_text(sheet: Path) -> None:
    assert printed_at_spot(sheet, 1, EMPTY, '37"') == "no_text"
    assert printed_at_spot(sheet, 2, EMPTY, '37"') == "no_text"


def test_coloured_markup_inside_a_stamp_is_never_read_as_printed(sheet: Path) -> None:
    assert printed_at_spot(sheet, 1, STAMP_RED, '38"') == "no_text"


@pytest.mark.parametrize(
    ("page", "box"),
    [
        (3, OWN_TEXT),
        (0, OWN_TEXT),
        (1, {"x0": 40, "y0": 30}),
        (1, {"x0": 110, "y0": 30, "x1": 40, "y1": 60}),
        (1, {"x0": "a", "y0": 30, "x1": 110, "y1": 60}),
    ],
)
def test_a_box_that_cannot_be_placed_is_no_box(sheet: Path, page: int, box: dict[str, Any]) -> None:
    assert printed_at_spot(sheet, page, box, '37"') == "no_box"


def test_a_value_that_is_not_a_number_is_said_so(sheet: Path) -> None:
    assert printed_at_spot(sheet, 1, OWN_TEXT, "missing") == "not_a_number"


def test_a_missing_file_is_no_box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GV_READER_MISSING_SPACE_HEIGHTS", "0.1")
    assert printed_at_spot(tmp_path / "gone.pdf", 1, OWN_TEXT, '37"') == "no_box"


def test_the_reader_setting_must_be_stated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GV_READER_MISSING_SPACE_HEIGHTS", raising=False)
    path = tmp_path / "unstated.pdf"
    path.write_bytes(_sheet())
    with pytest.raises(ValueError, match="GV_READER_MISSING_SPACE_HEIGHTS"):
        printed_at_spot(path, 1, OWN_TEXT, '37"')


# ---- one finding ---------------------------------------------------------------------------------


def _finding(**fields: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "item": "Sink base width",
        "architect": "3'-1\"",
        "vendor": '46"',
        "status": "different",
        "source": "architect",
        "arch_box": {**OWN_TEXT, "file": "set.pdf"},
        "vendor_box": {**STAMP_DUAL, "file": "set.pdf"},
    }
    finding.update(fields)
    return finding


def test_check_finding_checks_both_sides(sheet: Path) -> None:
    checks = check_finding(_finding(), {"set.pdf": sheet})
    assert checks == Checks(printed_architect="match", printed_vendor="match", arithmetic=None)
    assert checks.as_dict() == {
        "printed_architect": "match",
        "printed_vendor": "match",
        "arithmetic": None,
        "units": None,
    }


def test_check_finding_reports_each_side_on_its_own(sheet: Path) -> None:
    checks = check_finding(
        _finding(vendor='45"', arch_box=None), {"set.pdf": sheet, "other.pdf": sheet}
    )
    assert checks.printed_architect == "no_box"
    assert checks.printed_vendor == "not_found"


def test_a_box_without_a_file_means_the_only_upload(sheet: Path) -> None:
    finding = _finding(arch_box=OWN_TEXT, vendor_box=STAMP_DUAL)
    assert check_finding(finding, {"set.pdf": sheet}).printed_architect == "match"
    assert check_finding(finding, {"a.pdf": sheet, "b.pdf": sheet}).printed_architect == "no_box"


def test_a_box_naming_an_unknown_file_is_no_box(sheet: Path) -> None:
    finding = _finding(arch_box={**OWN_TEXT, "file": "nope.pdf"})
    assert check_finding(finding, {"set.pdf": sheet}).printed_architect == "no_box"


def test_a_finding_value_that_is_not_a_number(sheet: Path) -> None:
    checks = check_finding(_finding(architect="not shown", vendor=None), {"set.pdf": sheet})
    assert (checks.printed_architect, checks.printed_vendor) == ("not_a_number", "not_a_number")


def test_vendor_arithmetic_is_added_up_by_code(sheet: Path) -> None:
    finding = _finding(
        source="vendor_arithmetic", parts=['15"', "1'-6\"", '3 1/2"'], printed_total="3'-1\""
    )
    assert check_finding(finding, {"set.pdf": sheet}).arithmetic == "does_not_add_up"
    finding["printed_total"] = '36 1/2"'
    assert check_finding(finding, {"set.pdf": sheet}).arithmetic == "adds_up"


# ---- arithmetic ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("parts", "total", "result", "off_by"),
    [
        (['18"', '18"'], "3'-0\"", "adds_up", "0"),
        (["940 [37]", '23 1/2"'], '60 1/2"', "adds_up", "0"),
        (['18"', '18"', '1 1/2"'], "3'-1\"", "does_not_add_up", "-0.5"),
        (['24"', '12"'], '37"', "does_not_add_up", "1"),
        (['18"', "abc"], '36"', "not_a_number", None),
        (['18"'], "940 mm", "not_a_number", None),
        ([], '36"', "not_a_number", None),
    ],
)
def test_arithmetic(parts: list[str], total: str, result: str, off_by: str | None) -> None:
    assert arithmetic_check(parts, total) == result
    expected = None if off_by is None else Decimal(off_by)
    assert arithmetic_off_by(parts, total) == expected


def test_arithmetic_refuses_a_bare_string_for_parts() -> None:
    assert arithmetic_check('18"', '18"') == "not_a_number"


# ---- trust level ---------------------------------------------------------------------------------


def _checks(**fields: Any) -> dict[str, Any]:
    checks: dict[str, Any] = {
        "printed_architect": "match",
        "printed_vendor": "match",
        "second_read": "agrees",
        "rule": None,
        "arithmetic": None,
        "lead_checked": True,
    }
    checks.update(fields)
    return checks


@pytest.mark.parametrize(
    ("fields", "level"),
    [
        ({}, "confirmed"),
        ({"lead_checked": False}, "confirmed"),
        ({"printed_vendor": "no_text"}, "likely"),
        ({"printed_vendor": "no_text", "lead_checked": False}, "to_check"),
        # A non-AI witness (printed at both spots) and the lead's own check: confirmed.
        ({"second_read": "unsure"}, "confirmed"),
        # A non-AI witness alone: likely.
        ({"second_read": "not_run", "lead_checked": False}, "likely"),
        ({"second_read": "disagrees"}, "to_check"),
        ({"rule": "disagrees"}, "to_check"),
        ({"printed_architect": "not_found", "rule": "agrees"}, "confirmed"),
        (
            {"printed_architect": "no_text", "printed_vendor": "no_text", "rule": "not_run"},
            "likely",
        ),
        (
            {
                "printed_architect": "no_box",
                "printed_vendor": "no_box",
                "second_read": "unsure",
                "lead_checked": True,
            },
            "to_check",
        ),
        ({"printed_architect": "no_box", "arithmetic": "does_not_add_up"}, "confirmed"),
    ],
)
def test_trust_levels(fields: dict[str, Any], level: str) -> None:
    assert trust_level(_checks(**fields)) == level


def test_arithmetic_that_contradicts_the_finding_drops_it_to_check() -> None:
    adds_up = _checks(printed_architect="no_box", arithmetic="adds_up")
    assert trust_level(adds_up, status="different") == "to_check"
    assert trust_level(adds_up, status="same") == "confirmed"
    off = _checks(printed_architect="no_box", arithmetic="does_not_add_up")
    assert trust_level(off, status="same") == "to_check"
    assert trust_level(off, status="different") == "confirmed"


def test_an_empty_checks_dict_is_to_check() -> None:
    assert trust_level({}) == "to_check"
