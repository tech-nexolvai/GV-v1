"""The front door (#1724): every PDF variation becomes the standard working copy, shown as before.

One synthetic PDF per variation (the research note's test list, §8), each built from raw drawing
bytes so every expected position is known exactly. For each one:

- **it looks the same**: the original as displayed and the working copy, both drawn at 150 dpi
  with annotations, agree pixel for pixel but for anti-aliasing (`_same_look`);
- **it is in the standard shape**: `/Rotate 0`, the media box at the origin and equal to the crop
  box, no `/UserUnit`, no encryption (`_standard`);
- **the label is where the original shows it**: pdfplumber on the working copy against
  `PageTransform` on the original, within 0.01 pt (`_label_spot`).

A page the front door cannot make exactly is blank with a plain reason, never read in the wrong
place; a file already in shape comes back byte for byte.
"""

from __future__ import annotations

import io
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pdfplumber
import pikepdf
import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pytest
from PIL import Image

from evidence.coordinates import PageTransform, PdfPoint
from extraction import working_copy as front_door
from extraction.working_copy import WorkingCopyRefused, working_copy

DPI = 150
LABEL = "24 1/2"
#: Where the label is drawn, from the visible area's lower-left corner (content-stream points).
LABEL_AT = (320.0, 210.0)
WIDTH, HEIGHT = 700.0, 500.0
ROTATIONS = (0, 90, 180, 270)


# ---- building synthetic PDFs ---------------------------------------------------------------------


def _font(pdf: pikepdf.Pdf) -> Any:
    return pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
        )
    )


def _drawing(
    ox: float, oy: float, width: float = WIDTH, height: float = HEIGHT, colour: str = "0 g"
) -> bytes:
    """A frame, a few lines, the label and a note, placed from `(ox, oy)`."""
    lx, ly = ox + LABEL_AT[0], oy + LABEL_AT[1]
    return (
        f"1 w {ox + 10} {oy + 10} {width - 20} {height - 20} re S\n"
        f"0.5 w {ox + 40} {oy + 60} m {ox + 300} {oy + 400} l S\n"
        f"0 0 1 RG 2 w {ox + 500} {oy + 50} m {ox + 650} {oy + 120} l S\n"
        f"{colour} BT /F1 14 Tf {lx} {ly} Td ({LABEL}) Tj ET\n"
        f"0 g BT /F1 9 Tf {ox + 40} {oy + 30} Td (SCRIBE TO FIT) Tj ET\n"
    ).encode()


def _page(
    pdf: pikepdf.Pdf,
    *,
    rotate: int = 0,
    media: tuple[float, float, float, float] = (0, 0, WIDTH, HEIGHT),
    crop: tuple[float, float, float, float] | None = None,
    unit: float | None = None,
    content: bytes | None = None,
    colour: str = "0 g",
) -> pikepdf.Page:
    visible = crop or media
    page = pikepdf.Dictionary(
        Type=pikepdf.Name.Page,
        MediaBox=list(media),
        Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=_font(pdf))),
        Contents=pdf.make_stream(
            content
            if content is not None
            else _drawing(
                visible[0],
                visible[1],
                visible[2] - visible[0],
                visible[3] - visible[1],
                colour,
            )
        ),
    )
    if rotate:
        page["/Rotate"] = rotate
    if crop is not None:
        page["/CropBox"] = list(crop)
    if unit is not None:
        page["/UserUnit"] = unit
    pdf.pages.append(pikepdf.Page(page))
    return pdf.pages[-1]


def _bytes(pdf: pikepdf.Pdf, **save: Any) -> bytes:
    out = io.BytesIO()
    pdf.save(out, **save)
    return out.getvalue()


def _one(**page: Any) -> bytes:
    pdf = pikepdf.new()
    _page(pdf, **page)
    return _bytes(pdf)


def _appearance(pdf: pikepdf.Pdf, size: tuple[float, float], stream: bytes) -> Any:
    form = pdf.make_stream(stream)
    form["/Type"] = pikepdf.Name.XObject
    form["/Subtype"] = pikepdf.Name.Form
    form["/BBox"] = [0, 0, size[0], size[1]]
    form["/Resources"] = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=_font(pdf)))
    return form


