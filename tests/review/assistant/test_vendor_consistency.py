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
from fractions import Fraction
from itertools import pairwise
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
    # The results screen's proof: how long the wrong part is drawn, and the right overall.
    assert finding["wrong_part"]["drawn"] == '40"' and finding["wrong_part"]["overall"] == '60"'
    assert proof.should_be(finding) == '40" (the overall less the other parts)'


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


# ---- an overall past its sized pieces (DECIDED 2026-10-27) ----------------------------------------


def _sheet_unsized(
    *, left: float = 5, right: float = 5, printed: str = '49"', extra: bytes = b""
) -> bytes:
    """An overall drawn at 2 pt per inch over 20" + 12" + 12" pieces on the next row (15 pt apart),
    the pieces starting `left` points in from the overall's left end and stopping `right` points
    short of its right end (5 pt = 2 1/2" unsized at each end; made-up values)."""
    start = 300 + left
    return (
        _dim(300, start + 88 + right, 300, printed)
        + _dim(start, start + 40, 285, '20"')
        + _dim(start + 40, start + 64, 285, '12"')
        + _dim(start + 64, start + 88, 285, '12"')
        + extra
    )


def test_an_overall_past_unsized_ends_is_a_please_check_never_red(tmp_path: Path) -> None:
    from app.review.assistant import likely_problems, proof
    from app.review.assistant.chat_trust import judged

    result = _check(tmp_path, _sheet_unsized())
    assert result.strings["does_not_add_up"] == 0  # never added up
    assert [u.as_dict()["ends"] for u in result.unsized] == [['2 1/2"', '2 1/2"']]
    (finding,) = result.findings
    assert finding["status"] == "check" and finding["source"] == "vendor_arithmetic"
    words = finding[vc.UNSIZED_ENDS]["words"]
    assert words == (
        'Please check and confirm: the vendor\'s 49" overall runs about 2 1/2" past the sized '
        'pieces (20" + 12" + 12" = 44") at each end; nothing sizes those ends (unsized '
        "fillers or ends). Please check the ends."
    )
    item = judged({**finding, "id": "F1"})
    assert proof.proof_group(item) == "likely_problem"
    assert not proof.proved_wrong(item)
    assert proof.please_check_words(item) == words
    assert proof.proof_of(item)["reason"] == words
    # Turned into a Wrong by a later step, it stays a please-check, never code-proved: code never
    # adds unsized ends up.
    wrong = judged({**finding, "id": "F1", "status": "different"})
    assert likely_problems.unsized_ends(wrong) == words
    assert not proof.proved_wrong(wrong) and not proof.code_proved(wrong)
    assert proof.proof_group(wrong) == "likely_problem"
    assert likely_problems.unsized_ends({**finding, "status": "same"}) is None
    assert "1 overall runs past its sized pieces" in vc.summary_line(result)


def test_ends_within_the_field_cut_are_not_a_please_check(tmp_path: Path) -> None:
    """1" past the pieces at each end (46" over 44"): GV's field cut per wall end explains it."""
    result = _check(tmp_path, _sheet_unsized(left=2, right=2, printed='46"'))
    assert result.unsized == [] and result.findings == []


def test_a_projects_bigger_field_cut_explains_the_ends(tmp_path: Path) -> None:
    dims, page_map, uploads = _pack(tmp_path, [_sheet_unsized()])
    rules = "| Field cut per wall end (`field_cut`) | 3\" | this project's value | CT-WIDTH-001 |"
    result = vc.check_set(dims, page_map, segments=vc.line_work(uploads), rules_md=rules)
    assert result.unsized == [] and result.findings == []


def test_an_end_past_the_pieces_at_one_end_only_is_left_as_before(tmp_path: Path) -> None:
    result = _check(tmp_path, _sheet_unsized(left=0.0, right=10))
    assert result.unsized == [] and result.findings == []


