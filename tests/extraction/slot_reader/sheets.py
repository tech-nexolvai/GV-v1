"""Hand-built drawing sheets for the slot reader's tests (#987). No client drawing, no client value.

The shape is the client's: an empty page and one `/Stamp` annotation whose appearance holds the
drawing (and, nested inside it, a form holding the tick marks), built from literal streams as
`tests/extraction/test_rows.py` builds its page. The numbers are invented.

In appearance space (PDF, y up): a chain at y 600 from x 150 to 350 with ticks at 150, 200, 300 and
350 — three slots, 50, 100 and 50 wide — and its overall at y 585, ticks at 150 and 350.
"""

from __future__ import annotations

from tests.extraction.test_annotations import _pdf, _stamp
from tests.extraction.test_rows import _slash, _stream
from tests.extraction.test_stamp_text import HELVETICA

CHAIN_Y = 600.0
OVERALL_Y = 585.0
TICKS = (150.0, 200.0, 300.0, 350.0)
SLOT_CENTRES = (175.0, 250.0, 325.0)


def text(x: float, y: float, value: str, *, size: int = 6, colour: str = "0 g") -> bytes:
    escaped = value.replace("(", "\\(").replace(")", "\\)")
    return f"{colour} BT /F1 {size} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({escaped}) Tj ET 0 g\n".encode()


def glyphs(x: float, y: float) -> bytes:
    """A label drawn as paths: an upright straight stroke (a `1`), then two small curved shapes.

    The straight stroke is what a curve-only cluster leaves out; the whole run must keep it.
    """
    return (
        f"0.3 w {x:.2f} {y:.2f} m {x:.2f} {y + 5:.2f} l S\n"
        f"{x + 2:.2f} {y:.2f} m {x + 5:.2f} {y:.2f} {x + 5:.2f} {y + 2:.2f} {x + 5:.2f} {y + 5:.2f} c "
        f"{x + 2:.2f} {y + 5:.2f} l h f\n"
        f"{x + 6:.2f} {y:.2f} m {x + 9:.2f} {y:.2f} {x + 9:.2f} {y + 2:.2f} {x + 9:.2f} {y + 5:.2f} c "
        f"{x + 6:.2f} {y + 5:.2f} l h f\n"
    ).encode()


def lines(chain_y: float = CHAIN_Y, overall_y: float = OVERALL_Y) -> bytes:
    return (
        f"0.3 w {TICKS[0]} {chain_y} m {TICKS[-1]} {chain_y} l S\n"
        f"0.3 w {TICKS[0]} {overall_y} m {TICKS[-1]} {overall_y} l S\n"
    ).encode()


def ticks(chain_y: float = CHAIN_Y, overall_y: float = OVERALL_Y) -> bytes:
    return (
        b"".join(_slash(x, chain_y) for x in TICKS)
        + _slash(TICKS[0], overall_y)
        + _slash(TICKS[-1], overall_y)
    )


def text_labels(
    pieces: tuple[str, str, str] = ('12"', '24"', '36"'), overall: str = '72"'
) -> bytes:
    body = b"".join(text(x - 7, CHAIN_Y + 4, value) for x, value in zip(SLOT_CENTRES, pieces))
    return body + text(243, OVERALL_Y + 4, overall)


def glyph_labels() -> bytes:
    body = b"".join(glyphs(x - 5, CHAIN_Y + 3) for x in SLOT_CENTRES)
    return body + glyphs(245, OVERALL_Y + 3)


def sheet(
    drawing: bytes,
    tick_marks: bytes | None = None,
    *,
    chain_y: float = CHAIN_Y,
    overall_y: float = OVERALL_Y,
) -> bytes:
    """One page: an empty content stream, one stamp (5), its appearance (6), the font (7) and the
    nested form its ticks are drawn in (8). The stamp's `/Rect` is [50 50 350 250] on a 400 x 300
    page and its `/BBox` [100 500 400 700]."""
    appearance = _stream(
        lines(chain_y, overall_y) + drawing + b"/Ticks Do\n",
        b"<< /Type /XObject /Subtype /Form /FormType 1 /BBox [100 500 400 700] "
        b"/Matrix [1 0 0 1 -100 -500] /Resources << /Font << /F1 7 0 R >> "
        b"/XObject << /Ticks 8 0 R >> >>",
    )
    nested = _stream(
        ticks(chain_y, overall_y) if tick_marks is None else tick_marks,
        b"<< /Type /XObject /Subtype /Form /FormType 1 /BBox [100 500 400 700]",
    )
    return _pdf(
        annotations=[_stamp(appearance_object=6)],
        extra_objects=[appearance, HELVETICA, nested],
    )


def yellow_box(x: float, y: float, width: float, height: float) -> bytes:
    """The reviewer's yellow box painted over a label: a coloured fill, drawn after the label."""
    return f"1 1 0 rg {x:.2f} {y:.2f} {width:.2f} {height:.2f} re f 0 g\n".encode()