def _annotate(pdf: pikepdf.Pdf, page: pikepdf.Page, ox: float = 0, oy: float = 0) -> None:
    """A square (a reviewer's box), a pasted drawing (`/Stamp`), a note and an ink stroke."""

    def annotation(subtype: str, rect: list[float], stream: bytes, **more: Any) -> Any:
        size = (rect[2] - rect[0], rect[3] - rect[1])
        return pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Annot,
                Subtype=pikepdf.Name(subtype),
                Rect=[rect[0] + ox, rect[1] + oy, rect[2] + ox, rect[3] + oy],
                AP=pikepdf.Dictionary(N=_appearance(pdf, size, stream)),
                **more,
            )
        )

    page.obj["/Annots"] = pikepdf.Array(
        [
            annotation("/Square", [300, 190, 420, 240], b"1 0 0 RG 2 w 1 1 118 48 re S"),
            annotation(
                "/Stamp",
                [420, 300, 620, 400],
                b"0 0 0 RG 1 w 5 5 190 90 re S BT /F1 12 Tf 20 40 Td (24 3/4) Tj ET",
            ),
            annotation(
                "/FreeText",
                [60, 420, 180, 450],
                b"1 0 0 rg BT /F1 10 Tf 4 10 Td (GV NOTE) Tj ET",
                Contents=pikepdf.String("GV NOTE"),
            ),
            annotation(
                "/Ink",
                [100, 100, 200, 200],
                b"1 0 0 RG 2 w 10 10 m 90 90 l S",
                InkList=[[110 + ox, 110 + oy, 190 + ox, 190 + oy]],
            ),
        ]
    )


# ---- checking ------------------------------------------------------------------------------------


def _render(data: bytes, dpi: float = DPI) -> list[np.ndarray]:
    document = pdfium.PdfDocument(data)
    try:
        return [
            np.asarray(
                page.render(scale=dpi / 72, may_draw_forms=True).to_pil().convert("L")
            ).astype(int)
            for page in document
        ]
    finally:
        document.close()


def _same_look(original: bytes, working: bytes, *, scale: float = 1.0) -> None:
    """Every page of `working` at 150 dpi looks like `original`'s at `150 * scale` dpi: the same
    size and, but for anti-aliasing on an edge here and there, the same pixels (0.1%)."""
    before, after = _render(original, DPI * scale), _render(working)
    assert len(before) == len(after)
    for number, (a, b) in enumerate(zip(before, after, strict=True), start=1):
        assert a.shape == b.shape, f"page {number}: {a.shape} became {b.shape}"
        assert (a < 200).sum() > 100, f"page {number} shows nothing to compare"
        differing = (np.abs(a - b) > 32).mean()
        assert differing <= 0.001, f"page {number}: {differing:.4%} of pixels differ"


def _standard(working: bytes) -> None:
    with pikepdf.open(io.BytesIO(working)) as pdf:
        assert not pdf.is_encrypted
        for page in pdf.pages:
            assert int(page.obj.get("/Rotate", 0)) == 0
            media = [float(v) for v in page.obj["/MediaBox"]]
            assert media[:2] == [0.0, 0.0]
            crop = page.obj.get("/CropBox")
            assert crop is None or [float(v) for v in crop] == media
            assert "/UserUnit" not in page.obj


def _shown(
    rotate: int,
    visible: tuple[float, float, float, float],
    x: float,
    y: float,
    unit: float = 1,
) -> tuple[float, float]:
    """Where a content-stream point shows on the original page (points, origin top-left)."""
    box = tuple(Decimal(str(v)) for v in visible)
    transform = PageTransform(dpi=7200, rotation=rotate, media_box=box, crop_box=box)  # type: ignore[arg-type]
    point = transform.to_image(PdfPoint(Decimal(str(x)), Decimal(str(y))))
    return point.x / 100 * unit, point.y / 100 * unit


def _label_spot(working: bytes, page: int = 0) -> tuple[float, float]:
    """Where the label was placed on the working copy (its text origin), points from top-left."""
    with pdfplumber.open(io.BytesIO(working)) as document:
        chars = [c for c in document.pages[page].chars if c["text"] == "2"]
        assert chars, "the label is not read on the working copy"
        matrix = chars[0]["matrix"]
        return float(matrix[4]), float(document.pages[page].height) - float(matrix[5])


