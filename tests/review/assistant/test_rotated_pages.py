"""A rotated page's boxes land where the page shows the label (#1724), on synthetic PDFs, $0.

pdfplumber already gives `(x, top)` on the page as displayed (`/Rotate` applied); the reader once
rotated them a second time, so every box on a rotated page was off the spot, often off the page.
Each sheet here prints one label at a known point of its content stream, then is turned by
`/Rotate` (0, 90, 180, 270), with its media box at the origin or offset. Where that point shows is
worked out independently (`PageTransform`, which applies `/Rotate` once), and three things must
land on it:

- the label's box in `dims.json` (`dim_labels.page_labels`), and a phrase's box;
- the zoom crop of that box (`set_pack.crop_picture`): its ink is in the crop's middle;
- the cloud drawn round it (`chat_drawing.markup_pdf`): red on the rendered page right round it.
"""

from __future__ import annotations

import io
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pikepdf
import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pytest
from PIL import Image
from reportlab.pdfbase.pdfmetrics import stringWidth  # type: ignore[import-untyped]

from app.review.assistant import dim_labels
from app.review.assistant.chat_drawing import markup_pdf
from app.review.assistant.set_pack import crop_picture
from evidence.coordinates import PageTransform, PdfPoint
from extraction.reader import page_frame

#: The sheet before any turn: a landscape page, in points.
WIDTH, HEIGHT = 900.0, 600.0
#: Where the label is drawn in the content stream (PDF space, from the media box's corner).
LABEL_AT = (620.0, 430.0)
LABEL = '36"'
PHRASE_AT = (120.0, 140.0)
PHRASE = "SCRIBE TO FIT"
SIZE = 12.0
#: How far a box's centre may be from where the label shows: a glyph box is the font's, not the ink's.
NEAR_PT = 4.0

ROTATIONS = (0, 90, 180, 270)
#: The media box's corner: at the origin, and offset (a sheet cut out of a larger set).
ORIGINS = ((0.0, 0.0), (-37.0, 54.0))


def _sheet(path: Path, rotate: int, origin: tuple[float, float]) -> Path:
    """One page printing `LABEL` and `PHRASE` at known points, with a dimension line under the
    label, turned by `rotate`."""
    ox, oy = origin
    lx, ly = ox + LABEL_AT[0], oy + LABEL_AT[1]
    px, py = ox + PHRASE_AT[0], oy + PHRASE_AT[1]
    content = (
        f"BT /F1 {SIZE} Tf {lx} {ly} Td ({LABEL}) Tj ET\n"
        f"BT /F1 {SIZE} Tf {px} {py} Td ({PHRASE}) Tj ET\n"
        f"0.5 w {lx - 80} {ly - 6} m {lx + 100} {ly - 6} l S\n"
    ).encode()
    pdf = pikepdf.new()
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
        )
    )
    page = pikepdf.Dictionary(
        Type=pikepdf.Name.Page,
        MediaBox=[ox, oy, ox + WIDTH, oy + HEIGHT],
        Rotate=rotate,
        Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font)),
        Contents=pdf.make_stream(content),
    )
    pdf.pages.append(pikepdf.Page(page))
    pdf.save(path)
    return path


def _shown(rotate: int, origin: tuple[float, float], x: float, y: float) -> tuple[float, float]:
    """Where a content-stream point shows on the page (points, origin top-left), by
    `PageTransform` alone: rotation applied once."""
    ox, oy = origin
    box = (Decimal(str(ox)), Decimal(str(oy)), Decimal(str(ox + WIDTH)), Decimal(str(oy + HEIGHT)))
    transform = PageTransform(dpi=7200, rotation=rotate, media_box=box, crop_box=box)
    point = transform.to_image(PdfPoint(Decimal(str(x)), Decimal(str(y))))
    return point.x / 100, point.y / 100


def _text_centre(
    rotate: int, origin: tuple[float, float], at: tuple[float, float], text: str
) -> tuple[float, float]:
    """The middle of `text`'s ink as shown: half its width along, about a third of its size up."""
    x = origin[0] + at[0] + stringWidth(text, "Helvetica", SIZE) / 2
    y = origin[1] + at[1] + SIZE * 0.35
    return _shown(rotate, origin, x, y)


def _centre(box: Any) -> tuple[float, float]:
    x0, y0, x1, y1 = (float(v) for v in box)
    return (x0 + x1) / 2, (y0 + y1) / 2


def _shown_size(rotate: int) -> tuple[float, float]:
    return (HEIGHT, WIDTH) if rotate in (90, 270) else (WIDTH, HEIGHT)


