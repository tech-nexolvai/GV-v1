"""The vendor consistency check (#1584): the vendor's drawings checked against themselves, by code.

Every sheet here is synthetic: a small PDF made in the test with dimensions printed as text over
drawn dimension lines (ticks at their ends, extension lines), read by the real label reader
(`dim_labels`) and the real line-work reader. Nothing calls a model or reads a client drawing.
"""

from __future__ import annotations

import json
import threading
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import dim_labels as dl
from app.review.assistant import vendor_consistency as vc
from app.review.assistant.chat_trust import accuracy
from app.review.assistant.chat_unknowns import unknown_records, work_list_note

FILE = "set.pdf"
#: The whole sheet is the vendor's drawing (round 2), in points of the page as shown.
VENDOR = [{"box": [0, 0, 600, 400], "role": "vendor", "round": 2}]


# ---- a synthetic sheet ---------------------------------------------------------------------------


def _stream(body: bytes) -> bytes:
    data = zlib.compress(body)
    return b"<< /Filter /FlateDecode /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def _pdf(pages: Sequence[bytes]) -> bytes:
    """Pages of 600 x 400 points (font /F1 Helvetica)."""
    count = len(pages)
    kids = " ".join(f"{3 + 2 * n} 0 R" for n in range(count)).encode()
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % count,
    ]
    font = 3 + 2 * count
    for n, content in enumerate(pages):
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 400] /Resources << /Font << /F1 "
            + b"%d 0 R >> >> /Contents %d 0 R >>" % (font, 4 + 2 * n)
        )
        objects.append(_stream(content))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.7\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        start,
    )
    return bytes(out)


def _text(x: float, y: float, text: str, size: float = 8) -> bytes:
    return f"BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({text}) Tj ET ".encode("latin-1")


def _line(x0: float, y0: float, x1: float, y1: float) -> bytes:
    return f"0 0 0 RG 0.5 w {x0} {y0} m {x1} {y1} l S ".encode()


def _dim(x0: float, x1: float, y: float, text: str, *, ticks: bool = True) -> bytes:
    """A horizontal dimension (PDF space): its line, an extension line down at each end, a tick at
    each end, and its value printed over the middle."""
    body = _line(x0, y, x1, y) + _line(x0, y - 90, x0, y + 3) + _line(x1, y - 90, x1, y + 3)
    if ticks:
        body += _line(x0 - 1.5, y - 1.5, x0 + 1.5, y + 1.5)
        body += _line(x1 - 1.5, y - 1.5, x1 + 1.5, y + 1.5)
    return body + _text((x0 + x1) / 2 - 5, y + 2, text)


def _sheet_wrong(*, ticks: bool = True) -> bytes:
    """A string drawn at 2 pt per inch: 20" + 41" under a 60" overall that ends on the same two
    extension lines. The 41" piece is drawn as 40" (the vendor's slip). A 25" dimension elsewhere
    shares the scale."""
    return (
        _dim(300, 420, 300, '60"', ticks=ticks)
        + _dim(300, 340, 280, '20"', ticks=ticks)
        + _dim(340, 420, 280, '41"', ticks=ticks)
        + _dim(450, 500, 150, '25"', ticks=ticks)
    )


def _sheet_adds_up() -> bytes:
    return (
        _dim(300, 420, 300, '60"')
        + _dim(300, 340, 280, '20"')
        + _dim(340, 420, 280, '40"')
        + _dim(450, 500, 150, '25"')
    )


def _sheet_overhang() -> bytes:
    """A 61" top over 20" + 40" cabinets: the top runs 2 pt (1") past the cabinets' end. Not one
    string: a plain adder would call it a slip."""
    return (
        _dim(300, 422, 300, '61"')
        + _dim(300, 340, 280, '20"')
        + _dim(340, 420, 280, '40"')
        + _dim(450, 500, 150, '25"')
    )


def _sheet_twice() -> bytes:
    """The same span (300 to 420, 60" at the scale) printed 60" on one view and 61" on the view
    below it, both lines on the same two extension lines."""
    return (
        _dim(300, 420, 300, '60"')
        + _dim(300, 420, 250, '61"')
        + _dim(300, 340, 120, '20"')
        + _dim(450, 500, 150, '25"')
    )


def _pack(tmp_path: Path, pages: Sequence[bytes]) -> tuple[dict[str, Any], dict[str, Any], Path]:
    """The sheet as an upload, its labels (`dims.json`) and its page map."""
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / FILE).write_bytes(_pdf(pages))
    page_map = {
        "files": [FILE],
        "pages": [
            {
                "file": FILE,
                "page": n,
                "role": "vendor",
                "halves": VENDOR,
                "area": "1. Buffet",
                "area_from_page": None,
            }
            for n in range(1, len(pages) + 1)
        ],
    }
    dims = dl.build_dims({FILE: uploads / FILE}, page_map)
    return dims, page_map, uploads


def _check(tmp_path: Path, *pages: bytes, lines: bool = True) -> vc.ConsistencyResult:
    dims, page_map, uploads = _pack(tmp_path, pages)
    return vc.check_set(dims, page_map, segments=vc.line_work(uploads) if lines else None)


