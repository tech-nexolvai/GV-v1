"""Reading a sheet's annotation layers apart from each other.

Verification for: `extraction/annotations.py` (#539).

**The PDFs here are written by hand, from literal content and appearance streams**, the way
`tests/extraction/test_reader.py` does it and for the same reasons: no new dependency, and a file
that states exactly what it contains rather than a captured drawing that has to be believed. Each one
says what is in it — a `/FreeText` whose text is `KNOWN_MARKUP`, a `/Stamp` whose appearance draws a
100-point line and a five-path cluster — and the tests assert the reader reported *that*.

This is not the fixture `AGENTS.md` §9 forbids. Nothing here is tuned: every threshold is passed in
by the test, and the two that matter are asserted to change the answer when they change.

The tests worth reading first are `test_the_appearance_matrix_places_the_geometry`, which is the
failure that produced plausible geometry a third of a page out of place, and
`test_the_two_layers_are_never_merged`, which is the point of the module.
"""

from __future__ import annotations

import zlib
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from extraction.annotations import (
    DrawingLayer,
    read_annotation_layers,
)
from extraction.reader import UnreadablePdf

DOCUMENT = UUID("11111111-1111-4111-8111-111111111111")
DPI = 150

#: What the reviewer's annotation says. A dimension-shaped string, because that is the case that
#: matters: this text must reach a caller without a model being asked to read a picture of it.
KNOWN_MARKUP = '185 1/4"'
KNOWN_AUTHOR = "REVIEWER-1"

#: The appearance stream of the vendor's drawing: one long horizontal line, then five short strokes
#: close together — line-work and a glyph-sized cluster, which is the whole distinction the module
#: draws. Coordinates are in the appearance's own space, which is deliberately *not* page space.
STAMP_APPEARANCE = (
    b"1 w 100 500 m 200 500 l S\n"
    b"110 520 m 112 524 l S\n"
    b"113 520 m 115 524 l S\n"
    b"116 520 m 118 524 l S\n"
    b"119 520 m 121 524 l S\n"
    b"122 520 m 124 524 l S\n"
)


def _pdf(
    *,
    annotations: list[bytes],
    extra_objects: list[bytes] | None = None,
    box: bytes = b"[0 0 400 300]",
) -> bytes:
    """A one-page PDF whose page draws nothing and whose annotations carry everything.

    The shape of the first real client set: an all-but-empty page content stream with the drawing
    and the markup in `/Annots`. Built by hand with a real cross-reference table, so this is a valid
    PDF rather than something that happens to parse.
    """
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox "
        + box
        + b" /Annots ["
        + b" ".join(f"{index} 0 R".encode() for index in range(5, 5 + len(annotations)))
        + b"] /Contents 4 0 R >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
        *annotations,
        *(extra_objects or []),
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(start).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


def _free_text(text: str = KNOWN_MARKUP, rect: bytes = b"[40 40 120 60]") -> bytes:
    return (
        b"<< /Type /Annot /Subtype /FreeText /Rect "
        + rect
        + b" /Contents ("
        + text.encode("latin-1")
        + b") /T ("
        + KNOWN_AUTHOR.encode("latin-1")
        + b") >>"
    )


def _stamp(*, rect: bytes = b"[50 50 350 250]", appearance_object: int) -> bytes:
    """The vendor's drawing: a rect on the page and a reference to the appearance that fills it.

    `/BBox` and `/Matrix` belong to the appearance stream, not here — which is the whole reason the
    placement has to be computed rather than read off the annotation.
    """
    return (
        b"<< /Type /Annot /Subtype /Stamp /Rect "
        + rect
        + b" /T ("
        + KNOWN_AUTHOR.encode("latin-1")
        + b") /AP << /N "
        + str(appearance_object).encode()
        + b" 0 R >> >>"
    )


def _appearance(
    stream: bytes = STAMP_APPEARANCE,
    bbox: bytes = b"[100 500 400 700]",
    matrix: bytes = b"[1 0 0 1 -100 -500]",
) -> bytes:
    compressed = zlib.compress(stream)
    return (
        b"<< /Type /XObject /Subtype /Form /FormType 1 /BBox "
        + bbox
        + b" /Matrix "
        + matrix
        + b" /Filter /FlateDecode /Length "
        + str(len(compressed)).encode()
        + b" >>\nstream\n"
        + compressed
        + b"\nendstream"
    )