def _annotation_rects(working: bytes, page: int = 0) -> list[list[float]]:
    with pikepdf.open(io.BytesIO(working)) as pdf:
        return [[float(v) for v in a["/Rect"]] for a in pdf.pages[page].obj["/Annots"]]


def _shown_rect(
    rotate: int, visible: tuple[float, float, float, float], rect: list[float]
) -> list[float]:
    """An original rectangle as the working copy holds it (PDF space of the shown page)."""
    corners = [
        _shown(rotate, visible, x, y) for x in (rect[0], rect[2]) for y in (rect[1], rect[3])
    ]
    width, height = visible[2] - visible[0], visible[3] - visible[1]
    shown_height = width if rotate in (90, 270) else height
    xs = [c[0] for c in corners]
    ys = [shown_height - c[1] for c in corners]
    return [min(xs), min(ys), max(xs), max(ys)]


# ---- 1-4, 8: rotation, with annotations; the trigger shape ---------------------------------------


def test_a_file_already_in_shape_comes_back_byte_for_byte() -> None:
    data = _one()
    copy = working_copy(data)
    assert copy.changed is False and copy.data is data and copy.changes == ()


@pytest.mark.parametrize("offset", [(0.0, 0.0), (-30.0, 15.0)])
@pytest.mark.parametrize("rotate", ROTATIONS)
def test_rotation_is_baked_in_and_the_annotations_go_with_it(
    rotate: int, offset: tuple[float, float]
) -> None:
    ox, oy = offset
    media = (ox, oy, ox + WIDTH, oy + HEIGHT)
    pdf = pikepdf.new()
    page = _page(pdf, rotate=rotate, media=media)
    _annotate(pdf, page, ox, oy)
    data = _bytes(pdf)
    copy = working_copy(data)
    assert copy.changed is (rotate != 0 or offset != (0.0, 0.0))
    assert copy.not_read == ()
    _standard(copy.data)
    _same_look(data, copy.data)
    expected = _shown(rotate, media, ox + LABEL_AT[0], oy + LABEL_AT[1])
    assert _label_spot(copy.data) == pytest.approx(expected, abs=0.01)
    # Each annotation's rectangle is where the original page shows it; the ink stroke's points too.
    with pikepdf.open(io.BytesIO(data)) as original:
        rects = [[float(v) for v in a["/Rect"]] for a in original.pages[0].obj["/Annots"]]
    for got, rect in zip(_annotation_rects(copy.data), rects, strict=True):
        assert got == pytest.approx(_shown_rect(rotate, media, rect), abs=1e-6)
    with pikepdf.open(io.BytesIO(copy.data)) as out:
        ink = [float(v) for v in out.pages[0].obj["/Annots"][3]["/InkList"][0]]
    start = _shown(rotate, media, ox + 110, oy + 110)
    shown_height = WIDTH if rotate in (90, 270) else HEIGHT
    assert (ink[0], shown_height - ink[1]) == pytest.approx(start, abs=1e-6)


def test_the_set_pack_annotation_boxes_are_the_same_before_and_after() -> None:
    """The set pack's annotation boxes (shown points) read from the turned original and from its
    working copy agree: the front door moves nothing a reviewer sees."""
    from pypdf import PdfReader

    from app.review.assistant import set_pack

    pdf = pikepdf.new()
    page = _page(pdf, rotate=270, media=(-30, 15, WIDTH - 30, HEIGHT + 15))
    _annotate(pdf, page, -30, 15)
    data = _bytes(pdf)
    copy = working_copy(data)
    before = set_pack._annotations("a.pdf", PdfReader(io.BytesIO(data)))
    after = set_pack._annotations("a.pdf", PdfReader(io.BytesIO(copy.data)))
    assert len(before) == len(after) == 4
    assert [a["box"] for a in before] == pytest.approx([a["box"] for a in after], abs=0.11)


# ---- 5-6: inherited and odd rotations ------------------------------------------------------------