def test_a_size_printed_in_an_end_explains_it(tmp_path: Path) -> None:
    """A filler whose size is printed at the end (no dimension line of its own) sizes it."""
    result = _check(tmp_path, _sheet_unsized(extra=_text(300.5, 288, '2 1/2"', size=3)))
    assert result.unsized == [] and result.findings == []


def test_an_overall_with_its_own_string_has_its_ends_sized(tmp_path: Path) -> None:
    """When another row is the overall's string (its parts end on the overall's lines), that row
    sizes the ends: no please-check."""
    dims, _page_map, uploads = _pack(tmp_path, [_sheet_unsized()])
    raws = dims["pages"][0]["labels"]
    labels = [la for raw in raws if (la := vc._label(raw, FILE, 1, VENDOR)) is not None]
    lines = vc.line_work(uploads)
    assert len(vc.find_unsized_ends(labels, lines)) == 1
    overall = next(la for la in labels if la.text == '49"')
    string = vc.StringCheck(overall, (), "adds_up", Fraction(49), "")
    assert vc.find_unsized_ends(labels, lines, strings=[string]) == []


def test_strokes_drawn_in_an_end_leave_it_as_before(tmp_path: Path) -> None:
    """Text drawn as pen strokes (code does not read it) in the gap: code cannot say it is unsized."""
    points = [(301.0, 290.0), (302.0, 293.0), (303.0, 290.0), (304.0, 293.0), (304.8, 290.0)]
    scribble = b"".join(_line(*a, *b) for a, b in pairwise(points))
    result = _check(tmp_path, _sheet_unsized(extra=scribble))
    assert result.unsized == [] and result.findings == []


def test_a_difference_not_drawn_at_the_ends_is_left_as_before(tmp_path: Path) -> None:
    """The overall prints 51" but its ends show 5" between them: the drawing does not show the
    difference at the ends, so code cannot measure them."""
    result = _check(tmp_path, _sheet_unsized(printed='51"'))
    assert result.unsized == []
    assert all(vc.UNSIZED_ENDS not in f for f in result.findings)


def test_the_book_keeps_the_please_check_mark() -> None:
    from app.review.assistant import likely_problems

    finding = {"status": "check", likely_problems.UNSIZED_ENDS: {"words": "Please check: x."}}
    assert likely_problems.unsized_ends(finding) == "Please check: x."
    assert likely_problems.unsized_ends({"status": "check"}) is None


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
    monkeypatch.setattr(vc, "check_pack", lambda folder, uploads, **_: _result())
    # The lead already reported the vendor's 60" string on page 3: it is not added twice.
    engine = _Engine("full_check", [{"page": 3, "source": "vendor_arithmetic", "vendor": '60"'}])
    vc.add_to_answer(engine)
    assert [(g["page"], by) for g, by in engine.book.added] == [(4, "consistency")]
    assert [f["id"] for f in engine.shown] == ["F1"]
    assert engine.consistency_gaps == [{"page": 9, "why": "missing_round"}]
    assert (engine.consistency_line or "").startswith("Vendor consistency (by code, $0)")


def test_the_hook_runs_only_in_a_full_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vc, "check_pack", lambda folder, uploads, **_: _result())
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


@pytest.mark.usefixtures("read_twice")
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
    monkeypatch.setattr(vc, "check_pack", lambda folder, uploads, **_: result)
    engine = _Engine("full_check", [])
    engine.book = _FullBook([_ledger_string(['20"', '25"'])])
    dropped: list[str] = []
    engine._show_dropped = lambda finding_id, reason: dropped.append(finding_id)  # type: ignore[attr-defined]
    vc.add_to_answer(engine)
    assert dropped == ["F7"] and engine.book.live() == []


# ---- code's please-check of unsized ends replaces the reading's string (2026-10-27) -------------