#: A sheet with both layers on it: the vendor's stamp and one reviewer note.
BOTH_LAYERS = _pdf(
    annotations=[_free_text(), _stamp(appearance_object=7)],
    extra_objects=[_appearance()],
)


def _layers(
    data: bytes = BOTH_LAYERS,
    *,
    page: int = 0,
    line_minimum_pt: Decimal = Decimal(50),
    glyph_maximum_pt: Decimal = Decimal(10),
    glyph_gap_pt: Decimal = Decimal(4),
    dpi: int = DPI,
    document_version_id: UUID = DOCUMENT,
):
    return read_annotation_layers(
        data,
        page,
        document_version_id=document_version_id,
        dpi=dpi,
        line_minimum_pt=line_minimum_pt,
        glyph_maximum_pt=glyph_maximum_pt,
        glyph_gap_pt=glyph_gap_pt,
    )


# ---------------------------------------------------------------------------
# The markup layer: exact text, no model
# ---------------------------------------------------------------------------


def test_reviewer_markup_is_read_as_exact_text() -> None:
    """Input: a `/FreeText` saying `185 1/4"`. Outcome: that string, with its author.

    **The reason this module exists.** `extraction/reader.py` reads the page content stream, which on
    a sheet like this is empty, so it reports the page unreadable and everything goes to OCR — while
    this string sits in the file. A reading with an error rate would be substituted for a fact.
    """
    layers = _layers()

    assert [note.text for note in layers.markup] == [KNOWN_MARKUP]
    assert layers.markup[0].author == KNOWN_AUTHOR
    assert layers.markup[0].layer is DrawingLayer.REVIEWER_MARKUP
    assert layers.markup[0].subtype == "FreeText"
    assert layers.readable


def test_markup_text_is_not_parsed_measured_or_typed() -> None:
    """Outcome: a string and a rectangle. No value, no unit, no semantic type.

    Semantic typing is gated on reviewed answers (#274, Q20). `185 1/4"` looks like a width and this
    module must not say so — the guard is that `MarkupNote` has no field that could carry the claim.
    """
    note = _layers().markup[0]

    assert not hasattr(note, "semantic_type")
    assert not hasattr(note, "parsed_value")
    assert not hasattr(note, "unit_guess")
    assert note.text == KNOWN_MARKUP


def test_a_markup_note_records_where_in_the_file_it_came_from() -> None:
    """Outcome: the `/Annots` index, so a reading can be checked against the file."""
    assert _layers().markup[0].annotation_index == 0


# ---------------------------------------------------------------------------
# The drawing layer: geometry, and the placement that makes it mean anything
# ---------------------------------------------------------------------------


def test_the_stamp_line_work_is_extracted_as_page_geometry() -> None:
    """Input: an appearance drawing a 100-point line. Outcome: one segment, in stored space."""
    layers = _layers()

    assert len(layers.drawing_segments) == 1
    segment = layers.drawing_segments[0]
    assert segment.document_version_id == DOCUMENT
    assert segment.page == 0
    assert segment.axis == "horizontal"


def test_the_appearance_matrix_places_the_geometry() -> None:
    """**The bug that produced plausible geometry in the wrong place.**

    An appearance stream has its own coordinate system, mapped onto the annotation's `/Rect` through
    `/BBox` and `/Matrix` (PDF 32000-1 §12.5.5). Skipping the matrix moved every path on the first
    real sheet 192 points up the page — still a drawing, still parseable, every region wrong.

    The line here is drawn at `y = 500` in a `/BBox` starting at 500, translated to the origin by
    `/Matrix`, and placed in a `/Rect` whose bottom is 50. So it belongs at `y = 50` on a 300-point
    page: a fifth of the way from the bottom, which is four fifths of the way down in stored space.
    A reader that ignored the matrix would put it off the page entirely.
    """
    segment = _layers().drawing_segments[0]

    assert segment.start.y == pytest.approx(Decimal("0.8333"), abs=Decimal("0.002"))
    assert segment.start.x == pytest.approx(Decimal("0.125"), abs=Decimal("0.002"))
    assert segment.end.x == pytest.approx(Decimal("0.375"), abs=Decimal("0.002"))


