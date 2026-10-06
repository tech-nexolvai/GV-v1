"""The rows of a page whose drawing is a pasted stamp, end to end (#980).

Verification for: `extraction/rows.py::countertop_row_candidates`.

The page here has the shape of the client's: an empty content stream and a `/Stamp` annotation
whose appearance holds the drawing — and, inside that appearance, a nested Form XObject holding the
tick marks, because that is the arrangement the pypdfium2 reader stops at (#981) and this lane must
not. Built by hand from literal streams, as `tests/extraction/test_annotations.py` builds its pages.
No client drawing is read.
"""

from __future__ import annotations

import zlib
from decimal import Decimal

import pytest

from extraction.geometry.rows import MEASURED_SETTINGS, LabelKind, TickSource, Tiling
from extraction.reader import UnreadablePdf
from extraction.rows import countertop_row_candidates
from tests.extraction.test_annotations import _pdf, _stamp
from tests.extraction.test_stamp_text import HELVETICA


def _stream(body: bytes, dictionary: bytes) -> bytes:
    compressed = zlib.compress(body)
    return (
        dictionary
        + b" /Filter /FlateDecode /Length "
        + str(len(compressed)).encode()
        + b" >>\nstream\n"
        + compressed
        + b"\nendstream"
    )


def _slash(x: float, y: float) -> bytes:
    return (
        f"{x - 1:.2f} {y - 1:.2f} m {x + 1:.2f} {y + 1:.2f} l {x + 1.4:.2f} {y + 1:.2f} l "
        f"{x - 0.6:.2f} {y - 1:.2f} l h f\n"
    ).encode()


def _text(x: float, y: float, text: str) -> bytes:
    return f"BT /F1 6 Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({text}) Tj ET\n".encode()


#: Appearance space is deliberately not page space: the stamp's `/Rect` is [50 50 350 250] on a
#: 400 x 300 page, its `/BBox` [100 500 400 700], so the drawing is shifted by (-50, -450).
ROW_Y = 600.0
TICKS = (150.0, 200.0, 300.0, 350.0)
#: The row and the labels in the appearance itself; the ticks in a nested form it invokes.
APPEARANCE = (
    b"0.3 w 150 600 m 350 600 l S\n"
    + _text(170, ROW_Y + 4, "12")
    + _text(245, ROW_Y + 4, "24")
    + _text(320, ROW_Y + 4, "36")
    + b"0.3 w 150 585 m 350 585 l S\n"
    + _text(245, 589, "72")
    + b"/Ticks Do\n"
)
NESTED_TICKS = b"".join(_slash(x, ROW_Y) for x in TICKS) + _slash(150, 585) + _slash(350, 585)


def _sheet(drawing: bytes = APPEARANCE, ticks: bytes = NESTED_TICKS) -> bytes:
    """One page: an empty content stream, one stamp (object 5), its appearance (6), the font (7)
    and the nested form the appearance draws its ticks with (8)."""
    appearance = _stream(
        drawing,
        b"<< /Type /XObject /Subtype /Form /FormType 1 /BBox [100 500 400 700] "
        b"/Matrix [1 0 0 1 -100 -500] /Resources << /Font << /F1 7 0 R >> "
        b"/XObject << /Ticks 8 0 R >> >>",
    )
    nested = _stream(
        ticks,
        b"<< /Type /XObject /Subtype /Form /FormType 1 /BBox [100 500 400 700]",
    )
    return _pdf(
        annotations=[_stamp(appearance_object=6)],
        extra_objects=[appearance, HELVETICA, nested],
    )


def test_the_rows_of_a_pasted_drawing_are_found_with_ticks_from_a_nested_form() -> None:
    """Input: the stamped page. Outcome: one candidate row with three slots whose ticks came from
    the nested form, each slot labelled, and its coincident overall labelled `72`."""
    result = countertop_row_candidates(_sheet(), 0, dpi=150, settings=MEASURED_SETTINGS)

    assert result.page_index == 0 and result.drawings == 1
    assert len(result.rows.candidates) == 1
    row = result.rows.candidates[0]
    assert row.tick_source is TickSource.SLASH
    widths = [slot.width_pt.quantize(Decimal("0.01")) for slot in row.slots]
    assert widths == [Decimal(50), Decimal(100), Decimal(50)]
    assert [slot.label.text if slot.label else None for slot in row.slots] == ["12", "24", "36"]
    assert row.overall is not None and row.overall.tiling is Tiling.COINCIDENT
    assert [label.text for label in row.overall.labels] == ["72"]


def test_the_boxes_are_placed_on_the_page_not_in_the_appearance() -> None:
    """The appearance draws at (150..350, 600); on the page that is (100..300) across and 150 up
    from the bottom of a 300 pt page. Stored coordinates say so."""
    result = countertop_row_candidates(_sheet(), 0, dpi=150, settings=MEASURED_SETTINGS)

    row = result.rows.candidates[0]
    assert row.x0.quantize(Decimal("0.1")) == Decimal("100.2")
    assert row.x1.quantize(Decimal("0.1")) == Decimal("300.2")
    assert row.y == Decimal(150)  # pdfplumber's top: 300 - 150
    first = row.slots[0].stored
    assert Decimal("0.24") < first.left < Decimal("0.26")
    assert Decimal("0.44") < first.top < Decimal("0.56")


def test_a_label_cut_by_the_stamps_edge_is_flagged() -> None:
    """Input: a second row drawn 10 pt under the stamp's visible top, its first label running off
    that edge. Outcome: that label touches the drawing's edge; the labels inside do not."""
    top_y = 690.0
    upper = b"0.3 w 150 690 m 350 690 l S\n" + _text(170, 697, "12") + _text(245, top_y + 4, "24")
    upper += _text(320, top_y + 4, "36")
    ticks = NESTED_TICKS + b"".join(_slash(x, top_y) for x in TICKS)
    result = countertop_row_candidates(
        _sheet(APPEARANCE + upper, ticks), 0, dpi=150, settings=MEASURED_SETTINGS
    )

    (row,) = [row for row in result.rows.candidates if row.y < Decimal(100)]
    flags = [slot.label.touches_edge for slot in row.slots if slot.label is not None]
    assert flags == [True, False, False]
    assert row.slots[0].label is not None and row.slots[0].label.kind is LabelKind.TEXT


def test_a_page_beyond_the_document_is_refused_not_empty() -> None:
    with pytest.raises(UnreadablePdf):
        countertop_row_candidates(_sheet(), 3, dpi=150, settings=MEASURED_SETTINGS)


def test_a_page_with_no_stamp_has_no_drawings_and_no_rows() -> None:
    """A page whose line-work is in its own content stream is not the client's shape; the result
    says zero drawings rather than pretending to have looked at one."""
    result = countertop_row_candidates(_pdf(annotations=[]), 0, dpi=150, settings=MEASURED_SETTINGS)

    assert result.drawings == 0
    assert result.rows.rows_found == 0
