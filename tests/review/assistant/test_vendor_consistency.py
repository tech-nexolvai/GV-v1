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


def test_a_string_that_does_not_add_up_is_a_wrong_with_its_sum_as_should_be(
    tmp_path: Path,
) -> None:
    result = _check(tmp_path, _sheet_wrong())
    assert result.strings == {"found": 1, "adds_up": 0, "does_not_add_up": 1, "doubt": 0}
    (finding,) = result.findings
    assert finding["source"] == "vendor_arithmetic"
    assert finding["status"] == "different"
    assert finding["created_by"] == "consistency"
    assert finding["architect"] == '61"'  # the should-be: the parts' sum
    assert finding["vendor"] == finding["printed_total"] == '60"'
    assert finding["parts"] == ['20"', '41"']
    assert finding["item"] == "Buffet: dimension string (across, round 2)"
    assert finding["dims"]["vendor"]["text"] == '60"'
    assert finding["vendor_box"]["page"] == 1
    # The drawing names the likely slip: the 41" piece is drawn as 40".
    assert 'the 41" dimension is drawn as 40"' in finding["why"]
    assert finding["sum_text"] == '20" + 41" = 61" ≠ 60" printed (off 1")'


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
    (block,) = engine.blocks
    assert block["by"] == "code" and block["text"].startswith("Vendor consistency (by code, $0)")


def test_the_hook_runs_only_in_a_full_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vc, "check_pack", lambda folder, uploads: _result())
    engine = _Engine("chat", [])
    assert vc.add_to_answer(engine) is None
    assert engine.book.added == [] and engine.blocks == []


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