def test_a_scaled_appearance_is_placed_by_its_own_scale() -> None:
    """Input: the same drawing in a `/BBox` twice the `/Rect`. Outcome: geometry at half size.

    On the first real sheet one stamp's paths carried the identity matrix and the other a uniform
    0.12, so a reader that assumed a translation was right on one sheet and eight times out on the
    next. The placement is computed from the file, so a scale it has never seen still lands.
    """
    wide = _pdf(
        annotations=[_stamp(rect=b"[50 50 350 150]", appearance_object=6)],
        extra_objects=[_appearance(bbox=b"[100 500 700 900]", matrix=b"[1 0 0 1 -100 -500]")],
    )
    scaled = _layers(wide, line_minimum_pt=Decimal(20))

    # The line is 100 points long in an appearance 600 wide placed in a rect 300 wide, so it lands
    # 50 points long: an eighth of a 400-point page.
    segment = scaled.drawing_segments[0]
    assert abs(segment.end.x - segment.start.x) == pytest.approx(
        Decimal("0.125"), abs=Decimal("0.005")
    )


def test_glyph_sized_paths_become_one_candidate_region() -> None:
    """Input: five short strokes 3 points apart. Outcome: one region of five paths.

    Outlined text is what these sheets draw instead of characters, and one label is many small
    paths. Merging them into one region is what makes a crop that contains a whole number rather
    than one stroke of one digit.
    """
    layers = _layers()

    assert len(layers.outlined_regions) == 1
    region = layers.outlined_regions[0]
    assert region.path_count == 5
    assert region.point_count == 10


def test_a_wider_gap_splits_the_cluster() -> None:
    """Input: the same strokes with the gap set below their spacing. Outcome: five regions.

    The clustering length is load-bearing and belongs to the caller, so this asserts it *does*
    something: at a gap smaller than the spacing, every stroke is its own region — which is what a
    reader would produce if the number were set as five separate labels.
    """
    split = _layers(glyph_gap_pt=Decimal("0.5"))

    assert len(split.outlined_regions) == 5
    assert {region.path_count for region in split.outlined_regions} == {1}


def test_line_work_and_glyphs_are_split_by_the_length_the_caller_names() -> None:
    """Input: the same file read with the line minimum below the strokes' length. Outcome: more lines.

    The two lengths decide what is read and what is measured, which is exactly why neither has a
    default (#179). Lowering the minimum turns the glyph strokes into line-work as well, and the
    test is here so that this is a visible consequence rather than a surprise.
    """
    strict = _layers(line_minimum_pt=Decimal(50))
    loose = _layers(line_minimum_pt=Decimal(1))

    assert len(strict.drawing_segments) == 1
    assert len(loose.drawing_segments) > len(strict.drawing_segments)


def test_paths_that_are_neither_are_counted_rather_than_forced() -> None:
    """Input: a stroke too long for a glyph and too short for line-work. Outcome: a named refusal.

    Hatching and arrowheads live in this gap. Forcing them into one of the two answers would either
    invent a dimension line or send a crop of a hatch pattern to a model as though it were a label.
    """
    middling = _pdf(
        annotations=[_stamp(appearance_object=6)],
        extra_objects=[_appearance(b"1 w 100 500 m 130 500 l S\n")],
    )
    layers = _layers(middling, line_minimum_pt=Decimal(50), glyph_maximum_pt=Decimal(10))

    assert layers.drawing_segments == ()
    assert layers.outlined_regions == ()
    assert any("neither line-work nor glyph-sized" in item.reason for item in layers.refusals)


# ---------------------------------------------------------------------------
# The distinction the module exists for
# ---------------------------------------------------------------------------