def test_a_rotation_and_page_size_inherited_from_the_page_tree() -> None:
    pdf = pikepdf.new()
    _page(pdf)
    page = pdf.pages[0]
    del page.obj["/MediaBox"]
    pdf.Root.Pages["/MediaBox"] = pikepdf.Array([0, 0, WIDTH, HEIGHT])
    pdf.Root.Pages["/Rotate"] = 270
    data = _bytes(pdf)
    copy = working_copy(data)
    assert copy.changes == ("rotation baked into page 1",)
    _standard(copy.data)
    _same_look(data, copy.data)
    expected = _shown(270, (0, 0, WIDTH, HEIGHT), *LABEL_AT)
    assert _label_spot(copy.data) == pytest.approx(expected, abs=0.01)


def test_a_negative_quarter_turn_is_the_same_turn() -> None:
    data = _one(rotate=-90)
    copy = working_copy(data)
    _same_look(data, copy.data)
    assert _label_spot(copy.data) == pytest.approx(
        _shown(270, (0, 0, WIDTH, HEIGHT), *LABEL_AT), abs=0.01
    )


def test_a_turn_that_is_not_a_quarter_turn_is_shown_upright_as_viewers_show_it() -> None:
    data = _one(rotate=45)
    copy = working_copy(data)
    assert copy.changes == (
        "a rotation that is not a quarter turn shown upright (as viewers show it) on page 1",
    )
    _standard(copy.data)
    _same_look(data, copy.data)
    assert _label_spot(copy.data) == pytest.approx(
        _shown(0, (0, 0, WIDTH, HEIGHT), *LABEL_AT), abs=0.01
    )


# ---- 7, 9: offset and cropped boxes --------------------------------------------------------------


@pytest.mark.parametrize("rotate", [0, 270])
def test_an_offset_cropped_page_is_its_visible_area_at_the_corner(rotate: int) -> None:
    media = (500.0, 400.0, 1500.0, 1300.0)
    crop = (560.0, 440.0, 560.0 + WIDTH, 440.0 + HEIGHT)
    pdf = pikepdf.new()
    page = _page(pdf, rotate=rotate, media=media, crop=crop)
    page.obj["/TrimBox"] = pikepdf.Array([570, 450, 1250, 930])
    data = _bytes(pdf)
    copy = working_copy(data)
    _standard(copy.data)
    _same_look(data, copy.data)
    expected = _shown(rotate, crop, crop[0] + LABEL_AT[0], crop[1] + LABEL_AT[1])
    assert _label_spot(copy.data) == pytest.approx(expected, abs=0.01)
    with pikepdf.open(io.BytesIO(copy.data)) as out:
        trim = [float(v) for v in out.pages[0].obj["/TrimBox"]]
        media_after = [float(v) for v in out.pages[0].obj["/MediaBox"]]
    # The trim box is kept, moved, and held inside the visible page.
    assert 0 <= trim[0] < trim[2] <= media_after[2] and 0 <= trim[1] < trim[3] <= media_after[3]


def test_text_cropped_away_is_not_read(tmp_path: Path) -> None:
    """A word the file keeps outside its visible area is not on the drawing: not a label."""
    from app.review.assistant import dim_labels

    crop = (0.0, 0.0, WIDTH, HEIGHT)
    content = _drawing(0, 0) + b'BT /F1 14 Tf 760 300 Td (99") Tj ET\n'
    data = _one(media=(0, 0, WIDTH + 200, HEIGHT), crop=crop, content=content)
    copy = working_copy(data)
    path = tmp_path / "cropped.pdf"
    path.write_bytes(copy.data)
    found = dim_labels.page_labels(path, 1, equal_parts=False)
    texts = [label["text"] for label in found.labels] + [p["text"] for p in found.phrases]
    assert not any("99" in text for text in texts)
    assert any("SCRIBE" in text for text in texts)


# ---- 10-11: user unit, huge and mixed pages ------------------------------------------------------


def test_a_user_unit_is_applied() -> None:
    """`/UserUnit 2`: the working copy is twice the size in points and looks like the original drawn
    at twice the scale (PDFium ignores `/UserUnit`)."""
    data = _one(rotate=90, unit=2)
    copy = working_copy(data)
    assert "user unit applied on page 1" in copy.changes
    _standard(copy.data)
    with pikepdf.open(io.BytesIO(copy.data)) as pdf:
        assert [float(v) for v in pdf.pages[0].obj["/MediaBox"]] == [0, 0, 2 * HEIGHT, 2 * WIDTH]
    _same_look(data, copy.data, scale=2.0)
    expected = _shown(90, (0, 0, WIDTH, HEIGHT), *LABEL_AT, unit=2)
    assert _label_spot(copy.data) == pytest.approx(expected, abs=0.01)