def _unsized_and_string(
    tmp_path: Path, parts: list[str], **more: Any
) -> tuple[vc.ConsistencyResult, dict[str, Any]]:
    """Code's result for an overall 49" past its sized pieces 20" + 12" + 12" (made-up values),
    and a reading's string of the same overall at its spot (`more` changes it)."""
    result = _check(tmp_path, _sheet_unsized())
    (found,) = result.unsized
    x0, y0, x1, y1 = found.overall.box
    spot = {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "page": 1}
    return result, _ledger_string(parts, '49"', **{"vendor_box": spot, **more})


def test_codes_unsized_ends_please_check_replaces_the_readings_string_of_that_overall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real upload: the reading recorded the overall as a string that does not add up (never
    verified: Not compared), and that hid code's own please-check as a duplicate. Code's item now
    replaces it, a Likely problem, never red."""
    from app.review.assistant import proof
    from app.review.assistant.chat_trust import judged

    result, string = _unsized_and_string(tmp_path, ['20"', '12"', '12"'])
    assert proof.proof_group(judged(string)) == "not_compared"
    assert vc._already(result.findings[0], [string])  # what hid it before
    replaced = vc.judge_unsized([string], result.unsized)
    assert list(replaced) == ["F7"] and "its own please-check replaces" in replaced["F7"]

    monkeypatch.setattr(vc, "check_pack", lambda folder, uploads, **_: result)
    engine = _Engine("full_check", [])
    engine.book = _FullBook([string])
    dropped: list[str] = []
    engine._show_dropped = lambda finding_id, reason: dropped.append(finding_id)  # type: ignore[attr-defined]
    vc.add_to_answer(engine)
    assert dropped == ["F7"]
    ((added, by),) = engine.book.added
    assert by == vc.CREATED_BY and added[vc.UNSIZED_ENDS]
    item = judged({**added, "id": "F8"})
    assert proof.proof_group(item) == "likely_problem" and not proof.proved_wrong(item)


def test_a_string_read_after_codes_please_check_is_in_the_answer_is_replaced_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-28: the late look reads a string of an overall after code's please-check of that
    overall is in the answer. The hook replaces a string only when it adds code's item, so run
    again it keeps the string beside code's item (`_already` sees code's own item): the "hidden"
    set does not hold on a second run. `replaced_by_own` names it, with the hook's reason."""
    result, string = _unsized_and_string(tmp_path, ['20"', '12"', '12"'])
    monkeypatch.setattr(vc, "check_pack", lambda folder, uploads, **_: result)
    engine = _Engine("full_check", [])
    engine.book = _FullBook([])
    vc.add_to_answer(engine)  # the first round: code's please-check, no reading's string yet
    ((given, by),) = engine.book.added
    own = {**given, "id": "F8", "created_by": by}
    engine.book = _FullBook([own, string])
    dropped: list[str] = []
    engine._show_dropped = lambda finding_id, reason: dropped.append(finding_id)  # type: ignore[attr-defined]
    vc.add_to_answer(engine)  # run again: the later string stays beside code's item
    assert dropped == [] and [f["id"] for f in engine.book.live()] == ["F8", "F7"]
    replaced = vc.replaced_by_own([own, string], result)
    assert list(replaced) == ["F7"] and "its own please-check replaces" in replaced["F7"]
    # Without code's own item in the answer nothing is named (the hook adds it first then).
    assert vc.replaced_by_own([string], result) == {}
    # A string code's geometry verified is never replaced.
    assert vc.replaced_by_own([own, {**string, "string_geometry": "verified"}], result) == {}


class _RefusingBook(_FullBook):
    def add_code_finding(self, given: dict[str, Any], *, created_by: str) -> None:  # type: ignore[override]
        return None  # out of scope, or the book is full


