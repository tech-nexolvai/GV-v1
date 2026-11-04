"""One AutoCAD SHX-text detector for both readers, and AutoCAD's control codes (rebuild plan 2A).

Verification for: `extraction.annotations.is_autocad_shx_text`, `decode_autocad_codes`, and the two
readers that use the detector: the annotation reader (`read_markup_layer`, through `_layer_of`) and
the label reader's comments (`visible_text.is_shx_comment`, `shx_comment_text`).

**Why one detector.** Before, the annotation reader knew AutoCAD's note only as a `/Square`, and the
label reader knew it by an exact, case-sensitive `/T`. So the same note held as a `/FreeText` was the
vendor's text to one reader and the reviewer's markup to the other: the vendor's text with GV's
authority. Every row of the table below is read by both, and both must agree.

The PDFs are written by hand, one annotation each, so each says exactly what is in it. No client
file and no client value is used; every title and number is invented.
"""

from __future__ import annotations

import io

import pdfplumber
import pikepdf
import pytest

from app.evidence.record import UNPARSED_FLAG, _parse
from extraction.annotations import (
    DrawingLayer,
    decode_autocad_codes,
    is_autocad_shx_text,
    read_markup_layer,
)
from extraction.gv_marks import LAYER_NAME
from extraction.visible_text import is_shx_comment, shx_comment_text
from tests.extraction.test_annotations import DOCUMENT, DPI, KNOWN_AUTHOR, _pdf

#: The object number the extra objects start at: four page objects and the one annotation.
_FIRST_EXTRA = 6

#: An ordinary layer of the vendor's (an invented name): only GV's own layer name excludes a note.
_VENDOR_LAYER = b"<< /Type /OCG /Name (A-DIMS) >>"
_GV_LAYER = b"<< /Type /OCG /Name (" + LAYER_NAME.encode("latin-1") + b") >>"
#: An appearance GV signed (`gv_marks.sign`): `/PieceInfo /GVReview`.
_GV_SIGNED_APPEARANCE = (
    b"<< /Type /XObject /Subtype /Form /BBox [0 0 80 20] "
    b"/PieceInfo << /GVReview << /LastModified (D:20260101000000Z) >> >> /Length 0 >>"
    b"\nstream\n\nendstream"
)


def _annotation(subtype: bytes, extra: bytes = b"", contents: bytes = b"40") -> bytes:
    return (
        b"<< /Type /Annot /Subtype /"
        + subtype
        + b" /Rect [40 40 120 60] /Contents ("
        + contents
        + b") "
        + extra
        + b" >>"
    )


def _titled(title: bytes, key: bytes = b"T") -> bytes:
    return b"/" + key + b" (" + title + b")"


_SHX = _titled(b"AutoCAD SHX Text")
_REVIEWER = _titled(KNOWN_AUTHOR.encode("latin-1"))
_UTF16_SHX = b"/T <" + ("﻿AutoCAD SHX Text").encode("utf-16-be").hex().encode() + b">"

