"""Tags only on the drawing (final QA, 2026-10-16): `LabelLayout.TAGS`.

Labels sat on the drawing's own words (an architect label over a note, a shrunk one over another
note, one over a printed number) and a person's labels stacked over each other and off the sheet.
With `LabelLayout.TAGS` only the short tag (01, A1, R1) goes on the drawing, right next to its
cloud in a spot off the page's words, the other clouds and the tags already placed; the full words
stay in the margin note and on the summary. The default (`AS_BEFORE`) is unchanged: see
`test_redline_old_format_golden.py`.
"""

from __future__ import annotations

from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any

import pdfplumber
import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]

from reports.redline import (
    LabelLayout,
    RedlinePackage,
    RedlinePage,
    ReportMode,
    page_ink,
    render_redline,
)
from storage.local import LocalStore
from tests.reports.test_redline import (
    DOCUMENT,
    HEIGHT,
    REVISION,
    WIDTH,
    _finding,
    _reference,
    _transform,
)
from tests.reports.test_redline_ai_review import _review, _row
from tests.reports.test_redline_qa_fixes import _square
from workflow.redline_outputs import ai_review_input

#: Made-up notes printed on the sheet right where the old labels went (above-left of each cloud).
NOTES = (
    (40.0, 158.0, "COOK TOP NOTE MADE UP"),
    (120.0, 158.0, "FRIDGE NOTE MADE UP"),
)


def _source(rotation: int) -> bytes:
    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=(float(WIDTH), float(HEIGHT)), invariant=1)
    canvas.setFont("Helvetica", 9)
    for x, y, text in NOTES:
        canvas.drawString(x, y, text)
    canvas.showPage()
    canvas.save()
    page = PdfReader(BytesIO(buffer.getvalue())).pages[0]
    if rotation:
        page.rotate(rotation)
    writer = PdfWriter()
    writer.add_page(page)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _package(rotation: int) -> RedlinePackage:
    return RedlinePackage(
        package_revision_id=REVISION,
        source_pdf=_source(rotation),
        pages=(
            RedlinePage(
                document_version_id=DOCUMENT,
                page=0,
                source_index=0,
                transform=_transform(rotation),
            ),
        ),
    )


def _render(tmp_path: Path, rotation: int, layout: LabelLayout) -> bytes:
    # Four differences, two of them at one place, just under the notes.
    review = _review(
        _row(measures="overall width", vendor_box=_square(0.10, 0.30)),
        _row(measures="overall height", vendor_text='33"', vendor_box=_square(0.10, 0.30)),
        _row(measures="outlet centre", vendor_box=_square(0.30, 0.30)),
        _row(measures="filler", vendor_box=_square(0.50, 0.30)),
    )
    architect, _ = ai_review_input(review)
    artifact = render_redline(
        _package(rotation),
        [_finding(refs=(_reference(),))],
        ReportMode.INTERNAL,
        LocalStore(tmp_path),
        architect=architect,
        label_layout=layout,
    )
    with LocalStore(tmp_path).get(artifact.key) as handle:
        return bytes(handle.read())


def _sheet_chars(document: bytes) -> list[dict[str, Any]]:
    """Every character drawn on the visible sheet of the drawing page (not the margin notes)."""
    with pdfplumber.open(BytesIO(document)) as pdf:
        page = pdf.pages[0]
        sheet_width = float(WIDTH if page.rotation in (0, 180) else HEIGHT)
        return [
            dict(char) for char in page.chars if char["x1"] <= sheet_width and char["text"].strip()
        ]


def _overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return bool(
        a["x0"] < b["x1"] - 0.5
        and b["x0"] < a["x1"] - 0.5
        and a["top"] < b["bottom"] - 0.5
        and b["top"] < a["bottom"] - 0.5
    )


# 0 and 90 only: pdfplumber reports a 180/270 page's characters in a frame this check cannot use.
@pytest.mark.parametrize("rotation", [0, 90])
def test_only_tags_go_on_the_drawing_clear_of_its_words_and_of_each_other(
    tmp_path: Path, rotation: int
) -> None:
    chars = _sheet_chars(_render(tmp_path, rotation, LabelLayout.TAGS))
    # By font, not size: pdfplumber gives a turned character the size of its turned box.
    words = [char for char in chars if char["fontname"].endswith("Helvetica")]
    tags = [char for char in chars if char["fontname"].endswith("Helvetica-Bold")]
    assert words, "the sheet's own notes are still there"
    tag_text = "".join(char["text"] for char in tags)
    for tag in ("01", "A1", "A2", "A3", "A4"):
        assert tag in tag_text, f"tag {tag} is on the drawing"
    assert "Needs" not in tag_text and "FAIL" not in tag_text, "the full words stay in the margin"
    on_words = [(t["text"], w["text"]) for t in tags for w in words if _overlap(t, w)]
    assert not on_words, f"tags print over the drawing's words: {on_words[:5]}"
    stacked = [
        (a["text"], b["text"]) for i, a in enumerate(tags) for b in tags[i + 1 :] if _overlap(a, b)
    ]
    assert not stacked, f"tags print over each other: {stacked[:5]}"


def test_the_old_layout_still_writes_the_words_on_the_drawing(tmp_path: Path) -> None:
    chars = _sheet_chars(_render(tmp_path, 0, LabelLayout.AS_BEFORE))
    assert "Needs" in "".join(char["text"] for char in chars)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_the_pages_ink_is_found_where_it_is_drawn(rotation: int) -> None:
    """The page's own drawing (here two notes) is seen where it is, turned with the page; text
    drawn as pen strokes is ink as well, which word boxes would miss."""
    from reports.redline import _sheet, _turn

    package = _package(rotation)
    sheet = _sheet(package.pages[0], rotation)
    ink = page_ink(package.source_pdf, 0, sheet)
    assert ink is not None

    def turned(x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float, float]:
        corners = [
            _turn(Decimal(str(x)), Decimal(str(y)), (360 - rotation) % 360)
            for x in (x0, x1)
            for y in (y0, y1)
        ]
        return (
            float(min(x for x, _ in corners)),
            float(min(y for _, y in corners)),
            float(max(x for x, _ in corners)),
            float(max(y for _, y in corners)),
        )

    x, y, _ = NOTES[0]
    assert ink.cost(turned(x, y, x + 30, y + 7)) > 20, "the note's ink is under its own box"
    assert ink.cost(turned(x, 20, x + 30, 27)) == 0, "an empty part of the sheet has none"