@pytest.mark.parametrize("origin", ORIGINS)
@pytest.mark.parametrize("rotate", ROTATIONS)
def test_pixel_placement_applies_the_rotation_once(
    tmp_path: Path, rotate: int, origin: tuple[float, float]
) -> None:
    """The root: a pdfplumber point placed by `page_frame` is where `PageTransform` shows the same
    content-stream point."""
    import pdfplumber

    sheet = _sheet(tmp_path / "sheet.pdf", rotate, origin)
    with pdfplumber.open(sheet) as document:
        page = document.pages[0]
        transform, frame = page_frame(page, 72)
        line = page.lines[0]
        ends = [(line["x0"], line["top"]), (line["x1"], line["bottom"])]
        pdf_ends = [frame.to_pdf(x, top) for x, top in ends]
    lx, ly = origin[0] + LABEL_AT[0], origin[1] + LABEL_AT[1]
    expected = {(lx - 80, ly - 6), (lx + 100, ly - 6)}
    assert {(round(float(p.x), 3), round(float(p.y), 3)) for p in pdf_ends} == {
        (round(a, 3), round(b, 3)) for a, b in expected
    }
    assert transform.rotation == rotate


@pytest.mark.parametrize("origin", ORIGINS)
@pytest.mark.parametrize("rotate", ROTATIONS)
def test_the_label_box_the_crop_and_the_cloud_land_on_the_label(
    tmp_path: Path, rotate: int, origin: tuple[float, float]
) -> None:
    sheet = _sheet(tmp_path / "sheet.pdf", rotate, origin)
    width, height = _shown_size(rotate)
    found = dim_labels.page_labels(sheet, 1, equal_parts=False)

    # dims.json: the label's box is on the page, round where the label shows.
    labels = [label for label in found.labels if label["text"] == LABEL]
    assert len(labels) == 1, found.labels
    box = labels[0]["box"]
    assert 0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height, box
    expected = _text_centre(rotate, origin, LABEL_AT, LABEL)
    assert _centre(box) == pytest.approx(expected, abs=NEAR_PT)

    # ...and a phrase's box too.
    phrases = [p for p in found.phrases if PHRASE in str(p["text"])]
    assert phrases, found.phrases
    assert _centre(phrases[0]["box"]) == pytest.approx(
        _text_centre(rotate, origin, PHRASE_AT, PHRASE), abs=NEAR_PT
    )

    # The zoom crop of the box holds the label's ink, in its middle.
    pad = 8.0
    area = (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)
    crop = crop_picture(sheet, 1, area, 144)
    picture = np.asarray(Image.open(io.BytesIO(crop.png)).convert("L"))
    rows, cols = np.nonzero(picture < 128)
    assert len(rows) > 20, "the crop shows no ink: it was cut from the wrong place"
    middle = (cols.mean() / picture.shape[1], rows.mean() / picture.shape[0])
    assert middle == pytest.approx((0.5, 0.5), abs=0.2)

    # The cloud drawn round the box is red right round the label on the rendered page.
    out = tmp_path / "marked.pdf"
    finding = {
        "page": 1,
        "item": "Cabinet width",
        "architect": '30"',
        "vendor": LABEL,
        "status": "different",
        "trust_level": "confirmed",
        "why": "6 in wider than the design",
        "inches": {"architect": "30", "vendor": "36", "difference": "6"},
        "vendor_box": {"page": 1, "x0": box[0], "y0": box[1], "x1": box[2], "y1": box[3]},
    }
    coords = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
    markup_pdf(sheet, [(1, finding, 1, coords, "Marked on page 1.")], out)
    document = pdfium.PdfDocument(str(out))
    try:
        rendered = document[0].render(scale=1).to_pil().convert("RGB")
    finally:
        document.close()
    pixels = np.asarray(rendered).astype(int)
    assert pixels.shape[1] == round(width) and pixels.shape[0] == round(height)
    red = (pixels[..., 0] > 170) & (pixels[..., 1] < 70) & (pixels[..., 2] < 70)
    cx, cy = _centre(box)
    near = red[
        max(0, int(cy) - 30) : int(cy) + 30,
        max(0, int(cx) - 40) : int(cx) + 40,
    ]
    assert near.sum() > 20, "no cloud round the label"
    ring_rows, ring_cols = np.nonzero(near)
    ring_centre = (
        ring_cols.mean() + max(0, int(cx) - 40),
        ring_rows.mean() + max(0, int(cy) - 30),
    )
    assert ring_centre == pytest.approx((cx, cy), abs=12)


def test_an_unrotated_page_is_placed_exactly_as_before(tmp_path: Path) -> None:
    """The unrotated frame is the old one, to the digit: `(x, height - top)` in PDF space."""
    import pdfplumber

    sheet = _sheet(tmp_path / "sheet.pdf", 0, ORIGINS[1])
    with pdfplumber.open(sheet) as document:
        page = document.pages[0]
        _, frame = page_frame(page, 72)
        for char in page.chars:
            point = frame.to_pdf(char["x0"], char["top"])
            assert point.x == Decimal(str(char["x0"]))
            assert point.y == Decimal(str(page.height)) - Decimal(str(char["top"]))


# ---- the front door (#1724) ----------------------------------------------------------------------


def _source(path: Path, name: str, kind: str = "vendor") -> Any:
    import hashlib

    from app.review.assistant.chat_service import UploadSource

    data = path.read_bytes()
    return UploadSource(
        name=name,
        sha256=hashlib.sha256(data).hexdigest(),
        kind=kind,
        pages=1,
        read=lambda: data,
    )


