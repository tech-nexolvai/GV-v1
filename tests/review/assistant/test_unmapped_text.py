"""Numbers printed in text with no letter map, read at their spot by two OCR engines (#1750 item 3).

Every PDF here is made in the test (`_unmapped_fonts`): made-up dimensions shown in a Type3 font
(each character a little pen-stroke drawing named `g1`, `g2`...) and in a Type0 font with no
`ToUnicode` (Bitstream Vera embedded, its character maps hidden from the PDF reader), so the PDF
reader gives `(cid:N)` for every character, over made-up dimension lines on a page whose left part
is the architect's drawing and right part the vendor's. Most tests use scripted OCR engines (they
read a spot shown the right way round as one text); the last ones read with the two local engines
(RapidOCR and Tesseract) and are skipped where either is missing. No model is ever called. The
values are invented.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pdfplumber
import pytest

from app.review.assistant import dim_labels as dl
from app.review.assistant import reading_coverage, reading_routes, unmapped_text
from app.review.assistant.dim_index import label_line
from app.review.assistant.label_origin import ocr_read
from tests.review.assistant import _unmapped_fonts as uf
from tests.review.assistant.test_dim_labels import _dimension, _text

HALVES = [
    {"box": [0, 0, 250, 400], "role": "architect"},
    {"box": [250, 0, 600, 400], "role": "vendor"},
]


class _Reader:
    """A scripted engine: reads every spot it is shown as `text` (the route shows a row of codes
    only the way its text runs, so no reading is ever made the other way round)."""

    def __init__(self, name: str, text: str) -> None:
        self.name = name
        self.text = text

    def __call__(self, _picture: Any, *, single_line: bool) -> list[str]:
        return [self.text]


def _engines(rapid: str, tesseract: str) -> list[_Reader]:
    return [_Reader("RapidOCR", rapid), _Reader("Tesseract", tesseract)]


def _write(tmp_path: Path, body: bytes, name: str = "set.pdf") -> Path:
    return uf.write_pdf(tmp_path / name, _dimension(80, 200, 300) + _text(130, 303, '36"') + body)


def _ocr(path: Path, readers: Any) -> dl.PageLabels:
    first = dl.page_labels(path, 1, HALVES)
    return dl.ocr_pass(path, 1, HALVES, first.labels, first.phrases, budget_s=60, readers=readers)


def _coded(found: dl.PageLabels) -> list[dict[str, Any]]:
    return [label for label in found.labels if label.get(unmapped_text.FIELD)]


# ---- what the PDF reader gives ---------------------------------------------------------------------


def test_both_fonts_give_codes_with_no_letters(tmp_path: Path) -> None:
    body = uf.type3_text('24 1/2"', 330, 153) + uf.type0_text('36"', 340, 253, uf.type0_glyphs())
    path = _write(tmp_path, body)

    with pdfplumber.open(path) as document:
        texts = [str(c["text"]) for c in document.pages[0].chars if c["fontname"] != "Helvetica"]

    assert texts and all(text.startswith("(cid:") for text in texts)
    # The page's reading keeps them apart from its characters, the space code left out (no ink).
    parsed = dl._parse(path, 0)
    assert len(parsed.codes) == len('24 1/2"'.replace(" ", "")) + len('36"')
    assert all(code.turn == 0 for code in parsed.codes)
    assert ["".join(c.text for c in parsed.page.chars)] == ['36"']


def test_a_normal_pdf_has_no_codes_and_reads_as_before(tmp_path: Path) -> None:
    path = _write(tmp_path, _dimension(300, 400, 150) + _text(330, 153, '24 1/2"'))

    found = _ocr(path, _engines('24 1/2"', '24 1/2"'))

    assert dl._parse(path, 0).codes == ()
    assert [(label["text"], label["route"]) for label in found.labels] == [
        ('36"', "text"),
        ('24 1/2"', "text"),
    ]
    assert found.ocr is not None and "codes" not in found.ocr
    assert not _coded(found)


# ---- two agreeing reads ------------------------------------------------------------------------------


def test_two_engines_agreeing_on_a_row_of_codes_make_an_ocr_label(tmp_path: Path) -> None:
    path = _write(tmp_path, _dimension(300, 400, 150) + uf.type3_text('24 1/2"', 330, 153))

    found = _ocr(path, _engines('24 1/2"', '24 1/2"'))

    (label,) = _coded(found)
    assert (label["text"], label["inches"], label["side"], label["orientation"]) == (
        '24 1/2"',
        "24.5",
        "vendor",
        "h",
    )
    # The OCR route's trust class: drawn ink read by OCR, never typed text, no line of its own.
    assert (label["route"], label["origin"], label["dim_line"]) == ("drawn", "ocr", None)
    assert ocr_read(label)
    assert label["id"] == f"D{len(found.labels)}"
    codes = found.ocr["codes"] if found.ocr else {}
    assert (codes["spots"], codes["read"], codes["unconfirmed"]) == (1, 1, [])


def test_a_reading_that_spells_more_or_fewer_characters_than_the_row_is_refused(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, _dimension(300, 400, 150) + uf.type3_text('24 1/2"', 330, 153))

    # Six characters printed; `24"` spells three, `241 1/2"` seven.
    assert not _coded(_ocr(path, _engines('24"', '24"')))
    assert not _coded(_ocr(path, _engines('241 1/2"', '241 1/2"')))
    # An inch mark spelled as two primes is one character.
    assert _coded(_ocr(path, _engines("24 1/2''", '24 1/2"')))


def test_one_engine_alone_is_a_suggestion_never_a_label(tmp_path: Path) -> None:
    path = _write(tmp_path, _dimension(300, 400, 150) + uf.type3_text('24 1/2"', 330, 153))

    found = _ocr(path, _engines('24 1/2"', "-"))

    assert not _coded(found)
    (item,) = found.ocr["codes"]["unconfirmed"] if found.ocr else []
    assert item["text"] == '24 1/2"' and item["why"].startswith(unmapped_text.UNCONFIRMED)


def test_one_code_read_as_two_letters_on_a_page_sends_both_readings_back(
    tmp_path: Path,
) -> None:
    # `24"` and `22"` share the code of 2; engines reading both as `24"` give it the letters 2 and
    # 4: one reading is wrong and code cannot tell which, so neither is a label.
    body = _dimension(300, 400, 150) + uf.type3_text('24"', 340, 153)
    body += _dimension(300, 400, 250) + uf.type3_text('22"', 340, 253)
    path = _write(tmp_path, body)

    found = _ocr(path, _engines('24"', '24"'))

    assert not _coded(found)
    whys = [item["why"] for item in (found.ocr["codes"]["unconfirmed"] if found.ocr else [])]
    assert len(whys) == 2 and all(unmapped_text.TWO_LETTERS in why for why in whys)


def test_the_same_codes_read_the_same_way_twice_are_both_labels(tmp_path: Path) -> None:
    body = _dimension(300, 400, 150) + uf.type3_text('24"', 340, 153)
    body += _dimension(300, 400, 250) + uf.type3_text('24"', 340, 253)
    path = _write(tmp_path, body)

    assert [label["text"] for label in _coded(_ocr(path, _engines('24"', '24"')))] == ['24"'] * 2


# ---- what is never read --------------------------------------------------------------------------


def test_a_row_beside_a_stacked_fraction_is_never_read_alone(tmp_path: Path) -> None:
    # `24` with a small `1` over a small `2` after it, then the inch mark: `24"` alone would be
    # half an inch short.
    body = _dimension(300, 400, 150) + uf.type3_text("24", 330, 153)
    body += uf.type3_text("1", 342, 157, size=4) + uf.type3_text("2", 342, 152, size=4)
    body += uf.type3_text('"', 346, 153)
    path = _write(tmp_path, body)

    found = _ocr(path, _engines('24"', '24"'))

    assert not _coded(found)
    codes = found.ocr["codes"] if found.ocr else {}
    assert codes["spots"] == 0 and codes["not_read_rows"] >= 3


def test_codes_on_the_architects_drawing_are_not_read(tmp_path: Path) -> None:
    path = _write(tmp_path, _dimension(80, 200, 150) + uf.type3_text('24"', 120, 153))

    found = _ocr(path, _engines('24"', '24"'))

    assert not _coded(found)
    assert found.ocr is not None and found.ocr["codes"]["not_read_rows"] == 1


def test_codes_a_label_already_covers_are_not_read_again(tmp_path: Path) -> None:
    body = _dimension(300, 400, 150) + uf.type3_text('24"', 340, 153)
    path = _write(tmp_path, body)
    first = dl.page_labels(path, 1, HALVES)
    covering = {**first.labels[0], "id": "D9", "box": [338.0, 238.0, 360.0, 250.0]}

    found = dl.ocr_pass(
        path, 1, HALVES, [*first.labels, covering], [], budget_s=60, readers=_engines('24"', '24"')
    )

    assert not _coded(found)


# ---- the pack and the reading coverage -----------------------------------------------------------


def test_the_pack_numbers_them_last_and_the_coverage_counts_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _dimension(300, 400, 150) + uf.type3_text('24 1/2"', 330, 153)
    path = _write(tmp_path, body)
    page_map = {"pages": [{"file": "set.pdf", "page": 1, "halves": HALVES}]}
    monkeypatch.setattr(dl, "drawn_readers", lambda: _engines('24 1/2"', '24 1/2"'))

    document = dl.build_dims({"set.pdf": path}, page_map, drawn_budget_s=60)

    (entry,) = document["pages"]
    labels = entry["labels"]
    assert [label["id"] for label in labels] == [f"D{n}" for n in range(1, len(labels) + 1)]
    assert labels[-1][unmapped_text.FIELD] is True and labels[-1]["text"] == '24 1/2"'
    assert entry["ocr"]["codes"]["read"] == 1
    coverage = reading_coverage.build(document, {})
    vendor = coverage["sides"]["vendor"]
    assert vendor["codes"] == {"read": 1, "printed": 1}
    assert coverage["printed_as"]["vendor"] == {"codes": 1}


def test_the_coverage_counts_unconfirmed_numbers_as_not_read() -> None:
    label = {
        "id": "D2",
        "text": '24"',
        "inches": "24",
        "box": [340.0, 240.0, 360.0, 248.0],
        "side": "vendor",
        "route": "drawn",
        "origin": "ocr",
        unmapped_text.FIELD: True,
    }
    unconfirmed = {"text": '18"', "box": [300.0, 100.0, 320.0, 108.0], "side": "vendor"}
    document = {
        "pages": [
            {
                "file": "set.pdf",
                "page": 1,
                "labels": [label],
                "ocr": {"status": "done", "codes": {"unconfirmed": [unconfirmed]}},
            }
        ]
    }

    coverage = reading_coverage.build(document, {("set.pdf", 1): []})

    assert coverage["sides"]["vendor"]["read"] == 1 and coverage["sides"]["vendor"]["printed"] == 2
    assert coverage["not_read_by_reason"]["vendor"] == {"no_letter_map": 1}
    assert reading_coverage.headline(coverage)["vendor_codes_printed"] == 2
    (row,) = [r for r in reading_coverage.sheet_rows(coverage) if r[0] == "1"]
    assert row[2] == "in text with no letter map, not confirmed"
    assert row[5] == "text with no letter map"


# ---- the trust class -------------------------------------------------------------------------------


def test_a_number_read_from_codes_is_one_ocr_reading_never_typed_text() -> None:
    label = {
        "id": "D2",
        "file": "set.pdf",
        "page": 1,
        "text": '24 1/2"',
        "inches": "24.5",
        "route": "drawn",
        "origin": "ocr",
        "side": "vendor",
        unmapped_text.FIELD: True,
    }

    value = reading_routes._label_value(label, "vendor", None)

    # One kind of reading: never enough on its own for a red.
    assert value.routes == (reading_routes.OCR,) and not value.enough
    assert "text with no letter map" in label_line(label)


# ---- real engines (skipped where an engine is missing) ------------------------------------------


def _real() -> list[Any]:
    readers = dl.drawn_readers()
    if {reader.name for reader in readers} != {"RapidOCR", "Tesseract"}:
        pytest.skip("RapidOCR and Tesseract are both needed")
    return readers


def test_real_engines_read_numbers_in_a_type3_and_a_type0_font(tmp_path: Path) -> None:
    readers = _real()
    body = _dimension(300, 400, 150) + uf.type3_text('24 1/2"', 330, 153)
    body += _dimension(300, 420, 250) + uf.type0_text('36 3/4"', 340, 253, uf.type0_glyphs())
    path = _write(tmp_path, body)

    found = _ocr(path, readers)

    assert sorted((lab["text"], Decimal(lab["inches"])) for lab in _coded(found)) == [
        ('24 1/2"', Decimal("24.5")),
        ('36 3/4"', Decimal("36.75")),
    ]


def test_real_engines_read_a_type3_number_written_up_the_page(tmp_path: Path) -> None:
    readers = _real()
    path = _write(tmp_path, uf.type3_text('24 1/2"', 520, 140, size=9, up=True))

    (label,) = _coded(_ocr(path, readers))

    assert (label["text"], label["orientation"]) == ('24 1/2"', "v")
