"""The per-page health check (#1745): one synthetic page per fact, its value and its sentence, $0."""

from __future__ import annotations

import io
from typing import Any

import numpy as np
import pikepdf
import pytest
from PIL import Image

from extraction.page_health import page_health
from extraction.working_copy import working_copy

WIDTH, HEIGHT = 600.0, 400.0
TYPED = b"".join(
    f"0 g BT /F1 9 Tf 40 {380 - 12 * n} Td (LINE {n} 24 1/2) Tj ET\n".encode() for n in range(4)
)


def _pdf(content: bytes, *, rotate: int = 0, setup: Any = None, **save: Any) -> bytes:
    pdf = pikepdf.new()
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
        )
    )
    page = pikepdf.Dictionary(
        Type=pikepdf.Name.Page,
        MediaBox=[0, 0, WIDTH, HEIGHT],
        Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font)),
        Contents=pdf.make_stream(content),
    )
    if rotate:
        page["/Rotate"] = rotate
    pdf.pages.append(pikepdf.Page(page))
    if setup is not None:
        setup(pdf, pdf.pages[0])
    out = io.BytesIO()
    pdf.save(out, **save)
    return out.getvalue()


def _health(data: bytes) -> dict[str, Any]:
    """Through the front door, as the set pack sees a page."""
    copy = working_copy(data)
    item = {
        "intake_facts": copy.facts,
        "pages_not_read": [{"page": p, "why": w} for p, w in copy.not_read],
    }
    return page_health(copy.data, "a.pdf", 1, item)


def _says(health: dict[str, Any], words: str) -> bool:
    return any(words in sentence for sentence in health["says"])


def test_a_plain_typed_page() -> None:
    health = _health(_pdf(TYPED))
    facts = health["facts"]
    assert facts["typed_text"] > 20 and facts["hidden_text"] == 0
    assert facts["size_in"] == [round(WIDTH / 72, 1), round(HEIGHT / 72, 1)]
    assert health["says"] == ["This page has typed text: numbers are read directly."]


def test_a_turned_page_says_it_was_straightened() -> None:
    health = _health(_pdf(TYPED, rotate=270))
    assert health["facts"]["turned"] == 270
    assert _says(health, "stored turned 270 degrees; it was straightened")


def test_hidden_text_and_layers_off_are_counted_and_said() -> None:
    def layer(pdf: pikepdf.Pdf, page: pikepdf.Page) -> None:
        group = pdf.make_indirect(
            pikepdf.Dictionary(Type=pikepdf.Name.OCG, Name=pikepdf.String("A-ANNO-OLD"))
        )
        pdf.Root.OCProperties = pikepdf.Dictionary(OCGs=[group], D=pikepdf.Dictionary(OFF=[group]))
        page.obj["/Resources"]["/Properties"] = pikepdf.Dictionary(L1=group)

    hidden = (
        b'/OC /L1 BDC 0 g BT /F1 10 Tf 300 200 Td (99") Tj ET EMC\n'
        b'q 0 g BT 3 Tr /F1 10 Tf 300 150 Td (77") Tj ET Q\n'
        b'0 g BT /F1 10 Tf 300 100 Td (55") Tj ET 1 g 290 90 60 30 re f\n'
    )
    health = _health(_pdf(TYPED + hidden, setup=layer))
    facts = health["facts"]
    assert facts["hidden_text"] == 9 and facts["layers_off"] == ["A-ANNO-OLD"]
    assert facts["invisible_objects"] == 1 and facts["shx_hidden_text"] == 0
    assert _says(health, "9 characters on this page are in the file but not visible")
    assert _says(health, "Layers switched off in this file: A-ANNO-OLD")


def test_autocad_shx_text_is_said_to_be_used() -> None:
    strokes = b"0 0 0 RG 0.6 w " + b"".join(
        f"{300 + 5 * n} 200 m {303 + 5 * n} 209 l S ".encode() for n in range(5)
    )
    hidden = b'0 g BT 3 Tr /F1 10 Tf 300 200 Td (24") Tj ET\n'

    def comment(pdf: pikepdf.Pdf, page: pikepdf.Page) -> None:
        page.obj["/Annots"] = pikepdf.Array(
            [
                pdf.make_indirect(
                    pikepdf.Dictionary(
                        Type=pikepdf.Name.Annot,
                        Subtype=pikepdf.Name.Text,
                        Rect=[300, 200, 325, 209],
                        T=pikepdf.String("AutoCAD SHX Text"),
                        Contents=pikepdf.String('24"'),
                    )
                )
            ]
        )

    health = _health(_pdf(TYPED + strokes + hidden, setup=comment))
    facts = health["facts"]
    assert facts["shx_hidden_text"] == 1 and facts["shx_comments"] == 1
    assert facts["hidden_text"] == 0 and facts["markups"] == 0
    assert _says(health, "AutoCAD stored the real text behind the drawn letters; it was used.")