#: `(id, annotation, extra objects, the layer it must be)`.
ROWS: list[tuple[str, bytes, list[bytes], DrawingLayer]] = [
    ("square-titled", _annotation(b"Square", _SHX), [], DrawingLayer.VENDOR_TEXT),
    ("free-text-titled", _annotation(b"FreeText", _SHX), [], DrawingLayer.VENDOR_TEXT),
    ("sticky-note-titled", _annotation(b"Text", _SHX), [], DrawingLayer.VENDOR_TEXT),
    (
        "square-subject-only",
        _annotation(b"Square", _titled(b"AutoCAD SHX Text", b"Subj")),
        [],
        DrawingLayer.VENDOR_TEXT,
    ),
    (
        "free-text-subject-only",
        _annotation(b"FreeText", _titled(b"AutoCAD SHX Text", b"Subj") + b" " + _REVIEWER),
        [],
        DrawingLayer.VENDOR_TEXT,
    ),
    (
        "lower-case-surrounding-spaces",
        _annotation(b"Square", _titled(b"  autocad shx text  ")),
        [],
        DrawingLayer.VENDOR_TEXT,
    ),
    (
        "upper-case",
        _annotation(b"FreeText", _titled(b"AUTOCAD SHX TEXT")),
        [],
        DrawingLayer.VENDOR_TEXT,
    ),
    ("utf-16-title", _annotation(b"Square", _UTF16_SHX), [], DrawingLayer.VENDOR_TEXT),
    (
        "vendor-layer-free-text-titled",
        _annotation(b"FreeText", _SHX + b" /OC 6 0 R"),
        [_VENDOR_LAYER],
        DrawingLayer.VENDOR_TEXT,
    ),
    (
        "no-spaces-is-not-the-title",
        _annotation(b"Square", _titled(b"AutoCADSHXText")),
        [],
        DrawingLayer.OTHER,
    ),
    (
        "a-longer-title-is-not-the-title",
        _annotation(b"FreeText", _titled(b"AutoCAD SHX Text 2")),
        [],
        DrawingLayer.REVIEWER_MARKUP,
    ),
    ("reviewer-free-text", _annotation(b"FreeText", _REVIEWER), [], DrawingLayer.REVIEWER_MARKUP),
    ("reviewer-square", _annotation(b"Square", _REVIEWER), [], DrawingLayer.OTHER),
    (
        "gv-free-text",
        _annotation(b"FreeText", _REVIEWER + b" /OC 6 0 R"),
        [_GV_LAYER],
        DrawingLayer.REVIEWER_MARKUP,
    ),
    (
        "gv-layer-free-text-titled",
        _annotation(b"FreeText", _SHX + b" /OC 6 0 R"),
        [_GV_LAYER],
        DrawingLayer.REVIEWER_MARKUP,
    ),
    (
        "gv-layer-membership-square-titled",
        _annotation(b"Square", _SHX + b" /OC << /Type /OCMD /OCGs [7 0 R 6 0 R] >>"),
        [_GV_LAYER, _VENDOR_LAYER],
        DrawingLayer.OTHER,
    ),
    (
        "gv-signed-free-text-titled",
        _annotation(b"FreeText", _SHX + b" /AP << /N 6 0 R >>"),
        [_GV_SIGNED_APPEARANCE],
        DrawingLayer.REVIEWER_MARKUP,
    ),
    (
        "reviewer-measurement-line",
        _annotation(b"Line", _REVIEWER + b" /L [50 50 110 50] /IT /LineDimension", b'5"'),
        [],
        DrawingLayer.REVIEWER_MARKUP,
    ),
    (
        "measurement-line-titled",
        _annotation(b"Line", _SHX + b" /L [50 50 110 50] /IT /LineDimension", b'5"'),
        [],
        DrawingLayer.REVIEWER_MARKUP,
    ),
    (
        "measurement-free-text-titled",
        _annotation(b"FreeText", _SHX + b" /IT /LineDimension"),
        [],
        DrawingLayer.REVIEWER_MARKUP,
    ),
    ("circle-titled", _annotation(b"Circle", _SHX), [], DrawingLayer.OTHER),
]


def _layer_read(data: bytes) -> DrawingLayer:
    """Which list the annotation reader put the one annotation in."""
    layers = read_markup_layer(data, 0, document_version_id=DOCUMENT, dpi=DPI)
    found = [
        (DrawingLayer.REVIEWER_MARKUP, layers.markup),
        (DrawingLayer.VENDOR_TEXT, layers.vendor_text),
        (DrawingLayer.OTHER, layers.other_layer_notes),
    ]
    (layer,) = [layer for layer, notes in found for _ in notes]
    return layer


def test_the_rows_reference_the_objects_they_carry() -> None:
    """The table's own premise: an annotation's `6 0 R` is its first extra object."""
    assert _FIRST_EXTRA == 6
    assert all(b"6 0 R" in annotation for _, annotation, extra, _ in ROWS if extra)


@pytest.mark.parametrize(
    ("annotation", "extra", "layer"), [row[1:] for row in ROWS], ids=[row[0] for row in ROWS]
)
def test_both_readers_agree_on_every_annotation(
    annotation: bytes, extra: list[bytes], layer: DrawingLayer
) -> None:
    """**The hard stop.** Outcome: each annotation lands in the layer the row names, and the label
    reader's test (`is_shx_comment`, on pikepdf's dictionary) and the detector on pdfplumber's
    dictionary say "AutoCAD's text" exactly when the annotation reader says `VENDOR_TEXT`."""
    data = _pdf(annotations=[annotation], extra_objects=extra)
    expected = layer is DrawingLayer.VENDOR_TEXT

    assert _layer_read(data) is layer
    with pdfplumber.open(io.BytesIO(data)) as plumbed:
        (plumbed_annotation,) = [found["data"] for found in plumbed.pages[0].annots]
    assert is_autocad_shx_text(plumbed_annotation) is expected
    with pikepdf.open(io.BytesIO(data)) as pdf:
        (pike_annotation,) = list(pdf.pages[0].obj["/Annots"])
        assert is_autocad_shx_text(pike_annotation) is expected
        assert is_shx_comment(pike_annotation) is expected