def test_a_user_unit_past_the_largest_page_is_kept_and_said() -> None:
    data = _one(unit=40)
    copy = working_copy(data)
    assert copy.changes == ("large-page user unit kept (sizes need its factor) on page 1",)
    with pikepdf.open(io.BytesIO(copy.data)) as pdf:
        assert float(pdf.pages[0].obj["/UserUnit"]) == 40


def test_a_huge_sheet_and_mixed_sizes_each_get_their_own_shape() -> None:
    pdf = pikepdf.new()
    _page(pdf, media=(0, 0, 612, 792), content=_drawing(0, 0, 612, 792))
    _page(pdf, rotate=90, media=(0, 0, 3456, 2592), content=_drawing(0, 0, 3456, 2592))
    _page(pdf, rotate=270, media=(10, 10, 1738, 2602), content=_drawing(10, 10, 1728, 2592))
    data = _bytes(pdf)
    copy = working_copy(data)
    assert copy.changes == (
        "rotation baked into pages 2, 3",
        "visible area moved to the corner on page 3",
    )
    _standard(copy.data)
    _same_look(data, copy.data)
    with pikepdf.open(io.BytesIO(copy.data)) as out:
        sizes = [tuple(float(v) for v in p.obj["/MediaBox"]) for p in out.pages]
    assert sizes == [(0, 0, 612, 792), (0, 0, 2592, 3456), (0, 0, 2592, 1728)]


# ---- 12-14, 30: drawing structure the front door leaves alone ------------------------------------


def test_a_form_xobject_with_its_own_matrix_turns_with_the_page() -> None:
    pdf = pikepdf.new()
    block = pdf.make_stream(_drawing(0, 0))
    block["/Type"] = pikepdf.Name.XObject
    block["/Subtype"] = pikepdf.Name.Form
    block["/BBox"] = [0, 0, WIDTH, HEIGHT]
    block["/Matrix"] = [0.5, 0, 0, 0.5, 100, 80]
    block["/Resources"] = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=_font(pdf)))
    page = _page(pdf, rotate=90, content=b"q /B0 Do Q")
    page.obj["/Resources"]["/XObject"] = pikepdf.Dictionary(B0=block)
    data = _bytes(pdf)
    copy = working_copy(data)
    _same_look(data, copy.data)
    x, y = 100 + 0.5 * LABEL_AT[0], 80 + 0.5 * LABEL_AT[1]
    assert _label_spot(copy.data) == pytest.approx(
        _shown(90, (0, 0, WIDTH, HEIGHT), x, y), abs=0.01
    )


@pytest.mark.parametrize(
    "placing",
    [
        b"BT /F1 14 Tf 0 1 -1 0 320 210 Tm (24 1/2) Tj ET",  # vertical dimension text
        b"q -1 0 0 1 700 0 cm BT /F1 14 Tf 320 210 Td (24 1/2) Tj ET Q",  # a mirrored block
    ],
)
def test_vertical_and_mirrored_text_keep_their_place(placing: bytes) -> None:
    content = b"1 w 10 10 680 480 re S\n" + placing + b"\n"
    data = _one(rotate=270, content=content)
    copy = working_copy(data)
    _same_look(data, copy.data)
    with (
        pdfplumber.open(io.BytesIO(data)) as before,
        pdfplumber.open(io.BytesIO(copy.data)) as after,
    ):
        # pdfplumber already reads the page as displayed: the working copy's characters are
        # exactly where the original's were.
        a = [(c["text"], round(c["x0"], 2), round(c["top"], 2)) for c in before.pages[0].chars]
        b = [(c["text"], round(c["x0"], 2), round(c["top"], 2)) for c in after.pages[0].chars]
    assert a == b and len(a) == 6


