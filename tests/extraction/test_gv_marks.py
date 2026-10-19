"""GV Review's own marks are signed and never read back as the vendor's drawing, $0.

A cloud drawing GV sends can come back in round 2 with GV's clouds and notes still on the page.
These tests draw one on a synthetic sheet, then read it as the next round would: the vendor's
labels are read, GV's notes (which print values with inch marks) are not, **whatever their
colour**: the readers are made colour-blind here, so only the signature can be what leaves them out.
"""

from __future__ import annotations

import io
from pathlib import Path

import pikepdf
import pytest
from pypdf import PdfReader
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from app.review.assistant import dim_labels, set_pack
from app.review.assistant import verify_value as vv
from app.review.assistant.chat_drawing import markup_pdf
from extraction import gv_marks

WIDTH, HEIGHT = 900.0, 600.0


def _sheet(path: Path) -> Path:
    """A vendor sheet: two dimensions on their lines, one of them in red (the vendor's own ink)."""
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(WIDTH, HEIGHT), invariant=1)
    c.setLineWidth(0.5)
    for y, x0, x1, text, colour in (
        (400, 200, 400, '36"', (0, 0, 0)),
        (250, 500, 700, '24"', (1, 0, 0)),
    ):
        c.setStrokeColorRGB(*colour)
        c.setFillColorRGB(*colour)
        c.line(x0, y, x1, y)
        c.line(x0, y - 40, x0, y + 4)
        c.line(x1, y - 40, x1, y + 4)
        c.setFont("Helvetica", 10)
        c.drawString((x0 + x1) / 2 - 8, y + 3, text)
    c.showPage()
    c.save()
    path.write_bytes(out.getvalue())
    return path


def _marked(tmp_path: Path) -> tuple[Path, Path]:
    sheet = _sheet(tmp_path / "sheet.pdf")
    finding = {
        "page": 1,
        "item": "Cabinet width",
        "architect": '30"',
        "vendor": '36"',
        "status": "different",
        "trust_level": "confirmed",
        "why": "6 in wider than the design: should be 30 in",
        "inches": {"architect": "30", "vendor": "36", "difference": "6"},
        "vendor_box": {"page": 1, "x0": 290.0, "y0": 186.0, "x1": 312.0, "y1": 199.0},
    }
    out = tmp_path / "marked.pdf"
    markup_pdf(sheet, [(1, finding, 1, (290.0, 186.0, 312.0, 199.0), "Marked on page 1.")], out)
    return sheet, out