def test_the_readings_string_stays_when_codes_item_cannot_be_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, string = _unsized_and_string(tmp_path, ['20"', '12"', '12"'])
    monkeypatch.setattr(vc, "check_pack", lambda folder, uploads, **_: result)
    engine = _Engine("full_check", [])
    engine.book = _RefusingBook([string])
    dropped: list[str] = []
    engine._show_dropped = lambda finding_id, reason: dropped.append(finding_id)  # type: ignore[attr-defined]
    vc.add_to_answer(engine)
    assert dropped == [] and [f["id"] for f in engine.book.live()] == ["F7"]


def test_only_codes_please_check_of_that_overall_looks_past_the_readings_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another code finding the reading's string hid before (the same page and the same two
    values) is still hidden: only the please-check of that overall replaces the string (the
    review of 27 Oct)."""
    result, string = _unsized_and_string(tmp_path, ['20"', '12"', '12"'])
    other = {
        "page": 1,
        "source": "cross_view",
        "status": "different",
        "item": "same span printed twice",
        "architect": string["architect"],
        "vendor": string["vendor"],
    }
    assert vc._already(other, [string])
    result.findings.insert(0, other)
    monkeypatch.setattr(vc, "check_pack", lambda folder, uploads, **_: result)
    engine = _Engine("full_check", [])
    engine.book = _FullBook([string])
    engine._show_dropped = lambda finding_id, reason: None  # type: ignore[attr-defined]
    vc.add_to_answer(engine)
    assert [given.get(vc.UNSIZED_ENDS) is not None for given, _by in engine.book.added] == [True]


def test_a_string_on_another_vendor_file_is_not_replaced(tmp_path: Path) -> None:
    result, string = _unsized_and_string(tmp_path, ['20"', '12"', '12"'])
    elsewhere = {**string, "vendor_box": {**string["vendor_box"], "file": "other vendor.pdf"}}
    assert vc.judge_unsized([elsewhere], result.unsized) == {}
    same_file = {**string, "vendor_box": {**string["vendor_box"], "file": FILE}}
    assert list(vc.judge_unsized([same_file], result.unsized)) == ["F7"]


@pytest.mark.parametrize(
    ("parts", "more"),
    [
        (['20"', '12"', '12"'], {"string_geometry": "verified"}),  # code proved its geometry
        (['20"', '12"', '12"'], {"status": "same"}),  # the reading says it adds up
        (['20"', '12"'], {}),  # other parts: another claim
        (['20"', '12"', '12"'], {"page": 2}),  # another page
        (['20"', '12"', '12"'], {"vendor_box": {"x0": 0, "y0": 0, "x1": 5, "y1": 5}}),  # elsewhere
        (['20"', '12"', '12"'], {"created_by": "consistency"}),  # code's own
    ],
)
def test_any_other_string_is_left_as_it_is(
    tmp_path: Path, parts: list[str], more: dict[str, Any]
) -> None:
    result, string = _unsized_and_string(tmp_path, parts, **more)
    assert vc.judge_unsized([string], result.unsized) == {}


def test_a_string_with_no_spot_is_replaced_only_at_the_one_such_overall(tmp_path: Path) -> None:
    result, string = _unsized_and_string(tmp_path, ['20"', '12"', '12"'])
    del string["vendor_box"]
    assert list(vc.judge_unsized([string], result.unsized)) == ["F7"]
    assert vc.judge_unsized([string], result.unsized * 2) == {}
    assert vc.judge_unsized([string], result.unsized, verified=("F7",)) == {}


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


@pytest.mark.usefixtures("read_twice")
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


# ---- a reading's string is a card only as a whole row code reads (2026-10-31) --------------------


def _sheet_middle_unread() -> bytes:
    """A 60" overall over a row of 10" + 40" + 10" drawn end to end (2 pt per inch), the middle
    40" drawn with its ticks but no printed value code can read (a stacked or hand-lettered
    number): code sees the two 10" ends only. Made-up values."""
    middle = _line(320, 280, 400, 280) + _line(318.5, 278.5, 321.5, 281.5)
    middle += _line(398.5, 278.5, 401.5, 281.5)
    return (
        _dim(300, 420, 300, '60"')
        + _dim(300, 320, 280, '10"')
        + middle
        + _dim(400, 420, 280, '10"')
        + _dim(450, 500, 150, '25"')
    )