@pytest.mark.parametrize("colour", ["1 0 0 rg", "0 0 1 rg", "0 0.6 0 rg"])
def test_vendor_text_in_colour_is_handled_like_any_other(colour: str) -> None:
    data = _one(rotate=90, colour=colour)
    copy = working_copy(data)
    _same_look(data, copy.data)
    assert _label_spot(copy.data) == pytest.approx(
        _shown(90, (0, 0, WIDTH, HEIGHT), *LABEL_AT), abs=0.01
    )


def test_layers_are_kept() -> None:
    pdf = pikepdf.new()
    layer = pdf.make_indirect(
        pikepdf.Dictionary(Type=pikepdf.Name.OCG, Name=pikepdf.String("A-ANNO-DIMS"))
    )
    pdf.Root.OCProperties = pikepdf.Dictionary(
        OCGs=[layer], D=pikepdf.Dictionary(ON=[layer], Order=[layer])
    )
    content = b"/OC /L1 BDC\n" + _drawing(0, 0) + b"EMC\n"
    page = _page(pdf, rotate=270, content=content)
    page.obj["/Resources"]["/Properties"] = pikepdf.Dictionary(L1=layer)
    data = _bytes(pdf)
    copy = working_copy(data)
    _standard(copy.data)
    _same_look(data, copy.data)
    with pikepdf.open(io.BytesIO(copy.data)) as out:
        assert "/OCProperties" in out.Root
        assert "/Properties" in out.pages[0].obj["/Resources"]


# ---- 23: a scanned page --------------------------------------------------------------------------


def test_a_scanned_page_keeps_its_picture_where_it_shows_and_goes_to_the_picture_route(
    tmp_path: Path,
) -> None:
    """A page with no text layer, only a picture, on a turned page: the picture stays where the
    page shows it, and checking a value there goes to the drawn (picture) route, never to
    "nothing is printed here"."""
    from app.review.assistant.verify_value import verify_value

    pdf = pikepdf.new()
    pixels = np.full((200, 400), 255, dtype=np.uint8)
    pixels[60:140, 100:300] = 0
    image = pikepdf.Stream(pdf, Image.fromarray(pixels).tobytes())
    image["/Type"] = pikepdf.Name.XObject
    image["/Subtype"] = pikepdf.Name.Image
    image["/Width"], image["/Height"] = 400, 200
    image["/ColorSpace"] = pikepdf.Name.DeviceGray
    image["/BitsPerComponent"] = 8
    page = _page(pdf, rotate=90, content=b"q 400 0 0 200 100 150 cm /Im0 Do Q")
    page.obj["/Resources"]["/XObject"] = pikepdf.Dictionary(Im0=image)
    data = _bytes(pdf)
    copy = working_copy(data)
    _standard(copy.data)
    _same_look(data, copy.data)
    with pdfplumber.open(io.BytesIO(copy.data)) as document:
        assert document.pages[0].chars == []
        shown = document.pages[0].images[0]
    corners = [_shown(90, (0, 0, WIDTH, HEIGHT), x, y) for x, y in ((100, 150), (500, 350))]
    expected = (
        min(c[0] for c in corners),
        min(c[1] for c in corners),
        max(c[0] for c in corners),
        max(c[1] for c in corners),
    )
    assert tuple(float(shown[k]) for k in ("x0", "top", "x1", "bottom")) == pytest.approx(
        expected, abs=0.01
    )
    path = tmp_path / "scan.pdf"
    path.write_bytes(copy.data)
    box = {
        k: float(shown[v]) for k, v in (("x0", "x0"), ("y0", "top"), ("x1", "x1"), ("y1", "bottom"))
    }
    result = verify_value(path, 1, box, '36"', drawn=False)
    assert result.pending, result


# ---- 25-27: encryption, damage, saves in stages --------------------------------------------------


def test_an_encrypted_file_that_needs_no_password_is_decrypted() -> None:
    pdf = pikepdf.new()
    _page(pdf, rotate=90)
    data = _bytes(pdf, encryption=pikepdf.Encryption(owner="owner-only", user="", R=6))
    with pikepdf.open(io.BytesIO(data)) as check:
        assert check.is_encrypted
    copy = working_copy(data)
    assert "encryption removed (the file needed no password to open)" in copy.changes
    _standard(copy.data)
    _same_look(data, copy.data)