# ---- strings -------------------------------------------------------------------------------------


def test_a_string_whose_part_is_drawn_short_is_a_wrong_at_that_part(tmp_path: Path) -> None:
    """The drawing names the wrong number: the 41" part is drawn 40" and the 60" overall is drawn
    to its value, so the red goes round the 41" and it should be 40" (60" less 20"), never "the
    60" should be 61"" (a real two-file upload had the same pattern)"""
    from app.review.assistant import proof

    result = _check(tmp_path, _sheet_wrong())
    assert result.strings == {
        "found": 1,
        "adds_up": 0,
        "does_not_add_up": 1,
        "doubt": 0,
    }
    (finding,) = result.findings
    assert finding["source"] == "vendor_arithmetic"
    assert finding["status"] == "different"
    assert finding["created_by"] == "consistency"
    assert finding["architect"] == '40"'  # the should-be: the overall less the other part
    assert finding["vendor"] == '41"'
    assert finding["printed_total"] == '60"'
    assert finding["parts"] == ['20"', '41"']
    assert finding["item"] == "Buffet: dimension string (across, round 2)"
    assert finding["dims"]["vendor"]["text"] == '41"'
    assert finding["vendor_box"]["page"] == 1
    assert finding["wrong_part"]["text"] == '41"' and finding["wrong_part"]["should_be"] == "40"
    assert 'the 41" dimension is drawn as 40"' in finding["why"]
    assert 'the 41" part should be 40"' in finding["why"]
    assert finding["sum_text"] == '20" + 41" = 61" ≠ 60" printed (off 1")'
    assert proof.vendor_value(finding) == '41"'
    assert proof.should_be_value(finding) == '40"'


def _sheet_wrong_overall() -> bytes:
    """20" + 41" drawn to their values (2 pt per inch) under an overall printed 60" but drawn 61":
    the overall is the wrong number, and it should be the parts' sum."""
    return (
        _dim(300, 422, 300, '60"')
        + _dim(300, 340, 280, '20"')
        + _dim(340, 422, 280, '41"')
        + _dim(450, 500, 150, '25"')
    )


def test_an_overall_drawn_off_its_value_is_a_wrong_with_the_sum_as_should_be(
    tmp_path: Path,
) -> None:
    result = _check(tmp_path, _sheet_wrong_overall())
    (finding,) = result.findings
    assert finding["vendor"] == finding["printed_total"] == '60"'
    assert finding["architect"] == '61"'  # the parts' sum
    assert "wrong_part" not in finding


def _string_sheet(
    overall: str, parts: Sequence[tuple[float, str]], *, ppi: float, x0: float = 100.0
) -> bytes:
    """A string drawn at `ppi` points per inch: each part `(drawn inches, printed)` end to end
    under the overall, which spans them all; a 10" and a 12" dimension elsewhere share the scale.
    Made-up values."""
    total = sum(drawn for drawn, _text in parts)
    body = _dim(x0, x0 + total * ppi, 300, overall)
    x = x0
    for drawn, text in parts:
        body += _dim(x, x + drawn * ppi, 280, text)
        x += drawn * ppi
    return body + _dim(440, 440 + 10 * ppi, 150, '10"') + _dim(440, 440 + 12 * ppi, 110, '12"')


def _either_red(finding: dict[str, Any], printed: str, summed: str) -> None:
    """A string red as a whole: the drawing does not say which number is wrong, so the red is on
    the printed overall and the should-be names both ways to correct it (GV's rule: exact
    match, flag everything)."""
    from app.review.assistant import proof, proof_words

    assert finding["source"] == "vendor_arithmetic" and finding["status"] == "different"
    assert finding["vendor"] == finding["printed_total"] == printed
    assert finding["architect"] == summed  # the parts' sum
    assert "wrong_part" not in finding
    assert "the drawing does not show which number is wrong" in finding["why"]
    assert "correct the parts or the overall." in finding["why"]
    assert "architect" not in finding["why"].lower()
    assert proof.vendor_value(finding) == printed
    assert proof.should_be_value(finding) == summed
    assert proof_words.either_string(finding)
    assert proof_words.should_text(finding) == f"{summed} overall, or parts adding up to {printed}"
    said = proof_words.sentence(finding)
    assert f"Correct the total to {summed}, or the parts so they add up to {printed}." in said
    assert "architect" not in said.lower()


def test_three_equal_bays_printed_to_the_eighth_are_red_as_a_whole(tmp_path: Path) -> None:
    """172" in three equal bays: each is 57 1/3", printed 57 3/8" (the CAD's 1/8" rounding), so
    the parts print 172 1/8". GV's rule: exact match, flag everything, so it is a Wrong,
    red on the string as a whole: every value is its drawn length rounded, so the drawing cannot
    say which number is wrong."""
    third = 172 / 3
    sheet = _string_sheet('172"', [(third, '57 3/8"')] * 3, ppi=2.0, x0=40.0)
    result = _check(tmp_path, sheet)
    assert result.strings["does_not_add_up"] == 1 and "rounding" not in result.strings
    (finding,) = result.findings
    _either_red(finding, '172"', '172 1/8"')
    assert '(1/8" off)' in finding["why"]


