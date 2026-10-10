"""What a sheet prints about itself: its sheet number, and the references a vendor's sheet prints
(#1166).

Verification for `extraction/architect/sheet_index.py`. Invented sheets only (written byte by byte
like `architect_sheet.py`); no client value.
"""

from __future__ import annotations

import pytest

from extraction.architect.reader import MEASURED_ARCHITECT_SETTINGS
from extraction.architect.sheet_index import read_page_phrases, read_sheet_labels
from extraction.architect.view_matching import find_references
from extraction.reader import UnreadablePdf
from tests.extraction.architect.architect_sheet import _pdf, _stream, architect_sheet

TEXT = MEASURED_ARCHITECT_SETTINGS.text


def _sheet(*lines: tuple[float, float, str, float, bytes]) -> bytes:
    body = b"".join(
        colour + f" BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({text}) Tj ET\n".encode()
        for x, y, text, size, colour in lines
    )
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Contents 4 0 R "
                b"/Resources << /Font << /F1 5 0 R >> >> >>"
            ),
            _stream(body),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
    )


BLACK = b"0 g"
RED = b"1 0 0 rg"


def test_the_sheet_number_printed_after_the_word_sheet_is_read() -> None:
    labels = read_sheet_labels(architect_sheet(), 0, text=TEXT)

    assert labels.sheet_number == "X-101"
    assert "SHEET" in labels.reason


def test_a_number_printed_under_a_sheet_label_is_read() -> None:
    data = _sheet((450, 70, "SHEET NO.", 6, BLACK), (450, 45, "Q-207", 16, BLACK))

    assert read_sheet_labels(data, 0, text=TEXT).sheet_number == "Q-207"


def test_a_lone_number_with_no_sheet_label_is_never_guessed() -> None:
    data = _sheet((450, 45, "Q-207", 16, BLACK), (60, 300, "SYNTHETIC ELEVATION", 12, BLACK))

    labels = read_sheet_labels(data, 0, text=TEXT)
    assert labels.sheet_number is None and "no sheet number" in labels.reason


def test_two_different_sheet_numbers_give_none() -> None:
    data = _sheet((60, 60, "SHEET Q-201", 10, BLACK), (360, 60, "SHEET Q-202", 10, BLACK))

    labels = read_sheet_labels(data, 0, text=TEXT)
    assert labels.sheet_number is None and "2 different" in labels.reason


def test_a_reviewers_coloured_sheet_number_is_never_read() -> None:
    data = _sheet((60, 60, "SHEET Q-201", 10, RED))

    assert read_sheet_labels(data, 0, text=TEXT).sheet_number is None


def test_a_page_beyond_the_file_is_unreadable() -> None:
    with pytest.raises(UnreadablePdf):
        read_sheet_labels(architect_sheet(), 3, text=TEXT)


def test_a_vendors_printed_reference_is_found_and_a_reviewers_is_not() -> None:
    data = _sheet(
        (60, 300, "KITCHEN RUN  REF 3/Q-101", 9, BLACK),
        (60, 200, "SEE 7/Q-909", 9, RED),
        (60, 100, '3/4" PLY BACK', 9, BLACK),
    )

    phrases = read_page_phrases(data, 0, text=TEXT)
    assert find_references(phrases) == ("3/Q101",)