def _spot_of(result: vc.ConsistencyResult, value: str) -> dict[str, Any]:
    """The spot of the vendor label printing `value` (a reading's string records it there)."""
    labels = [raw for found in result.page_labels.values() for raw in found]
    (label,) = [raw for raw in labels if raw.get("text") == value]
    x0, y0, x1, y1 = label["box"]
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "page": 1}


def test_a_reading_that_leaves_a_part_of_the_row_out_makes_no_card(tmp_path: Path) -> None:
    """A paid run's false lead, in made-up numbers: the reading typed 10" + 10" for the 60" row
    and left the middle part out ("10" + 10" = 20" ≠ 60""). Code's labels show the two parts
    stop short of each other: the string is dropped with that reason, never a card."""
    result = _check(tmp_path, _sheet_middle_unread())
    assert vc.judge_strings([_ledger_string(['10"', '10"'])], result.checks) == vc.StringJudgement()
    string = _ledger_string(['10"', '10"'], vendor_box=_spot_of(result, '60"'))
    judged = vc.judge_rows([string], result)
    assert judged.complete == () and list(judged.dropped) == ["F7"]
    reason = judged.dropped["F7"]
    assert reason.startswith('The reading\'s sum (10" + 10" against the 60" printed) is not shown')
    assert "stop short" in reason and "one whole row" in reason


def test_the_hook_drops_a_part_row_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _check(tmp_path, _sheet_middle_unread())
    monkeypatch.setattr(vc, "check_pack", lambda folder, uploads, **_: result)
    engine = _Engine("full_check", [])
    engine.book = _FullBook([_ledger_string(['10"', '10"'], vendor_box=_spot_of(result, '60"'))])
    dropped: list[tuple[str, str]] = []
    engine._show_dropped = lambda finding_id, reason: dropped.append((finding_id, reason))  # type: ignore[attr-defined]
    vc.add_to_answer(engine)
    assert [finding_id for finding_id, _reason in dropped] == ["F7"]
    assert "stop short" in dropped[0][1] and engine.book.live() == []


def _raw(
    label_id: str, text: str, inches: str, line: tuple[float, float], across: float
) -> dict[str, Any]:
    """A `dims.json` vendor label on a horizontal dimension line (made-up values)."""
    x0, x1 = line
    middle = (x0 + x1) / 2
    return {
        "id": label_id,
        "text": text,
        "inches": inches,
        "box": [middle - 5, across - 9, middle + 5, across - 1],
        "side": "vendor",
        "orientation": "h",
        "dim_line": [[x0, across], [x1, across]],
        "extension": [[[x0, across + 40], [x0, across]], [[x1, across + 40], [x1, across]]],
        "route": "text",
    }


def _row_result(*labels: dict[str, Any]) -> vc.ConsistencyResult:
    """Code's result with only these labels on page 1 (no strings of its own)."""
    return vc.ConsistencyResult(page_labels={(FILE, 1): list(labels)})


#: A 30" overall at 2 pt per inch over a row 20 7/8" + 9" that fills its line end to end.
_OVERALL = _raw("D1", '30"', "30", (100.0, 160.0), 300.0)
_ROW = (
    _raw("D2", '20 7/8"', "20.875", (100.0, 141.75), 280.0),
    _raw("D3", '9"', "9", (141.75, 160.0), 280.0),
)


def _string_at_overall(parts: list[str], **more: Any) -> dict[str, Any]:
    x0, y0, x1, y1 = _OVERALL["box"]
    spot = {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "page": 1, "file": FILE}
    return _ledger_string(parts, '30"', vendor_box=spot, **more)


