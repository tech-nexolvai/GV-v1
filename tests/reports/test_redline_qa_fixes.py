"""Cloud labels stay on the page (QA 2026-10-16, problem 5).

* The margin notes beside the drawing: a heading longer than its column (an AI page review
  difference says both numbers and what they measure) wraps onto more lines inside the column,
  never past the page edge; many differences on one page stack and stay inside the page.
* The label on the drawing: when the full words fit inside the sheet and touch no other label they
  are drawn exactly as before; otherwise the label is moved inside the sheet, and when it still
  does not fit, or would print over another label, the drawing says only its number ("A2") and the
  full words stay in the margin note and on the summary.

All values are made up.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from typing import Any

import pdfplumber
import pytest

from reports.redline import ReportMode, render_redline
from storage.local import LocalStore
from tests.reports.test_redline import (
    DOCUMENT,
    HEIGHT,
    WIDTH,
    _finding,
    _package,
    _reference,
)
from tests.reports.test_redline_ai_review import _review, _row
from workflow.redline_outputs import ai_review_input

LONG = "left wall to the centre of the left backsplash outlet above the counter"


def _box(points: Sequence[tuple[str, str]]) -> dict[str, Any]:
    return {
        "document_version_id": str(DOCUMENT),
        "page_number": 1,
        "polygon": [list(point) for point in points],
    }


def _square(x: float, y: float, size: float = 0.06) -> dict[str, Any]:
    return _box(
        (
            (f"{x:.2f}", f"{y:.2f}"),
            (f"{x + size:.2f}", f"{y:.2f}"),
            (f"{x + size:.2f}", f"{y + size:.2f}"),
            (f"{x:.2f}", f"{y + size:.2f}"),
        )
    )


def _render(tmp_path: Path, review: object, rotation: int = 0) -> bytes:
    architect, _agreed = ai_review_input(review)
    artifact = render_redline(
        _package(rotation),
        [_finding(refs=(_reference(page=5),))],
        ReportMode.INTERNAL,
        LocalStore(tmp_path),
        architect=architect,
    )
    with LocalStore(tmp_path).get(artifact.key) as handle:
        return bytes(handle.read())


def _chars(document: bytes) -> tuple[list[dict[str, Any]], tuple[float, float, float, float]]:
    """Every character drawn on the first (drawing) page, and that page's box, in PDF space."""
    with pdfplumber.open(BytesIO(document)) as pdf:
        page = pdf.pages[0]
        return [dict(char) for char in page.chars], (
            0.0,
            0.0,
            float(page.width),
            float(page.height),
        )


def _text(document: bytes) -> str:
    with pdfplumber.open(BytesIO(document)) as pdf:
        return " ".join(" ".join((page.extract_text() or "").split()) for page in pdf.pages)


def _four_differences() -> object:
    return _review(
        *(
            _row(
                architect_text=f"{index}'-0\"",
                architect_inches=str(index * 12),
                measures=f"{LONG} number {index}",
                vendor_text=f"{index * 100} [{index}]",
                vendor_box=_square(0.10 + 0.2 * index, 0.30),
            )
            for index in range(1, 5)
        )
    )


# 0 and 90 only: pdfplumber reports a 180/270 page's characters in a frame this check cannot use
# (the drawing's own text reads as off the page there too).
@pytest.mark.parametrize("rotation", [0, 90])
def test_many_long_margin_notes_stay_inside_the_page(tmp_path: Path, rotation: int) -> None:
    document = _render(tmp_path, _four_differences(), rotation)
    chars, (left, bottom, right, top) = _chars(document)

    outside = [
        char["text"]
        for char in chars
        if char["x0"] < left - 0.5
        or char["x1"] > right + 0.5
        or char["top"] < bottom - 0.5
        or char["bottom"] > top + 0.5
    ]
    assert not outside, f"{len(outside)} character(s) drawn off the page: {''.join(outside)[:80]}"
    text = _text(document)
    for index in range(1, 5):
        assert f"{LONG} number {index}" in text, "every difference keeps its full words"


def test_a_label_at_the_right_edge_is_kept_inside_the_sheet(tmp_path: Path) -> None:
    review = _review(_row(measures=LONG, vendor_box=_square(0.90, 0.30, 0.05)))
    document = _render(tmp_path, review)
    chars, _ = _chars(document)

    straddling = [char["text"] for char in chars if char["x0"] < float(WIDTH) < char["x1"]]
    assert not straddling, "no word on the drawing runs over the sheet's right edge"
    on_sheet = [
        char for char in chars if char["x1"] <= float(WIDTH) and char["top"] < float(HEIGHT)
    ]
    assert any(char["text"] == "A" for char in on_sheet), "the drawing still names the cloud"


def test_a_label_at_the_top_edge_is_kept_inside_the_sheet(tmp_path: Path) -> None:
    review = _review(_row(measures="width", vendor_box=_square(0.10, 0.01, 0.04)))
    chars, _ = _chars(_render(tmp_path, review))

    assert all(char["top"] >= -0.5 for char in chars), "nothing is drawn above the sheet"


def test_two_labels_at_one_place_never_print_over_each_other(tmp_path: Path) -> None:
    same = _square(0.10, 0.30)
    review = _review(
        _row(measures="overall width", vendor_box=same),
        _row(measures="overall height", vendor_text='33"', vendor_box=same),
    )
    document = _render(tmp_path, review)
    chars, _ = _chars(document)
    sheet = [
        char
        for char in chars
        if char["x1"] <= float(WIDTH) and char["text"].strip() and char["size"] < 7.25
    ]

    def overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
        return bool(
            a["x0"] < b["x1"] - 0.5
            and b["x0"] < a["x1"] - 0.5
            and a["top"] < b["bottom"] - 0.5
            and b["top"] < a["bottom"] - 0.5
        )

    clashes = [
        (a["text"], b["text"]) for i, a in enumerate(sheet) for b in sheet[i + 1 :] if overlap(a, b)
    ]
    assert not clashes, f"labels print over each other: {clashes[:5]}"
    text = _text(document)
    # pdfplumber reads the ✗ glyph (ZapfDingbats) as "7".
    assert re.search(r"A1 · \S Needs: 36", text), "the first label keeps its full words"
    assert "(per architect: overall height)" in text, "the second's words are in the margin"