def test_the_chat_reads_the_working_copy_and_its_pack_is_made_from_it(tmp_path: Path) -> None:
    from app.review.assistant import set_pack
    from app.review.assistant.chat_service import sync_uploads
    from app.review.assistant.sandbox import ChatWorkspace

    turned = _sheet(tmp_path / "turned.pdf", 90, ORIGINS[1])
    upright = _sheet(tmp_path / "upright.pdf", 0, ORIGINS[0])
    workspace = ChatWorkspace(tmp_path / "chat")
    # Two files of different sides: kept apart (two vendor files are joined, see below).
    sources = [
        _source(turned, "turned.pdf"),
        _source(upright, "upright.pdf", "the architect's drawings"),
    ]
    listing, changed = sync_uploads(workspace, sources)
    assert changed
    turned_item, upright_item = listing
    # The turned file is read as its working copy: /Rotate 0, its media box at the corner.
    copy = workspace.uploads / "turned.pdf"
    with pikepdf.open(copy) as pdf:
        assert int(pdf.pages[0].obj.get("/Rotate", 0)) == 0
        assert [float(v) for v in pdf.pages[0].obj["/MediaBox"]] == [0, 0, HEIGHT, WIDTH]
    assert turned_item["working_changes"]
    assert turned_item["working_sha256"] != turned_item["sha256"]
    # The upright file is copied byte for byte and keeps its pack's name.
    assert (workspace.uploads / "upright.pdf").read_bytes() == upright.read_bytes()
    assert "working_sha256" not in upright_item
    old = [{"name": "upright.pdf", "sha256": upright_item["sha256"], "kind": upright_item["kind"]}]
    assert set_pack.pack_key([upright_item]) == set_pack.pack_key(old)
    # The turned file's pack is a new one, made from the working copy.
    assert set_pack.pack_key([turned_item]) != set_pack.pack_key(
        [{"name": "turned.pdf", "sha256": turned_item["sha256"]}]
    )
    # A second sync changes nothing.
    again, changed_again = sync_uploads(workspace, sources)
    assert not changed_again and again == listing
    # And the working copy's label box is where the original shows the label.
    found = dim_labels.page_labels(copy, 1, equal_parts=False)
    box = next(label["box"] for label in found.labels if label["text"] == LABEL)
    assert _centre(box) == pytest.approx(_text_centre(90, ORIGINS[1], LABEL_AT, LABEL), abs=NEAR_PT)


def test_several_shop_drawing_files_are_one_vendor_file_their_pages_numbered_on(
    tmp_path: Path,
) -> None:
    from app.review.assistant.chat_findings import main_upload
    from app.review.assistant.chat_service import sync_uploads, uploaded_files_text
    from app.review.assistant.sandbox import ChatWorkspace

    cabinets = _sheet(tmp_path / "cabinets.pdf", 0, ORIGINS[0])
    tops = _sheet(tmp_path / "tops.pdf", 90, ORIGINS[1])
    design = _sheet(tmp_path / "design.pdf", 0, ORIGINS[0])
    workspace = ChatWorkspace(tmp_path / "chat")
    shop = "the vendor's shop drawings (a sheet may carry the architect's drawing beside it)"
    earlier = "the vendor's previous round of shop drawings (round 1)"
    sources = [
        _source(cabinets, "cabinets.pdf", shop),
        _source(design, "design.pdf", "the architect's drawings"),
        _source(tops, "tops.pdf", shop),
        _source(design, "round1.pdf", earlier),
    ]
    listing, changed = sync_uploads(workspace, sources)
    assert changed
    # An earlier round stays its own file (the round-2 check follows it).
    assert [item["name"] for item in listing] == [
        "Shop drawings (2 files joined).pdf",
        "design.pdf",
        "round1.pdf",
    ]
    joined = listing[0]
    assert main_upload(listing) is joined and joined["pages"] == 2
    assert [(p["name"], p["first_page"], p["pages"]) for p in joined["joined_from"]] == [
        ("cabinets.pdf", 1, 1),
        ("tops.pdf", 2, 1),
    ]
    # One PDF, each file's working copy in upload order: the turned page is upright here too.
    with pikepdf.open(workspace.uploads / joined["name"]) as pdf:
        assert len(pdf.pages) == 2
        assert int(pdf.pages[1].obj.get("/Rotate", 0)) == 0
    assert joined["intake_facts"]["pages"].keys() == {"1", "2"}
    assert 'joined_from="pages 1-1: cabinets.pdf; pages 2-2: tops.pdf"' in uploaded_files_text(
        listing, changed=False
    )
    # A second sync changes nothing; a changed file makes a new joined file.
    again, changed_again = sync_uploads(workspace, sources)
    assert not changed_again and again == listing
    sources[2] = _source(cabinets, "tops.pdf", shop)
    other, changed = sync_uploads(workspace, sources)
    assert changed and other[0]["sha256"] != joined["sha256"]