def test_a_reading_whose_parts_are_a_whole_row_of_codes_labels_is_kept_as_codes() -> None:
    """The parts are code's own labels of one row that fills the overall's line: the string stays
    (it does not add up: 29 7/8" against 30"), and code's sum of them is a witness."""
    judged = vc.judge_rows([_string_at_overall(['20 7/8"', '9"'])], _row_result(_OVERALL, *_ROW))
    assert judged.complete == ("F7",) and judged.dropped == {}


def test_a_row_code_sees_is_incomplete_makes_no_card() -> None:
    """A part left out of a row code reads (the parts stop short of the overall's end), a part
    no label of code's prints while the run prints another part the list leaves out, parts that
    miss the overall by a whole part or more, and a part that is not a size: each dropped, with
    why, never a please-check."""
    labels = _row_result(_OVERALL, *_ROW)
    short = vc.judge_rows([_string_at_overall(['20 7/8"'])], labels)
    assert "stop short" in short.dropped["F7"] and short.unproved == {}
    unknown = vc.judge_rows([_string_at_overall(['20 7/8"', '9 1/8"'])], labels)
    assert "no row of its own labels printing exactly these parts" in unknown.dropped["F7"]
    assert '(the drawing also prints 9" along that run), so the reading left a part out' in (
        unknown.dropped["F7"]
    )
    assert unknown.unproved == {}
    # 2" + 3/4" against 30": 27 1/4" off, more than any part read (no label of the overall).
    grouped = vc.judge_rows([_string_at_overall(['2"', '3/4"'])], _row_result(*_ROW))
    assert "as much as a whole part or more" in grouped.dropped["F7"]
    assert grouped.unproved == {}
    unread = vc.judge_rows([_string_at_overall(['20 7/8"', "nine"])], labels)
    assert "not one size code reads" in unread.dropped["F7"] and unread.unproved == {}


def test_a_row_code_cannot_see_is_a_please_check_with_codes_reason() -> None:
    """M6 (Anant, 2026-11-01): an overall code has no label of at the spot, or no row of code's
    labels printing the parts (and nothing else printed along the run): code cannot see the row,
    so the string is kept as a please-check with what code could not confirm, never dropped."""
    no_overall = vc.judge_rows([_string_at_overall(['20 7/8"', '9"'])], _row_result(*_ROW))
    assert no_overall.dropped == {} and no_overall.complete == ()
    why = no_overall.unproved["F7"]
    assert why.startswith("Code could not confirm these parts form one string on the drawing")
    assert 'no label of the 30" overall' in why and "please check" in why
    no_row = vc.judge_rows([_string_at_overall(['20 7/8"', '9"'])], _row_result(_OVERALL))
    assert no_row.dropped == {}
    assert "no row of its own labels printing exactly these parts" in no_row.unproved["F7"]


def test_strings_code_judges_another_way_are_left_alone() -> None:
    """Code's own strings, strings it verified, the unsized-ends rule's strings (`keep`), and
    anything not a reading's Wrong string are never dropped here."""
    nothing = _row_result()
    for finding in (
        {**_string_at_overall(['1"']), "created_by": vc.CREATED_BY},
        {**_string_at_overall(['1"']), "status": "check"},
        {**_string_at_overall(['1"']), "source": "cross_view"},
    ):
        assert vc.judge_rows([finding], nothing) == vc.RowJudgement()
    verified = _string_at_overall(['1"'], string_geometry="verified")
    assert vc.judge_rows([verified], nothing).complete == ("F7",)
    assert vc.judge_rows([_string_at_overall(['1"'])], nothing, keep={"F7"}) == vc.RowJudgement()