def test_recesses_drawn_just_under_two_inches_are_red_as_a_whole(tmp_path: Path) -> None:
    """2" + 20 1/8" + 2" under 24": the recesses are drawn 1 15/16" (printed 2"), so the parts
    print 24 1/8". 1/8" off: a Wrong on the string as a whole."""
    sheet = _string_sheet('24"', [(1.9375, '2"'), (20.125, '20 1/8"'), (1.9375, '2"')], ppi=8.0)
    result = _check(tmp_path, sheet)
    (finding,) = result.findings
    _either_red(finding, '24"', '24 1/8"')


def test_a_part_drawn_far_from_its_value_stays_red_with_the_part_as_should_be(
    tmp_path: Path,
) -> None:
    """20" = 2 5/8" + 15" + 3": the 15" part is drawn 14 3/8" and the 20" overall is drawn 20":
    a real slip (the parts print 20 5/8"). Red at the 15", should be 14 3/8" (20" less the other
    parts), never "the 20" should be 20 5/8"". Made-up geometry."""
    sheet = _string_sheet('20"', [(2.625, '2 5/8"'), (14.375, '15"'), (3.0, '3"')], ppi=8.0)
    result = _check(tmp_path, sheet)
    (finding,) = result.findings
    assert finding["vendor"] == '15"' and finding["architect"] == '14 3/8"'
    assert finding["printed_total"] == '20"'
    assert finding["dims"]["vendor"]["text"] == '15"'


def test_an_eighth_slip_the_strings_own_lines_show_is_red_at_its_part(tmp_path: Path) -> None:
    """31" = 22 3/4" + 8 1/8" with the 8 1/8" part drawn 8 1/4" (the overall and the other part
    drawn to their values): 1/8" off, but no rounding makes 8 1/4" print 8 1/8", so it is a
    slip, red at that part, should be 8 1/4"."""
    sheet = _string_sheet('31"', [(22.75, '22 3/4"'), (8.25, '8 1/8"')], ppi=8.0)
    result = _check(tmp_path, sheet)
    (finding,) = result.findings
    assert finding["vendor"] == '8 1/8"' and finding["architect"] == '8 1/4"'
    assert finding["wrong_part"]["text"] == '8 1/8"'


def test_the_same_eighth_drawn_as_rounding_is_red_on_the_string(tmp_path: Path) -> None:
    """31" = 22 3/4" + 8 1/8" with the parts drawn 22 13/16" + 8 3/16" (each printed to the
    nearest 1/8"): the CAD's rounding explains the 1/8", but GV counts it as a vendor mistake.
    The drawing cannot say which number is wrong: red on the string, both fixes named."""
    sheet = _string_sheet('31"', [(22.8125, '22 3/4"'), (8.1875, '8 1/8"')], ppi=8.0)
    result = _check(tmp_path, sheet)
    (finding,) = result.findings
    _either_red(finding, '31"', '30 7/8"')
    assert finding["dims"]["vendor"]["text"] == '31"'  # the red round the printed overall


def test_a_string_that_adds_up_gives_nothing(tmp_path: Path) -> None:
    result = _check(tmp_path, _sheet_adds_up())
    assert result.strings["found"] == 1
    assert result.strings["adds_up"] == 1
    assert result.findings == []


def test_an_overhang_is_never_added_up(tmp_path: Path) -> None:
    """The top runs past its cabinets: the parts do not end on the overall's extension line, so
    they are not its string, and no Wrong is made (a plain adder would say 60 vs 61)."""
    result = _check(tmp_path, _sheet_overhang())
    assert result.strings["found"] == 0
    assert result.findings == []


def _sheet_doors(inset: float) -> bytes:
    """A 120" cabinet drawn at 1 pt per inch with two 59 7/8" doors under it, drawn `inset` points
    inside each end of the cabinet (the doors' reveal). A 25" dimension elsewhere shares the
    scale. Made-up values."""
    return (
        _dim(300, 420, 300, '120"')
        + _dim(300 + inset, 360, 280, '59 7/8"')
        + _dim(360, 420 - inset, 280, '59 7/8"')
        + _dim(450, 475, 150, '25"')
        + _dim(450, 490, 120, '40"')
    )


def test_parts_drawn_short_of_the_overall_by_the_difference_are_not_its_string(
    tmp_path: Path,
) -> None:
    """#1866: two doors drawn 1/8" inside each end of their cabinet print 1/4" less than it,
    because they are drawn so; at a small scale that is under `END_PT`, and it was a false red."""
    result = _check(tmp_path, _sheet_doors(0.125))
    assert result.strings["found"] == 1 and result.strings["doubt"] == 1
    assert result.findings == []
    assert "drawn short of the overall's ends" in result.checks[0].reason


def test_parts_drawn_to_the_overall_s_ends_that_do_not_add_up_are_still_a_wrong(
    tmp_path: Path,
) -> None:
    result = _check(tmp_path, _sheet_doors(0.0))
    assert result.strings["does_not_add_up"] == 1
    (finding,) = result.findings
    assert finding["parts"] == ['59 7/8"', '59 7/8"']