def test_markups_are_counted_and_a_bluebeam_file_in_stages_is_a_warning_only() -> None:
    def markup(pdf: pikepdf.Pdf, page: pikepdf.Page) -> None:
        page.obj["/Annots"] = pikepdf.Array(
            [
                pdf.make_indirect(
                    pikepdf.Dictionary(
                        Type=pikepdf.Name.Annot,
                        Subtype=pikepdf.Name.FreeText,
                        Rect=[100, 100, 200, 130],
                        Contents=pikepdf.String("Check"),
                    )
                )
            ]
        )
        pdf.docinfo["/Producer"] = pikepdf.String("Bluebeam PDF Library 21")

    data = _pdf(TYPED, setup=markup)
    staged = data + b"\n%%EOF\n"  # a second save's end marker, as an incremental save leaves
    health = _health(staged)
    facts = health["facts"]
    assert facts["annotations"] == {"FreeText": 1} and facts["markups"] == 1
    assert facts["revisions"] == 2 and facts["flattened_markup"] is True
    assert _says(health, "1 markups on this page were not read")
    assert _says(health, "may contain markups merged into the drawing")
    assert _says(health, "saved in several stages")


def test_drawn_lettering_is_said() -> None:
    strokes = b"0 0 0 RG 0.5 w " + b"".join(
        f"{20 + 6 * (n % 80)} {20 + 14 * (n // 80)} m {23 + 6 * (n % 80)} {29 + 14 * (n // 80)} l S ".encode()
        for n in range(400)
    )
    health = _health(_pdf(strokes))
    assert health["facts"]["drawn_letters"] is True
    assert _says(health, "drawn with lines, not typed")


def test_a_scanned_page_is_said() -> None:
    def picture(pdf: pikepdf.Pdf, page: pikepdf.Page) -> None:
        pixels = np.full((200, 300), 255, dtype=np.uint8)
        pixels[50:150, 50:250] = 0
        image = pikepdf.Stream(pdf, Image.fromarray(pixels).tobytes())
        image["/Type"] = pikepdf.Name.XObject
        image["/Subtype"] = pikepdf.Name.Image
        image["/Width"], image["/Height"] = 300, 200
        image["/ColorSpace"] = pikepdf.Name.DeviceGray
        image["/BitsPerComponent"] = 8
        page.obj["/Resources"]["/XObject"] = pikepdf.Dictionary(Im0=image)

    health = _health(_pdf(f"q {WIDTH} 0 0 {HEIGHT} 0 0 cm /Im0 Do Q".encode(), setup=picture))
    assert health["facts"]["picture_share"] == 1.0 and health["facts"]["typed_text"] == 0
    assert _says(health, "This page is a scanned picture")


def test_a_font_without_a_letter_map_is_said() -> None:
    def type0(pdf: pikepdf.Pdf, page: pikepdf.Page) -> None:
        page.obj["/Resources"]["/Font"]["/F2"] = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Font,
                Subtype=pikepdf.Name.Type0,
                BaseFont=pikepdf.Name("/CADFont-Identity-H"),
                Encoding=pikepdf.Name("/Identity-H"),
                DescendantFonts=[
                    pikepdf.Dictionary(
                        Type=pikepdf.Name.Font,
                        Subtype=pikepdf.Name.CIDFontType2,
                        BaseFont=pikepdf.Name("/CADFont"),
                        CIDSystemInfo=pikepdf.Dictionary(
                            Registry=pikepdf.String("Adobe"),
                            Ordering=pikepdf.String("Identity"),
                            Supplement=0,
                        ),
                        DW=1000,
                    )
                ],
            )
        )

    health = _health(_pdf(TYPED, setup=type0))
    assert health["facts"]["no_letter_map"] == ["CADFont-Identity-H"]
    assert _says(health, "stored as codes without a letter map")


@pytest.mark.parametrize("signed", [True, False])
def test_a_signed_original_is_said(signed: bool) -> None:
    def form(pdf: pikepdf.Pdf, page: pikepdf.Page) -> None:
        if signed:
            pdf.Root.AcroForm = pikepdf.Dictionary(Fields=[], SigFlags=3)

    health = _health(_pdf(TYPED, setup=form))
    assert health["facts"]["signed"] is signed
    assert _says(health, "digital signature is untouched") is signed


def test_a_page_that_could_not_be_read_says_only_why() -> None:
    def broken(pdf: pikepdf.Pdf, page: pikepdf.Page) -> None:
        page.obj["/CropBox"] = pikepdf.Array([100, 100, 100, 300])

    health = _health(_pdf(TYPED, setup=broken))
    assert health["says"] == [
        "Page 1 could not be read: its visible area is empty (its page box is broken)."
    ]