def test_the_hook_marks_a_whole_rows_parts_as_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kept: its parts are code's (`parts_read: code`) on the finding and its checks, and its
    trust is worked out again, code's sum now one of its witnesses."""
    monkeypatch.setattr(vc, "check_pack", lambda folder, uploads, **_: _row_result(_OVERALL, *_ROW))
    engine = _Engine("full_check", [])
    typed = _string_at_overall(['20 7/8"', '9"'])
    typed["checks"] = {"arithmetic": "does_not_add_up", "parts_read": "ai", "critic": "passed"}
    typed["witnesses"] = []
    engine.book = _FullBook([typed])
    engine._show_dropped = lambda finding_id, reason: None  # type: ignore[attr-defined]
    vc.add_to_answer(engine)
    (kept,) = engine.book.live()
    assert kept["parts_read"] == "code" and kept["checks"]["parts_read"] == "code"
    assert kept["witnesses"] == ["arithmetic"] and kept["trust_level"] == "confirmed"
    assert [f["id"] for f in engine.shown] == ["F7"]


# ---- a string code cannot see as one row is a please-check (M6, Anant 2026-11-01) ---------------


class _LiveBook(_FullBook):
    """The book as the engine's: code's own item is live once added."""

    def add_code_finding(self, given: dict[str, Any], *, created_by: str) -> dict[str, Any]:
        added = super().add_code_finding(given, created_by=created_by)
        self._live.append(added)
        return added


def _hook(
    monkeypatch: pytest.MonkeyPatch, result: vc.ConsistencyResult, *strings: dict[str, Any]
) -> tuple[_Engine, list[tuple[str, str]]]:
    monkeypatch.setattr(vc, "check_pack", lambda folder, uploads, **_: result)
    engine = _Engine("full_check", [])
    engine.book = _LiveBook(list(strings))
    dropped: list[tuple[str, str]] = []
    engine._show_dropped = lambda finding_id, reason: dropped.append((finding_id, reason))  # type: ignore[attr-defined]
    vc.add_to_answer(engine)
    return engine, dropped


def test_the_hook_keeps_a_row_code_cannot_see_as_a_please_check_never_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reading's string whose overall code has no label of (a hand-drawn row): a Likely problem
    (status check, never Wrong, never clouded), code's reason as its why, AI reading only even with
    two AI reads agreeing; the page reviewer's please-check words stay."""
    from app.review.assistant import proof
    from app.review.assistant.chat_trust import cloud_kind

    typed = _string_at_overall(['20 7/8"', '9"'])
    typed["checks"] = {
        "arithmetic": "does_not_add_up",
        "parts_read": "ai",
        "critic": "passed",
        "second_read": "agrees",
    }
    typed["why"] = "The parts add up to 29 7/8 against 30. (page review)"
    typed["whole_look"] = {
        "by": "page_reviewer",
        "proof": "labels",
        "kind": "size",
        "words": "Please check and confirm: Dimension string (elevation): the parts add up to 29 7/8",
    }
    engine, dropped = _hook(monkeypatch, _row_result(*_ROW), typed)
    assert dropped == []
    (kept,) = engine.book.live()
    assert kept["status"] == "check" and kept["source"] == "vendor_arithmetic"
    assert kept["why"].startswith("Code could not confirm these parts form one string")
    assert kept["why"].endswith("The reading: The parts add up to 29 7/8 against 30. (page review)")
    mark = kept["whole_look"]
    assert mark["proof"] == "likely_only" and mark["by"] == "page_reviewer"
    assert mark["why"].startswith("Code could not confirm") and 'the 30" overall' in mark["why"]
    assert mark["words"] == typed["whole_look"]["words"]
    assert kept["trust_level"] == "to_check" and kept["evidence_kind"] == "inferred"
    assert "arithmetic" not in kept["witnesses"]
    assert proof.proof_group(kept) == "likely_problem" and cloud_kind(kept) == "check"
    assert not proof.proved_wrong(kept)
    assert [f["id"] for f in engine.shown] == ["F7"]


