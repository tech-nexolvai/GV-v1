"""A typed number's second reading by two OCR engines at its own spot (`typed_ocr`, final plan #2).

Made-up pages and pictures only. The decision rule runs on stand-in engines (each returns what it
is told to read); the last test reads a made-up PDF with the two local engines and is skipped where
either is missing. No model is ever called.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from app.review.assistant import typed_ocr
from tests.review.assistant.test_dim_labels import _pdf, _text


class _Reader:
    """A stand-in engine: reads `says` from any picture."""

    def __init__(self, name: str, says: str) -> None:
        self.name = name
        self.says = says

    def __call__(self, picture: np.ndarray, single_line: bool = True) -> list[str]:
        return [self.says] if self.says else []


def _sheet(tmp_path: Path) -> Path:
    path = tmp_path / "sheet_drawings_only.pdf"
    path.write_bytes(_pdf([_text(100, 300, '36"', size=12) + _text(200, 300, '4 11/16"')]))
    return path


def _label(text: str, inches: str, x: float = 100) -> dict[str, Any]:
    # Page 400 points tall: text at y 300 (PDF) sits at 100 from the top as shown.
    return {
        "id": "D1",
        "file": "sheet.pdf",
        "page": 1,
        "text": text,
        "inches": inches,
        "orientation": "h",
        "box": [x - 1, 89, x + 24, 102],
    }


def _reader(tmp_path: Path, rapid: str, tesseract: str) -> typed_ocr.TypedReader:
    return typed_ocr.TypedReader(
        {"sheet.pdf": _sheet(tmp_path)},
        [_Reader("RapidOCR", rapid), _Reader("Tesseract", tesseract)],
    )


def test_both_engines_reading_the_typed_value_is_a_second_route(tmp_path: Path) -> None:
    assert _reader(tmp_path, '36"', '36"')(_label('36"', "36")) == (typed_ocr.AGREES, None)
    # RapidOCR drops the inch mark: its digits still count.
    assert _reader(tmp_path, "36", '36"')(_label('36"', "36")) == (typed_ocr.AGREES, None)


def test_one_engine_alone_or_engines_that_differ_read_nothing(tmp_path: Path) -> None:
    assert _reader(tmp_path, '36"', "")(_label('36"', "36")) is None
    assert _reader(tmp_path, '36"', '38"')(_label('36"', "36")) is None
    assert _reader(tmp_path, '39"', '38"')(_label('36"', "36")) is None


def test_another_whole_number_of_the_same_shape_is_a_doubt(tmp_path: Path) -> None:
    assert _reader(tmp_path, '38"', '38"')(_label('36"', "36")) == (typed_ocr.DISAGREES, '38"')


def test_a_dropped_fraction_or_foot_mark_is_never_a_doubt(tmp_path: Path) -> None:
    assert _reader(tmp_path, '4"', '4"')(_label('4 11/16"', "4.6875", x=200)) is None
    assert _reader(tmp_path, '24"', '24"')(_label("2'-4\"", "28")) is None


def test_a_stacked_fraction_is_split_into_its_three_parts() -> None:
    picture = np.full((60, 110), 255, np.uint8)
    cv2.putText(picture, "97", (4, 44), cv2.FONT_HERSHEY_SIMPLEX, 1.2, 0, 3)
    cv2.putText(picture, "1", (66, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, 0, 2)
    cv2.line(picture, (62, 30), (84, 30), 0, 2)
    cv2.putText(picture, "2", (66, 52), cv2.FONT_HERSHEY_SIMPLEX, 0.6, 0, 2)
    parts = typed_ocr._stacked(picture)
    assert parts is not None
    whole, numerator, denominator = parts
    assert whole.shape[1] > 2 * numerator.shape[1]  # "97" is wider than its "1"
    assert numerator.shape[0] < whole.shape[0] and denominator.shape[0] < whole.shape[0]
    plain = np.full((60, 110), 255, np.uint8)
    cv2.putText(plain, "36", (4, 44), cv2.FONT_HERSHEY_SIMPLEX, 1.2, 0, 3)
    assert typed_ocr._stacked(plain) is None


def test_no_drawings_only_copy_means_no_reader(tmp_path: Path) -> None:
    assert typed_ocr.reader_for_pack(tmp_path, ["sheet.pdf"]) is None
    _sheet(tmp_path)
    assert typed_ocr.reader_for_pack(tmp_path, ["sheet.pdf"]) is not None


def test_the_local_engines_read_a_typed_number_at_its_spot(tmp_path: Path) -> None:
    dim_labels = pytest.importorskip("app.review.assistant.dim_labels")
    engines = dim_labels.drawn_readers()
    if len({reader.name for reader in engines}) < 2:
        pytest.skip("RapidOCR and Tesseract are not both installed")
    reader = typed_ocr.TypedReader({"sheet.pdf": _sheet(tmp_path)}, engines)
    assert reader(_label('36"', "36")) == (typed_ocr.AGREES, None)