def test_lines_with_no_end_marks_are_a_doubt_not_a_finding(tmp_path: Path) -> None:
    result = _check(tmp_path, _sheet_wrong(ticks=False))
    assert result.strings["doubt"] == 1
    assert result.findings == []
    assert "end mark" in result.checks[0].reason


def test_without_the_line_work_nothing_is_reported(tmp_path: Path) -> None:
    result = _check(tmp_path, _sheet_wrong(), lines=False)
    assert result.strings["doubt"] == 1
    assert result.findings == []


def test_two_labels_off_the_scale_is_a_doubt(tmp_path: Path) -> None:
    body = (
        _dim(300, 420, 300, '60"')
        + _dim(300, 340, 280, '30"')  # drawn as 20"
        + _dim(340, 420, 280, '41"')  # drawn as 40"
        + _dim(450, 500, 150, '25"')
        + _dim(450, 510, 120, '30"')
    )
    result = _check(tmp_path, body)
    assert result.strings["doubt"] == 1
    assert result.findings == []


# ---- the same span printed twice -----------------------------------------------------------------


def test_the_same_span_printed_twice_names_the_value_the_drawing_backs(tmp_path: Path) -> None:
    result = _check(tmp_path, _sheet_twice())
    assert result.contradictions == 1
    (finding,) = [f for f in result.findings if f["source"] == "cross_view"]
    assert finding["architect"] == '60"'  # the reference: drawn to its value
    assert finding["vendor"] == '61"'
    assert finding["reference_side"] == "vendor"
    assert finding["status"] == "different"
    assert set(finding["dims"]) == {"architect", "vendor"}
    assert finding["arch_box"]["page"] == finding["vendor_box"]["page"] == 1


def test_a_part_detail_beside_an_elevation_is_never_the_same_span(tmp_path: Path) -> None:
    """A side panel drawn beside the cabinet's elevation, its height line ending at the same two
    heights as the cabinet's overall (by chance), printed smaller than drawn: the panel's own
    extension lines stop at its own outline, so the two are not proved to be one view, and no
    finding comes of it (a part detail is not a projection of the elevation)."""

    def detail(x0: float, x1: float, y: float, text: str) -> bytes:
        # The panel's dimension above its outline: its extension lines go up from the outline
        # (y - 40 .. y + 3), never down across the elevation's line.
        return (
            _line(x0, y, x1, y)
            + _line(x0, y - 40, x0, y + 3)
            + _line(x1, y - 40, x1, y + 3)
            + _line(x0 - 1.5, y - 1.5, x0 + 1.5, y + 1.5)
            + _line(x1 - 1.5, y - 1.5, x1 + 1.5, y + 1.5)
            + _text((x0 + x1) / 2 - 5, y + 2, text)
        )

    body = (
        # The elevation's overall: extension lines come up to it from below (y 120 .. 213).
        _line(300, 210, 420, 210)
        + _line(300, 120, 300, 213)
        + _line(420, 120, 420, 213)
        + _line(298.5, 208.5, 301.5, 211.5)
        + _line(418.5, 208.5, 421.5, 211.5)
        + _text(355, 212, '60"')
        # The panel detail above it, the same two ends, printed smaller than drawn.
        + detail(300, 420, 300, '57"')
        + _dim(300, 340, 120, '20"')
        + _dim(450, 500, 150, '25"')
    )
    result = _check(tmp_path, body)
    assert result.contradictions == 0
    assert [f for f in result.findings if f["source"] == "cross_view"] == []


def test_a_label_beside_the_end_of_a_line_is_not_that_lines_value(tmp_path: Path) -> None:
    """A small dimension's text placed at the end of a long line (as drafters do when it does not
    fit) is not a second value for the long line's span."""
    body = (
        _dim(300, 420, 300, '60"')
        + _line(300, 250, 420, 250)
        + _line(298.5, 248.5, 301.5, 251.5)
        + _line(418.5, 248.5, 421.5, 251.5)
        + _text(302, 252, '1"')
        + _dim(300, 340, 120, '20"')
        + _dim(450, 500, 150, '25"')
    )
    result = _check(tmp_path, body)
    assert result.contradictions == 0
    assert result.findings == []


def _sheet_joint(*, gap: float, ticks: str = "one") -> bytes:
    """A 60" overall over 20" + 39 3/8" (2 pt per inch), the joint between the parts drawn as a
    doubled extension line `gap` points apart (at x 340 and 340 + gap). `ticks`: "one" slash across
    both lines (the drafter's one joint), "two" (one on each line: two places)."""
    y, right = 280, 340 + gap
    body = (
        _dim(300, 420, 300, '60"')
        # The parts' line in two pieces, an extension line down at each of their ends.
        + _line(300, y, 340, y)
        + _line(right, y, 420, y)
        + _line(300, y - 90, 300, y + 3)
        + _line(340, y - 90, 340, y + 3)
        + _line(right, y - 90, right, y + 3)
        + _line(420, y - 90, 420, y + 3)
        + _line(298.5, y - 1.5, 301.5, y + 1.5)
        + _line(418.5, y - 1.5, 421.5, y + 1.5)
        + _text(315, y + 2, '20"')
        + _text(370, y + 2, '39 3/8"')
        + _dim(450, 500, 150, '25"')
    )
    middle = 340 + gap / 2
    if ticks == "one":
        body += _line(middle - 1.5, y - 1.5, middle + 1.5, y + 1.5)
    else:
        body += _line(338.5, y - 1.5, 341.5, y + 1.5)
        body += _line(right - 1.5, y - 1.5, right + 1.5, y + 1.5)
    return body