@pytest.fixture
def colour_blind(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every colour read as the drawing's ink: colour decides nothing in these tests."""
    monkeypatch.setattr(vv, "drawing_ink", lambda char: True)
    vv._PAGE_CACHE.clear()


def _labels(pdf: Path) -> list[str]:
    return sorted(str(label["text"]) for label in dim_labels.page_labels(pdf, 1).labels)


def _phrases(pdf: Path) -> list[str]:
    return [str(p["text"]) for p in dim_labels.page_labels(pdf, 1).phrases]


def test_the_cloud_drawing_is_signed_and_in_its_own_layer(tmp_path: Path) -> None:
    _, marked = _marked(tmp_path)
    with pikepdf.open(marked) as pdf:
        page = pdf.pages[0]
        xobjects = page.obj["/Resources"]["/XObject"]
        signed = [n for n in xobjects if gv_marks.is_gv_mark(str(n), xobjects[n])]
        assert signed and gv_marks.PIECE_KEY in xobjects[signed[0]]["/PieceInfo"]
        layers = [str(g.get("/Name")) for g in pdf.Root.OCProperties.OCGs]
        assert gv_marks.LAYER_NAME in layers
        assert b"/OC /GVReviewMarkup BDC" in page.obj["/Contents"][-1].read_bytes()


def test_a_marked_page_that_comes_back_reads_as_the_vendor_drew_it(
    tmp_path: Path, colour_blind: None
) -> None:
    sheet, marked = _marked(tmp_path)
    # GV's notes print values with inch marks; colour-blind, only the signature leaves them out.
    assert _labels(marked) == _labels(sheet) == ['24"', '36"']
    assert not any("make it" in phrase or "AI markup" in phrase for phrase in _phrases(marked))


def test_without_the_signature_the_notes_would_be_read(tmp_path: Path, colour_blind: None) -> None:
    """The control: the same marked page with the signature and the reserved name taken off is
    read with GV's notes in it, so the test above is the signature's doing."""
    _, marked = _marked(tmp_path)
    with pikepdf.open(marked, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        xobjects = page.obj["/Resources"]["/XObject"]
        for name in list(xobjects.keys()):
            form = xobjects[name]
            if "/PieceInfo" in form:
                del form["/PieceInfo"]
                xobjects["/Plain1"] = form
                del xobjects[name]
                contents = page.obj["/Contents"]
                last = contents[-1]
                last.write(last.read_bytes().replace(name.encode(), b"/Plain1"))
        pdf.save(marked)
    vv._PAGE_CACHE.clear()
    assert any("make it" in phrase or "AI markup" in phrase for phrase in _phrases(marked))


def test_an_older_unsigned_overlay_is_known_by_its_reserved_name(
    tmp_path: Path, colour_blind: None
) -> None:
    sheet = _sheet(tmp_path / "sheet.pdf")
    with pikepdf.open(sheet, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        note = pdf.make_stream(b'1 0 0 rg BT /F1 10 Tf 600 500 Td (48") Tj ET')
        note["/Type"] = pikepdf.Name.XObject
        note["/Subtype"] = pikepdf.Name.Form
        note["/BBox"] = [0, 0, WIDTH, HEIGHT]
        note["/Resources"] = page.obj["/Resources"]
        page.add_resource(note, pikepdf.Name.XObject, name=pikepdf.Name("/GVCloudDrawing"))
        page.contents_add(b"q /GVCloudDrawing Do Q\n")
        pdf.save(sheet)
    with pikepdf.open(sheet) as pdf:
        overlay = pdf.pages[0].obj["/Resources"]["/XObject"]["/GVCloudDrawing"]
        assert gv_marks.is_gv_mark("/GVCloudDrawing", overlay)
    assert _labels(sheet) == ['24"', '36"']


def test_an_annotation_is_never_read_as_the_vendors_text(
    tmp_path: Path, colour_blind: None
) -> None:
    sheet = _sheet(tmp_path / "sheet.pdf")
    with pikepdf.open(sheet, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        appearance = pdf.make_stream(b'0 g BT /F1 10 Tf 2 4 Td (99") Tj ET')
        appearance["/Type"] = pikepdf.Name.XObject
        appearance["/Subtype"] = pikepdf.Name.Form
        appearance["/BBox"] = [0, 0, 40, 16]
        appearance["/Resources"] = page.obj["/Resources"]
        page.obj["/Annots"] = pikepdf.Array(
            [
                pdf.make_indirect(
                    pikepdf.Dictionary(
                        Type=pikepdf.Name.Annot,
                        Subtype=pikepdf.Name.FreeText,
                        Rect=[290, 410, 330, 426],
                        Contents=pikepdf.String('99"'),
                        AP=pikepdf.Dictionary(N=appearance),
                    )
                )
            ]
        )
        pdf.save(sheet)
    assert '99"' not in _labels(sheet)


def test_the_drawings_only_copy_hides_gv_marks(tmp_path: Path) -> None:
    _, marked = _marked(tmp_path)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "marked.pdf").write_bytes(marked.read_bytes())
    copy = set_pack.drawings_only(tmp_path, uploads, "marked.pdf")
    reader = PdfReader(str(copy))
    text = reader.pages[0].extract_text()
    assert "make it" not in text and "AI markup" not in text
    assert '36"' in text


def test_the_redline_overlay_is_known_by_its_reserved_name() -> None:
    """The redline writer's overlay keeps its byte-exact output (its golden test) and is known by
    its reserved resource name, which no vendor drawing uses."""
    from reports import redline

    source = Path(redline.__file__).read_text(encoding="utf-8")
    assert 'NameObject("/GVRedline939")' in source
    assert "/GVRedline939" in gv_marks.RESERVED_NAMES