def test_a_comment_without_a_rectangle_is_not_set_as_text() -> None:
    """Outcome: the label reader needs a rectangle to set the text in; the detector does not."""
    note = pikepdf.Dictionary(
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Square, T=pikepdf.String("AutoCAD SHX Text")
    )
    assert is_autocad_shx_text(note) and not is_shx_comment(note)


def test_anything_that_is_not_a_dictionary_is_not_a_note() -> None:
    assert not is_autocad_shx_text(None)
    assert not is_autocad_shx_text("AutoCAD SHX Text")
    assert not is_autocad_shx_text(pikepdf.Name.Square)


# ---------------------------------------------------------------------------
# AutoCAD's control codes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "decoded"),
    [
        ("%%c12", "⌀12"),
        ("%%C12", "⌀12"),
        ("90%%d", "90°"),
        ("90%%D", "90°"),
        ('36"%%p', '36"±'),
        ('%%P1/8"', '±1/8"'),
        ("50%%%", "50%"),
        ("%%uNOTE%%u", "NOTE"),
        ("%%Oover%%o", "over"),
        ("%%%c", "%c"),
        ("%%x and 5%", "%%x and 5%"),
        ('24 1/2"', '24 1/2"'),
    ],
)
def test_autocad_control_codes_are_the_characters_it_draws(raw: str, decoded: str) -> None:
    assert decode_autocad_codes(raw) == decoded


def test_the_vendors_cad_text_is_decoded_and_a_reviewers_note_is_not() -> None:
    """Outcome: the CAD-text route's note reads as AutoCAD draws it; a reviewer's note keeps the
    file's characters, because the codes are AutoCAD's and the reviewer's tool is not AutoCAD."""
    data = _pdf(
        annotations=[
            _annotation(b"FreeText", _SHX, b'%%c2" 90%%d'),
            b"<< /Type /Annot /Subtype /FreeText /Rect [140 40 200 60] /Contents (90%%d) "
            + _REVIEWER
            + b" >>",
        ]
    )
    layers = read_markup_layer(data, 0, document_version_id=DOCUMENT, dpi=DPI)

    assert [note.text for note in layers.vendor_text] == ['⌀2" 90°']
    assert [note.text for note in layers.markup] == ["90%%d"]


def test_a_comments_symbols_come_back_from_its_text_as_themselves() -> None:
    """Outcome: the comment's string, set as invisible text (`shx_comment_text`), reads back with
    `⌀`, `°` and `±` (the font's own encoding names them), a foot mark as a straight `'`, and its
    codes decoded."""
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(400, 300))
    page = pdf.pages[0]
    comment = pikepdf.Dictionary(
        Type=pikepdf.Name.Annot,
        Subtype=pikepdf.Name.Square,
        Rect=[100, 100, 200, 112],
        T=pikepdf.String("AutoCAD SHX Text"),
        Contents=pikepdf.String('%%c2" 90%%d %%p1/8" 45° 2\'-5"'),
    )
    assert shx_comment_text(pdf, page, [comment]) == 1
    buffer = io.BytesIO()
    pdf.save(buffer)

    with pdfplumber.open(io.BytesIO(buffer.getvalue())) as plumbed:
        text = "".join(char["text"] for char in plumbed.pages[0].chars)
    assert text == '⌀2" 90° ±1/8" 45° 2\'-5"'


@pytest.mark.parametrize("text", ["45°", "90°", "⌀12", '⌀2"', '2"⌀', '⌀ 3/4"', '±1/8"', '36"±'])
def test_a_decoded_symbol_never_makes_a_dimension(text: str) -> None:
    """Outcome: the parser the CAD-text route records with (`record._parse`, through `units/`)
    gives none of these a value, so a degree or a diameter never becomes inches. Each stays a
    string for a person to read."""
    measurement, flags, dual = _parse(text)

    assert measurement is None and dual is None
    assert flags == (UNPARSED_FLAG,)