def test_a_joint_under_one_tick_on_a_doubled_line_is_one_point(tmp_path: Path) -> None:
    """#1629: the parts meet across a 1.2 pt doubled extension line marked by one slash (within
    2.5 line weights of 0.5 pt): the string is the overall's, and its sliver is not dimensioned."""
    result = _check(tmp_path, _sheet_joint(gap=1.2))
    assert result.strings["does_not_add_up"] == 1
    (finding,) = result.findings
    assert finding["parts"] == ['20"', '39 3/8"']
    assert finding["architect"] == '59 3/8"'  # code's sum
    assert finding["vendor"] == '60"'


@pytest.mark.parametrize(
    ("gap", "ticks"),
    [
        (1.2, "two"),  # a tick on each line: two places, never bridged
        (3.0, "one"),  # wider than 2.5 line weights: never bridged
    ],
)
def test_a_wider_or_doubly_marked_joint_is_never_bridged(
    tmp_path: Path, gap: float, ticks: str
) -> None:
    result = _check(tmp_path, _sheet_joint(gap=gap, ticks=ticks))
    assert result.strings["found"] == 0
    assert result.findings == []


# ---- completeness --------------------------------------------------------------------------------


def _entry(
    page: int, area: str | None, *halves: tuple[str, int | None], **more: Any
) -> dict[str, Any]:
    return {
        "file": FILE,
        "page": page,
        "role": "+".join(sorted({role for role, _ in halves})) or "general",
        "halves": [{"box": [0, 0, 10, 10], "role": r, "round": n} for r, n in halves],
        "area": area,
        "area_from_page": None,
        **more,
    }


def test_completeness_lists_areas_on_one_side_and_rounds_left_out() -> None:
    page_map = {
        "pages": [
            _entry(1, None, section="1"),
            _entry(2, "1. Buffet", ("architect", None), ("vendor", 1)),
            _entry(3, "1. Buffet", ("vendor", 2), area_from_page=2),
            _entry(4, "2. Vanity", ("architect", None), ("vendor", 1)),
            _entry(5, "3. Coffee bar", ("architect", None)),
            _entry(6, "4. Island", ("vendor", 1), ("vendor", 2)),
        ]
    }
    gaps = vc.completeness(page_map)
    assert [(g["page"], g["why"]) for g in gaps] == [
        (4, "missing_round"),
        (5, "no_counterpart"),
        (6, "no_counterpart"),
    ]
    assert gaps[0]["what"] == "Vanity: round 2 does not include this area (round 1 did, p.4)"
    assert gaps[1]["what"] == "Coffee bar: architect drawing, no vendor sheet"
    assert gaps[2]["what"] == "Island: vendor sheet, no architect drawing"
    assert all(g["scope"] == "set" for g in gaps)


def test_a_part_with_one_round_misses_no_round() -> None:
    """Rounds are compared within one part of the set (a divider with a bare number starts one)."""
    page_map = {
        "pages": [
            _entry(1, None, section="1"),
            _entry(2, "1. Buffet", ("architect", None), ("vendor", 1), ("vendor", 2)),
            _entry(3, None, section="2"),
            _entry(4, "1. Vanity", ("architect", None), ("vendor", 1)),
        ]
    }
    assert vc.completeness(page_map) == []


def test_the_sheet_index_lists_a_sheet_no_page_shows() -> None:
    texts = {
        (FILE, 1): "COVER\nSHEET INDEX\nSD-1 COVER\nSD-2 PLAN\nSD-3 ELEVATIONS\nSD-4 DETAILS",
        (FILE, 2): "PLAN ... SHEET SD-2",
        (FILE, 3): "ELEVATIONS ... SHEET SD-3",
    }
    gaps, found = vc.sheet_index(texts, {"pages": []})
    assert found["index_pages"] == [1]
    assert found["missing"] == ["SD4"]
    assert [g["why"] for g in gaps] == ["not_in_set"]


def test_no_printed_index_gives_no_gap() -> None:
    gaps, found = vc.sheet_index({(FILE, 1): "PLAN SD-2"}, {"pages": []})
    assert gaps == [] and found["index_pages"] == []


# ---- the grey list, the accuracy and the summary -------------------------------------------------


def test_set_gaps_join_the_not_checked_list_but_not_the_score_or_the_work_list() -> None:
    gaps = [
        {
            "page": 4,
            "what": "Vanity: round 2 does not include this area",
            "why": "missing_round",
            "needs": "the vendor's drawing",
            "scope": "set",
        }
    ]
    records = unknown_records([], None, set_gaps=gaps)
    assert records == [{"id": "U1", **gaps[0]}]
    assert work_list_note(records) is None
    score = accuracy([], [], None, records)
    assert score["unknowns"] == 1


