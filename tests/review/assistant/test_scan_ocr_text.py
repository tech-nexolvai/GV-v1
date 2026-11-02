"""The readers treat a scan's own OCR text as an unverified read, never typed (#1750, item 4), $0.

Synthetic pages (`tests/extraction/test_scan_text.py`): a full-page scanned picture with the
scanner's invisible text `36 1/4"` over it, and the controls (typed text over a scan, a plain
vector page, AutoCAD's hidden SHX text over its strokes on a page with a full-page picture).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import dim_labels, picture_labels, reading_routes
from app.review.assistant import verify_value as vv
from app.review.assistant.label_origin import SCAN, ocr_read
from tests.extraction.test_scan_text import HEIGHT, INVISIBLE, TYPED, page, strokes
from tests.extraction.test_scan_text import SCAN as PICTURE

BOX = {"x0": 297.0, "y0": HEIGHT - 214.0, "x1": 345.0, "y1": HEIGHT - 196.0}


def _file(tmp_path: Path, content: bytes) -> Path:
    path = tmp_path / "sheet.pdf"
    path.write_bytes(page(content))
    vv._PAGE_CACHE.clear()
    return path


def _labels(path: Path) -> list[dict[str, Any]]:
    return [label for label in dim_labels.page_labels(path, 1).labels if label["text"]]


def test_a_scans_ocr_text_is_an_unverified_label_never_typed(tmp_path: Path) -> None:
    sheet = _file(tmp_path, PICTURE + INVISIBLE)
    [label] = _labels(sheet)
    assert label["text"] == '36 1/4"' and label["inches"] == "36.25"
    assert label["route"] == "drawn" and label["origin"] == SCAN and ocr_read(label)
    assert label["dim_line"] is None
    # verify_value never reads it as the drawing's text: no text match, no mismatch.
    found = vv.verify_value(sheet, 1, BOX, '36 1/4"', drawn=False)
    assert found.result != "match" and found.route != "text"
    assert vv.verify_value(sheet, 1, BOX, '35"', drawn=False).result != "mismatch"


@pytest.mark.parametrize(
    "content",
    [PICTURE + TYPED, TYPED, PICTURE + strokes() + INVISIBLE],
    ids=["typed-over-a-scan", "vector-page", "shx-over-strokes-on-a-picture"],
)
def test_typed_text_reads_as_before(tmp_path: Path, content: bytes) -> None:
    sheet = _file(tmp_path, content)
    [label] = _labels(sheet)
    assert label["text"] == '36 1/4"' and label["route"] == "text" and "origin" not in label
    assert vv.verify_value(sheet, 1, BOX, '36 1/4"', drawn=False).result == "match"


def test_code_s_own_picture_reading_replaces_the_scanners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The picture route reads the scanner's spot (its label does not keep the engines away);
    where two engines confirm a value there, their label replaces the scanner's."""
    sheet = _file(tmp_path, PICTURE + INVISIBLE)
    scanner = _labels(sheet)[0]
    seen: list[Any] = []

    def read_page(*_args: Any, **_kwargs: Any) -> picture_labels.PictureRead:
        seen.append(True)
        confirmed = picture_labels.PictureLabel(
            text='36 1/4"',
            inches=Decimal("36.25"),
            box=tuple(scanner["box"]),
            turn=0,
            font=10.0,
            kind="dimension",
            confirmed=True,
            reads=('rapidocr 0: 36 1/4"', 'tesseract 0: 36 1/4"'),
        )
        return picture_labels.PictureRead(
            boxes=[(0, 0, 600, 400)], labels=[confirmed], status="done"
        )

    monkeypatch.setattr(picture_labels, "read_page", read_page)
    [label] = _labels_with_pictures(sheet)
    assert seen and label["origin"] == picture_labels.ORIGIN and label["id"] == "D1"


def _labels_with_pictures(path: Path) -> list[dict[str, Any]]:
    return dim_labels.page_labels(path, 1, picture_budget_s=5.0, readers=[object()]).labels


def test_a_scan_label_is_never_proof_of_a_red() -> None:
    """Millimetres over inches agreeing would make two routes for a picture's label; read from a
    scan's own OCR text the same label names a doubt, so it never feeds a red."""
    label = {
        "id": "D1",
        "page": 1,
        "file": "set.pdf",
        "text": "584 [23]",
        "inches": "23",
        "side": "vendor",
        "route": "drawn",
        "units": "agrees",
        "box": [10, 10, 30, 18],
    }
    picture = reading_routes._label_value({**label, "origin": "picture"}, "vendor", None)
    assert set(picture.routes) == {reading_routes.OCR, reading_routes.UNITS} and not picture.doubts
    scan = reading_routes._label_value({**label, "origin": SCAN}, "vendor", None)
    assert scan.doubts == (reading_routes.SCAN_DOUBT,)