def test_a_file_locked_by_a_password_is_refused_plainly() -> None:
    pdf = pikepdf.new()
    _page(pdf)
    data = _bytes(pdf, encryption=pikepdf.Encryption(owner="o", user="secret", R=6))
    with pytest.raises(WorkingCopyRefused, match="locked with a password"):
        working_copy(data)


def test_a_file_that_is_not_a_pdf_is_refused_plainly() -> None:
    with pytest.raises(WorkingCopyRefused, match="damaged and could not be opened"):
        working_copy(b"%PDF-1.7 this is not a PDF")


def test_slight_damage_alone_leaves_the_file_as_it_is() -> None:
    data = _one()
    broken = data[: data.rindex(b"startxref")] + b"startxref\n999999\n%%EOF\n"
    copy = working_copy(broken)
    assert copy.changed is False and copy.data is broken


def test_a_broken_index_is_repaired_and_said() -> None:
    data = _one(rotate=90)
    broken = data[: data.rindex(b"startxref")] + b"startxref\n999999\n%%EOF\n"
    copy = working_copy(broken)
    assert "the file was slightly damaged and was repaired" in copy.changes
    _standard(copy.data)
    _same_look(data, copy.data)


def test_a_file_saved_in_stages_is_read_at_its_latest() -> None:
    from pypdf import PdfReader, PdfWriter

    data = _one(rotate=90)
    from pypdf.generic import ContentStream

    writer = PdfWriter(io.BytesIO(data), incremental=True)
    writer.pages[0].replace_contents(ContentStream(None, writer))
    out = io.BytesIO()
    writer.write(out)
    staged = out.getvalue()
    assert staged.count(b"%%EOF") == 2
    assert PdfReader(io.BytesIO(staged)).pages[0].extract_text() == ""
    copy = working_copy(staged)
    with pdfplumber.open(io.BytesIO(copy.data)) as document:
        assert document.pages[0].chars == []
    assert copy.data.count(b"%%EOF") == 1


# ---- what cannot be put in shape -----------------------------------------------------------------


def test_a_page_with_no_visible_area_is_blank_with_a_plain_reason() -> None:
    pdf = pikepdf.new()
    _page(pdf)
    _page(pdf, crop=(100, 100, 100, 300))
    copy = working_copy(_bytes(pdf))
    assert [page for page, _ in copy.not_read] == [2]
    assert copy.not_read[0][1] == (
        "page 2 could not be read: its visible area is empty (its page box is broken)"
    )
    with pdfplumber.open(io.BytesIO(copy.data)) as document:
        assert "24" in document.pages[0].extract_text()
        said = document.pages[1].extract_text()
    assert said.startswith("page 2 could not be read") and "24" not in said
    _standard(copy.data)


def test_a_page_whose_copy_does_not_look_the_same_is_never_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The picture check: a page whose working copy differs from the original is blank and says
    why; the unchanged page is never used in its place."""
    real = front_door._same
    calls = {"n": 0}

    def second_page_differs(a: np.ndarray, b: np.ndarray) -> bool:
        calls["n"] += 1
        return False if calls["n"] == 2 else real(a, b)

    monkeypatch.setattr(front_door, "_same", second_page_differs)
    pdf = pikepdf.new()
    _page(pdf, rotate=90)
    _page(pdf, rotate=90)
    copy = working_copy(_bytes(pdf))
    assert copy.not_read == (
        (
            2,
            (
                "page 2 could not be read: its working copy did not look exactly like the "
                "original, so nothing is read or drawn on it"
            ),
        ),
    )
    with pdfplumber.open(io.BytesIO(copy.data)) as document:
        assert "24" in document.pages[0].extract_text()
        assert "24" not in document.pages[1].extract_text()


def test_the_picture_check_tells_an_edge_from_a_moved_mark() -> None:
    page = np.full((400, 400), 255, dtype=np.int16)
    page[40:60, 40:42] = 0
    edge = page.copy()
    edge[40:60, 42] = 0  # an edge drawn one pixel over
    moved = page.copy()
    moved[10:20, 10:20] = 0  # a mark somewhere else
    assert front_door._same(page, page)
    assert front_door._same(page, edge)
    assert not front_door._same(page, moved)