def test_the_summary_line_says_what_was_added_up() -> None:
    result = vc.ConsistencyResult(
        strings={"found": 26, "adds_up": 25, "does_not_add_up": 1, "doubt": 0},
        unknowns=[{"page": 4}],
        vendor_pages_without_labels=[3, 5],
    )
    assert vc.summary_line(result) == (
        "Vendor consistency (by code, $0): 26 dimension strings added up, 1 does not; "
        "1 sheet gap (see Not checked); pages 3, 5 have no readable vendor numbers yet."
    )


def test_codes_own_summary_carries_the_line_before_the_next_step() -> None:
    from app.review.assistant.chat_summary import code_summary

    line = "Vendor consistency (by code, $0): 3 dimension strings added up, all add up."
    summary = code_summary(
        [], coverage=None, page_count=3, products=["countertop"], stopped=None, also=[line]
    )
    lines = summary.splitlines()
    assert lines[lines.index(line) + 1].startswith("Next:")


# ---- the full check's hook -----------------------------------------------------------------------


class _Book:
    def __init__(self, live: list[dict[str, Any]]) -> None:
        self.added: list[tuple[dict[str, Any], str]] = []
        self._live = live

    def live(self) -> list[dict[str, Any]]:
        return list(self._live)

    def add_code_finding(self, given: dict[str, Any], *, created_by: str) -> dict[str, Any]:
        self.added.append((given, created_by))
        return {**given, "id": f"F{len(self.added)}"}


class _Engine:
    def __init__(self, mode: str, live: list[dict[str, Any]]) -> None:
        self.settings = type("S", (), {"mode": mode})()
        workspace = type("W", (), {"set_pack": Path("pack"), "uploads": Path("up")})()
        self.tools = type("T", (), {"workspace": workspace})()
        self.book = _Book(live)
        self.finding_lock = threading.RLock()
        self.shown: list[dict[str, Any]] = []
        self.blocks: list[dict[str, Any]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.consistency_gaps: list[dict[str, Any]] = []
        self.consistency_line: str | None = None

    def _show_finding(self, finding: dict[str, Any]) -> None:
        self.shown.append(finding)

    def _add_block(self, block: dict[str, Any]) -> int:
        self.blocks.append(block)
        return len(self.blocks) - 1

    def emit(self, event: str, data: dict[str, Any]) -> None:
        self.events.append((event, data))


def _result() -> vc.ConsistencyResult:
    string = {
        "page": 3,
        "source": "vendor_arithmetic",
        "architect": '61"',
        "vendor": '60"',
        "printed_total": '60"',
    }
    twice = {"page": 4, "source": "cross_view", "architect": '60"', "vendor": '61"'}
    return vc.ConsistencyResult(
        findings=[string, twice],
        unknowns=[{"page": 9, "why": "missing_round"}],
        strings={"found": 2, "adds_up": 1, "does_not_add_up": 1, "doubt": 0},
    )


def test_the_hook_adds_new_findings_gaps_and_a_summary_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vc, "check_pack", lambda folder, uploads: _result())
    # The lead already reported the vendor's 60" string on page 3: it is not added twice.
    engine = _Engine("full_check", [{"page": 3, "source": "vendor_arithmetic", "vendor": '60"'}])
    vc.add_to_answer(engine)
    assert [(g["page"], by) for g, by in engine.book.added] == [(4, "consistency")]
    assert [f["id"] for f in engine.shown] == ["F1"]
    assert engine.consistency_gaps == [{"page": 9, "why": "missing_round"}]
    assert (engine.consistency_line or "").startswith("Vendor consistency (by code, $0)")


def test_the_hook_runs_only_in_a_full_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vc, "check_pack", lambda folder, uploads: _result())
    engine = _Engine("chat", [])
    assert vc.add_to_answer(engine) is None
    assert engine.book.added == [] and engine.consistency_line is None


def test_check_pack_reads_the_pack_files(tmp_path: Path) -> None:
    dims, page_map, uploads = _pack(tmp_path, [_sheet_wrong()])
    folder = tmp_path / "pack"
    folder.mkdir()
    (folder / "dims.json").write_text(json.dumps(dims))
    (folder / "page_map.json").write_text(json.dumps(page_map))
    result = vc.check_pack(folder, uploads, read_texts=False)
    assert result is not None
    assert [f["source"] for f in result.findings] == ["vendor_arithmetic"]
    assert vc.check_pack(tmp_path / "missing", uploads) is None


# ---- code's witnesses through the real checks ------------------------------------------------------