def test_the_two_layers_are_never_merged() -> None:
    """Outcome: the reviewer's text and the vendor's geometry arrive in separate fields.

    On the first real sheet the drawing says one overall width and the markup says another, and
    flattening them into one rendered image left colour as the only way to tell which was which.
    Keeping them apart is what makes that disagreement a review signal instead of a blend.
    """
    layers = _layers()

    assert [note.text for note in layers.markup] == [KNOWN_MARKUP]
    assert len(layers.drawing_segments) == 1
    assert len(layers.outlined_regions) == 1
    assert all(note.layer is DrawingLayer.REVIEWER_MARKUP for note in layers.markup)
    # Nothing on the result compares them, and nothing has a field that could hold a reconciliation.
    assert not hasattr(layers, "agreed")
    assert not hasattr(layers, "resolved")


def test_an_unrecognised_annotation_is_reported_and_not_folded_into_either_layer() -> None:
    """Input: a `/Square` annotation. Outcome: `OTHER`, kept separately.

    A markup type nobody has considered is exactly the thing that must not be quietly counted as
    part of the vendor's drawing.
    """
    other = _pdf(
        annotations=[
            b"<< /Type /Annot /Subtype /Square /Rect [10 10 60 40] /Contents (a box) >>",
            _stamp(appearance_object=7),
        ],
        extra_objects=[_appearance()],
    )
    layers = _layers(other)

    assert layers.markup == ()
    assert [note.subtype for note in layers.other_layer_notes] == ["Square"]
    assert layers.other_layer_notes[0].layer is DrawingLayer.OTHER


# ---------------------------------------------------------------------------
# Refusing rather than inventing
# ---------------------------------------------------------------------------


def test_a_page_with_no_annotations_says_so() -> None:
    """Input: a page with an empty `/Annots`. Outcome: a reason, not an empty success.

    Empty tuples with no explanation read as a sheet that had nothing on it, which is the same
    mistake `PageContents.unreadable_reason` exists to prevent.
    """
    layers = _layers(_pdf(annotations=[]))

    assert not layers.readable
    assert layers.unreadable_reason is not None
    assert "no annotation layers" in layers.unreadable_reason


def test_a_stamp_with_no_appearance_is_refused_not_skipped() -> None:
    """Input: a `/Stamp` with no `/AP`. Outcome: a refusal naming it.

    There is no geometry to read and no honest way to invent any. Silence would make a sheet whose
    drawing failed to load look like a sheet with no drawing on it.
    """
    layers = _layers(
        _pdf(annotations=[b"<< /Type /Annot /Subtype /Stamp /Rect [50 50 350 250] >>"])
    )

    assert layers.drawing_segments == ()
    assert [item.subtype for item in layers.refusals] == ["Stamp"]
    assert "appearance" in layers.refusals[0].reason


def test_a_page_beyond_the_document_is_refused() -> None:
    """Input: page 9 of a one-page file. Outcome: `UnreadablePdf`, not an empty result."""
    with pytest.raises(UnreadablePdf, match="beyond"):
        _layers(page=9)


@pytest.mark.parametrize("data", [b"", b"not a pdf", b"%PDF-1.7\nbroken"])
def test_a_file_that_will_not_parse_is_refused(data: bytes) -> None:
    """Input: junk. Outcome: `UnreadablePdf`."""
    with pytest.raises(UnreadablePdf):
        _layers(data)


@pytest.mark.parametrize("dpi", [0, -1, True])
def test_a_dpi_that_cannot_scale_is_refused(dpi: object) -> None:
    """Input: a non-positive or boolean dpi. Outcome: `ValueError`, because stored space depends on it."""
    with pytest.raises(ValueError, match="dpi"):
        _layers(dpi=dpi)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["line_minimum_pt", "glyph_maximum_pt", "glyph_gap_pt"])
def test_a_float_length_is_refused(name: str) -> None:
    """Input: a float length. Outcome: `TypeError`.

    A float would make which paths are line-work depend on binary rounding, and the wrong answer
    would look exactly like the right one — the reason `text_association` refuses one too.
    """
    with pytest.raises(TypeError, match="never a float"):
        _layers(**{name: 12.0})  # type: ignore[arg-type]


def test_geometry_is_tied_to_the_document_version_it_was_read_from() -> None:
    """Outcome: every polygon and segment carries the caller's version, not a page-local guess."""
    version = uuid4()
    layers = _layers(document_version_id=version)

    assert layers.markup[0].extent.document_version_id == version
    assert layers.drawing_segments[0].document_version_id == version
    assert layers.outlined_regions[0].extent.document_version_id == version