def test_a_ledger_string_code_cannot_see_gets_codes_please_check_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _dropped = _hook(
        monkeypatch, _row_result(_OVERALL), _string_at_overall(['20 7/8"', '9"'])
    )
    (kept,) = engine.book.live()
    mark = kept["whole_look"]
    assert mark["by"] == "whole_drawing_look" and mark["kind"] == "size"
    assert mark["words"] == (
        "Please check and confirm: Dimension string (elevation): the parts read here "
        '(20 7/8" + 9" = 29 7/8") do not add up to the 30" printed.'
    )


def test_the_hook_still_drops_strings_code_sees_are_incomplete_or_add_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Code proved the reading wrong: the string it measured adds up (another chain grouped in),
    or the parts' lines stop short of the overall's ends (a part left out). Both stay dropped,
    never a please-check."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    adds_up = _check(tmp_path / "a", _sheet_two_chains())
    engine, dropped = _hook(monkeypatch, adds_up, _ledger_string(['20"', '25"']))
    assert [i for i, _r in dropped] == ["F7"] and "it adds up" in dropped[0][1]
    assert engine.book.live() == []
    short = _check(tmp_path / "b", _sheet_middle_unread())
    string = _ledger_string(['10"', '10"'], vendor_box=_spot_of(short, '60"'))
    engine, dropped = _hook(monkeypatch, short, string)
    assert [i for i, _r in dropped] == ["F7"] and "stop short" in dropped[0][1]
    assert engine.book.live() == []


def test_a_whole_row_string_stays_a_wrong_beside_a_please_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Code's proved string is unchanged (Wrong, its parts code's); the unseeable one on another
    page becomes the please-check."""
    whole = _string_at_overall(['20 7/8"', '9"'])
    other = {**_string_at_overall(['20 7/8"', '9"']), "id": "F8", "page": 2}
    other["vendor_box"] = {**other["vendor_box"], "page": 2}
    engine, dropped = _hook(monkeypatch, _row_result(_OVERALL, *_ROW), whole, other)
    assert dropped == []
    by_id = {f["id"]: f for f in engine.book.live()}
    assert by_id["F7"]["status"] == "different" and by_id["F7"]["parts_read"] == "code"
    assert "whole_look" not in by_id["F7"]
    assert by_id["F8"]["status"] == "check"
    assert by_id["F8"]["whole_look"]["proof"] == "likely_only"


def test_one_please_check_per_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two readings of the same unseeable string (same page, overall and spot): one please-check;
    the other is dropped as its copy. A string already a card is never repeated either."""
    first = _string_at_overall(['20 7/8"', '9"'])
    second = {**_string_at_overall(['20 3/4"', '9"']), "id": "F8"}
    engine, dropped = _hook(monkeypatch, _row_result(*_ROW), first, second)
    assert [f["id"] for f in engine.book.live()] == ["F7"]
    assert dropped == [
        ("F8", "The reading's sum is not shown again: F7 is already a card of the same string.")
    ]
    elsewhere = {**second, "vendor_box": {**second["vendor_box"], "x0": 400.0, "x1": 410.0}}
    engine, dropped = _hook(monkeypatch, _row_result(*_ROW), first, elsewhere)
    assert dropped == [] and {f["status"] for f in engine.book.live()} == {"check"}


def test_codes_own_item_of_the_string_is_added_and_replaces_the_please_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Code's own item of the same overall is never hidden by the reading's string about to be a
    please-check; the please-check is then dropped as its copy."""
    own = {
        "page": 1,
        "source": "vendor_arithmetic",
        "architect": '29 7/8"',
        "vendor": '30"',
        "printed_total": '30"',
        "status": "different",
    }
    result = vc.ConsistencyResult(findings=[own], page_labels={(FILE, 1): list(_ROW)})
    engine, dropped = _hook(monkeypatch, result, _string_at_overall(['20 7/8"', '9"']))
    assert [(g["printed_total"], by) for g, by in engine.book.added] == [('30"', "consistency")]
    assert [f["id"] for f in engine.book.live()] == ["F1"]
    assert dropped == [
        ("F7", "The reading's sum is not shown again: F1 is already a card of the same string.")
    ]