def test_the_findings_carry_their_code_witnesses_through_the_real_checks(tmp_path: Path) -> None:
    """The stand-alone answer: each Wrong item goes through the finding book and code's checks like
    any finding. The string is backed by code's sum (`arithmetic`), the span printed twice by both
    printed values (`printed`); with no AI reading yet, both are Likely (red on the cloud drawing,
    never made Right by the settler)."""
    from app.review.assistant.consistency_answer import run_check
    from app.review.assistant.sandbox import ChatWorkspace

    workspace = ChatWorkspace.open(tmp_path / "chat")
    sheets = _pdf([_sheet_wrong(), _sheet_twice()])
    (workspace.uploads / FILE).write_bytes(sheets)
    listing = [{"name": FILE, "pages": 2, "kind": "the vendor's shop drawings"}]
    (workspace.root / "uploads.json").write_text(json.dumps(listing), encoding="utf-8")
    page_map = {
        "files": [FILE],
        "pages": [
            {"file": FILE, "page": n, "role": "vendor", "halves": VENDOR, "area": "1. Buffet"}
            for n in (1, 2)
        ],
    }
    dims = dl.build_dims({FILE: workspace.uploads / FILE}, page_map)

    def check(folder: Path | None, uploads: Path) -> vc.ConsistencyResult:
        return vc.check_set(dims, page_map, segments=vc.line_work(uploads))

    made = run_check(workspace, listing, check=check)
    findings = [b["finding"] for b in made.blocks if b["type"] == "finding"]
    by_source = {f["source"]: f for f in findings}
    string, twice = by_source["vendor_arithmetic"], by_source["cross_view"]
    assert string["checks"]["arithmetic"] == "does_not_add_up"
    assert "arithmetic" in string["witnesses"]
    assert string["trust_level"] == "likely"
    assert string["parts_sum"] == "61"
    assert twice["checks"]["printed_architect"] == twice["checks"]["printed_vendor"] == "match"
    assert "printed" in twice["witnesses"] and twice["evidence_kind"] == "observed"
    assert twice["trust_level"] == "likely"
    assert all(f["created_by"] == "consistency" and f["status"] == "different" for f in findings)
    assert made.blocks[0]["by"] == "code"


# ---- strings a reading grouped, against code's geometry (#1639) -----------------------------------


def _sheet_two_chains() -> bytes:
    """A 60" overall over 20" + 40" (it adds up), and a 25" dimension of another chain beside it."""
    return _sheet_adds_up()


def _ledger_string(parts: list[str], printed: str = '60"', **more: Any) -> dict[str, Any]:
    from app.review.assistant.finding_checks import to_inches
    from app.review.assistant.ledger import format_inches

    total = sum(to_inches(p) or 0 for p in parts)
    return {
        "id": "F7",
        "page": 1,
        "status": "different",
        "source": "vendor_arithmetic",
        "created_by": "ledger",
        "item": "Dimension string (elevation)",
        "architect": format_inches(total),
        "vendor": printed,
        "printed_total": printed,
        "parts": parts,
        "checks": {"arithmetic": "agrees"},
        "witnesses": ["arithmetic"],
        "trust_level": "likely",
        **more,
    }


def test_a_string_grouped_from_two_chains_is_dropped_when_codes_string_adds_up(
    tmp_path: Path,
) -> None:
    """A paid run's false red, in made-up numbers: a reading put a part of another chain into a string (20" + 25"
    under the 60" overall, leaving out the 40"), so it "did not add up". Code's own string of that
    overall is 20" + 40" = 60": the reading's claim is dropped, never red."""
    result = _check(tmp_path, _sheet_two_chains())
    grouped = _ledger_string(['20"', '25"'])
    judged = vc.judge_strings([grouped], result.checks)
    assert judged.verified == () and list(judged.dropped) == ["F7"]
    assert "it adds up" in judged.dropped["F7"]


def test_a_grouped_string_code_could_not_measure_is_never_red() -> None:
    """With no string of code's for that overall (no geometry), the reading's sum is Not compared:
    the arithmetic witness alone never clouds it."""
    from app.review.assistant import proof

    grouped = _ledger_string(['7 1/2"', '31 1/2"', '4 1/4"'], '58 1/4"')
    assert vc.judge_strings([grouped], []) == vc.StringJudgement()
    assert proof.proof_group(grouped) == "not_compared"
    found = proof.classified(grouped)
    assert found.label == "Not reviewed: string not verified on the drawing"
    assert found.why.startswith('parts 7 1/2" + 31 1/2" + 4 1/4" = 43 1/4"; printed 58 1/4"')


def test_a_grouped_string_that_is_codes_own_chain_is_verified(tmp_path: Path) -> None:
    from app.review.assistant import proof

    result = _check(tmp_path, _sheet_wrong_overall())
    same = _ledger_string(['41"', '20"'])
    other = {**_ledger_string(['20"', '25"']), "id": "F8"}
    judged = vc.judge_strings([same, other], result.checks)
    assert judged.verified == ("F7",)
    assert "its own finding replaces" in judged.dropped["F8"]
    assert proof.proof_group({**same, "string_geometry": "verified"}) == "clouded"
    assert proof.proof_group(same) == "not_compared"


def test_a_grouped_string_whose_part_the_drawing_names_is_replaced_by_codes_own(
    tmp_path: Path,
) -> None:
    """The reading's red would say "the 60" should be 61""; code's own finding says the 41" part
    should be 40": the reading's is dropped for code's."""
    result = _check(tmp_path, _sheet_wrong())
    judged = vc.judge_strings([_ledger_string(['41"', '20"'])], result.checks)
    assert judged.verified == ()
    assert "its own finding replaces" in judged.dropped["F7"]


def test_a_grouped_string_code_measures_off_by_an_eighth_is_verified(tmp_path: Path) -> None:
    """A reading's string 1/8" off its overall, the same parts code measured: verified (never
    dropped as rounding: GV's rule, exact match)."""
    third = 172 / 3
    result = _check(tmp_path, _string_sheet('172"', [(third, '57 3/8"')] * 3, ppi=2.0, x0=40.0))
    grouped = _ledger_string(['57 3/8"'] * 3, '172"')
    judged = vc.judge_strings([grouped], result.checks)
    assert judged.verified == ("F7",) and judged.dropped == {}


def test_two_overalls_of_one_value_and_no_spot_are_left_alone(tmp_path: Path) -> None:
    result = _check(tmp_path, _sheet_adds_up())
    twice = result.checks * 2
    assert vc.judge_strings([_ledger_string(['20"', '25"'])], twice) == vc.StringJudgement()


class _FullBook(_Book):
    def replace(self, finding: dict[str, Any]) -> bool:
        self._live = [finding if f.get("id") == finding["id"] else f for f in self._live]
        return True

    def absorb(self, finding_id: str) -> bool:
        before = len(self._live)
        self._live = [f for f in self._live if f.get("id") != finding_id]
        return len(self._live) < before


def test_the_hook_drops_a_grouped_string_codes_geometry_refutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _check(tmp_path, _sheet_two_chains())
    monkeypatch.setattr(vc, "check_pack", lambda folder, uploads: result)
    engine = _Engine("full_check", [])
    engine.book = _FullBook([_ledger_string(['20"', '25"'])])
    dropped: list[str] = []
    engine._show_dropped = lambda finding_id, reason: dropped.append(finding_id)  # type: ignore[attr-defined]
    vc.add_to_answer(engine)
    assert dropped == ["F7"] and engine.book.live() == []


def test_the_line_work_is_read_once_per_set_and_kept_in_its_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading a page's line work took about a second a page, half a minute of every full check
    (the pipeline audit, 2026-10-19). It is kept in the set pack, exactly, and the next check of
    the set reads it from there; the pack's warm-up reads every labelled page."""
    from types import SimpleNamespace

    from app.review.assistant import dim_labels
    from app.review.assistant import vendor_consistency as vc

    calls: list[int] = []
    segments = [((1.5, 2.25), (3.125, 4.0)), ((0.1, 0.2), (0.30000000000000004, 0.4))]

    def parse(_pdf: Path, index: int) -> Any:
        calls.append(index)
        return SimpleNamespace(segments=list(segments), weight=0.72)

    monkeypatch.setattr(dim_labels, "_parse", parse)
    pack, uploads = tmp_path / "pack", tmp_path / "uploads"
    pack.mkdir()
    uploads.mkdir()
    first = vc.line_work(uploads, pack)
    assert first("set.pdf", 3) == segments and first.weight("set.pdf", 3) == 0.72
    assert calls == [2]
    again = vc.line_work(uploads, pack)
    assert again("set.pdf", 3) == segments and again.weight("set.pdf", 3) == 0.72
    assert calls == [2]  # read from the pack, exactly
    (pack / "dims.json").write_text(
        json.dumps(
            {
                "files": ["set.pdf"],
                "pages": [
                    {"file": "set.pdf", "page": 3, "labels": [{"id": "D1"}]},
                    {"file": "set.pdf", "page": 4, "labels": [{"id": "D1"}]},
                    {"file": "set.pdf", "page": 5, "labels": []},
                ],
            }
        ),
        encoding="utf-8",
    )
    assert vc.warm_line_work(pack, uploads) == 2
    assert calls == [2, 3]  # page 4 read once; page 3 kept; page 5 has no labels
    # Without a pack (a question, not a full check), nothing is kept.
    assert vc.line_work(uploads)("set.pdf", 6) == segments and calls == [2, 3, 5]


def test_two_readings_of_a_string_an_eighth_off_make_it_red() -> None:
    """Two readings agreeing on a five-part string 1/8" off its overall: code's sum proves it does
    not add up, and GV counts that as a vendor mistake (GV's rule: exact match, flag everything), so it
    is red. Only a miss within the readings' 1/16" is not."""
    from app.review.assistant import proof
    from app.review.assistant.full_check_advocate import compare_strings

    parts = ['5 1/4"', '12 3/8"', '5 1/4"', '3"', '30"']  # 55 7/8" printed 56"
    eighth = _ledger_string(parts, '56"', string_geometry="two_reads")
    assert proof.proof_group(eighth) == "clouded"
    chains = {"chains": [{"parts": parts, "overall": '56"', "direction": "across"}], "note": ""}
    assert compare_strings(parts, '56"', chains)[0] == "proved"
    near = ['5 1/4"', '12 3/8"', '5 1/4"', '3"', '30 1/16"']  # 55 15/16" printed 56"
    chains = {"chains": [{"parts": near, "overall": '56"', "direction": "across"}], "note": ""}
    assert compare_strings(near, '56"', chains)[0] == "within_rounding"
    slip = _ledger_string(['20"', '41"'], '60"', string_geometry="two_reads")
    assert proof.proof_group(slip) == "clouded"
