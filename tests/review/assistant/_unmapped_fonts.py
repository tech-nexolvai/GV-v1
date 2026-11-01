"""PDFs with text whose fonts have no letter map, made for the tests (#1750 item 3)."""

from __future__ import annotations

import io
import struct
from pathlib import Path

import pikepdf
import reportlab
from reportlab.pdfbase.ttfonts import TTFontFile

from tests.review.assistant.test_ocr_labels import _ADVANCE, _STROKES

#: The stroke font's extra characters: a slash and the digits the test font leaves out.
STROKES = {
    **_STROKES,
    "/": [[(0.5, -0.5), (5, 9.5)]],
    "5": [[(5.5, 9), (0.8, 9), (0.4, 5), (2, 5.8), (3.5, 5.8), (5, 5.2), (6, 3.5), (5.6, 1.2)]
          + [(4.2, 0.1), (2.5, 0), (0.8, 0.6)]],
    "9": [[(5.7, 5.8), (4.6, 4.2), (3, 3.8), (1.4, 4.2), (0.3, 5.5), (0.3, 7.5), (1.4, 8.8)]
          + [(3, 9), (4.6, 8.8), (5.7, 7.5), (5.7, 4), (5, 1.4), (3.5, 0), (1.5, 0.2)]],
}  # fmt: skip
#: Font units a stroke-font unit (the cell is 6 x 9: letters 720 units tall, 1000 a point size).
_UNIT = 80


def _type3(pdf: pikepdf.Pdf, alphabet: str) -> pikepdf.Dictionary:
    """A Type3 font drawing `alphabet` as pen strokes, its glyphs named `g1`, `g2`... (names that
    say nothing about the letter) and no `ToUnicode`: the PDF reader gives `(cid:N)`."""
    procs = pikepdf.Dictionary()
    widths = []
    for number, char in enumerate(alphabet, start=1):
        advance = int(_ADVANCE.get(char, 7.5) * _UNIT)
        body = f"{advance} 0 d0 60 w 1 J 1 j ".encode()
        for stroke in STROKES[char]:
            (x, y), rest = stroke[0], stroke[1:]
            body += f"{x * _UNIT:.0f} {y * _UNIT:.0f} m ".encode()
            body += b"".join(f"{a * _UNIT:.0f} {b * _UNIT:.0f} l ".encode() for a, b in rest)
            body += b"S "
        procs[f"/g{number}"] = pikepdf.Stream(pdf, body)
        widths.append(advance)
    return pikepdf.Dictionary(
        Type=pikepdf.Name.Font,
        Subtype=pikepdf.Name.Type3,
        FontBBox=[0, -100, 1000, 900],
        FontMatrix=[0.001, 0, 0, 0.001, 0, 0],
        CharProcs=procs,
        Encoding=pikepdf.Dictionary(
            Type=pikepdf.Name.Encoding,
            Differences=[1, *(pikepdf.Name(f"/g{n}") for n in range(1, len(alphabet) + 1))],
        ),
        FirstChar=1,
        LastChar=len(alphabet),
        Widths=widths,
        Resources=pikepdf.Dictionary(),
    )


def _no_unicode_cmap(data: bytes) -> bytes:
    """A TrueType font whose character maps are all marked Macintosh-Roman (platform 1): the PDF
    reader takes none of them for letters, as with a subset font whose only map is a symbol map;
    the glyphs (drawn by their numbers) are untouched."""
    font = bytearray(data)
    tables = struct.unpack(">H", font[4:6])[0]
    for index in range(tables):
        entry = 12 + 16 * index
        if bytes(font[entry : entry + 4]) == b"cmap":
            offset = struct.unpack(">L", font[entry + 8 : entry + 12])[0]
            count = struct.unpack(">H", font[offset + 2 : offset + 4])[0]
            for sub in range(count):
                at = offset + 4 + 8 * sub
                font[at : at + 4] = struct.pack(">HH", 1, 0)
    return bytes(font)


def _vera() -> tuple[bytes, dict[int, int], dict[int, int]]:
    path = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"
    parsed = TTFontFile(str(path))
    return path.read_bytes(), dict(parsed.charToGlyph), dict(parsed.charWidths)


def _type0(pdf: pikepdf.Pdf) -> tuple[pikepdf.Dictionary, dict[int, int]]:
    """A Type0 font (Bitstream Vera embedded whole, codes are glyph numbers) with no `ToUnicode`
    and no character map the PDF reader uses: it gives `(cid:N)`. Also each letter's code."""
    data, glyphs, widths = _vera()
    file = pikepdf.Stream(pdf, _no_unicode_cmap(data))
    file["/Length1"] = len(data)
    descriptor = pikepdf.Dictionary(
        Type=pikepdf.Name.FontDescriptor,
        FontName=pikepdf.Name.BitstreamVeraSans,
        Flags=32,
        FontBBox=[-183, -236, 1287, 928],
        ItalicAngle=0,
        Ascent=928,
        Descent=-236,
        CapHeight=729,
        StemV=80,
        FontFile2=file,
    )
    used = sorted({glyphs[ord(c)] for c in "0123456789/\"' -"})
    by_glyph = {glyphs[ord(c)]: widths.get(ord(c), 600) for c in "0123456789/\"' -"}
    descendant = pikepdf.Dictionary(
        Type=pikepdf.Name.Font,
        Subtype=pikepdf.Name.CIDFontType2,
        BaseFont=pikepdf.Name.BitstreamVeraSans,
        CIDSystemInfo=pikepdf.Dictionary(Registry="Adobe", Ordering="Identity", Supplement=0),
        FontDescriptor=descriptor,
        CIDToGIDMap=pikepdf.Name.Identity,
        DW=600,
        W=[x for gid in used for x in (gid, [by_glyph[gid]])],
    )
    font = pikepdf.Dictionary(
        Type=pikepdf.Name.Font,
        Subtype=pikepdf.Name.Type0,
        BaseFont=pikepdf.Name.BitstreamVeraSans,
        Encoding=pikepdf.Name("/Identity-H"),
        DescendantFonts=[descendant],
    )
    return font, {ord(c): glyphs[ord(c)] for c in "0123456789/\"' -"}


ALPHABET = "0123456789/\"'-"


def type3_text(text: str, x: float, y: float, size: float = 8.0, *, up: bool = False) -> bytes:
    """`text` shown in the Type3 font (`/T3`) from (x, y), PDF space (`up`: reading up the page),
    one character at a time (a space is a gap: the font has no glyph for it)."""
    out = b""
    pen = 0.0
    for char in text:
        if char != " ":
            matrix = f"0 1 -1 0 {x} {y + pen}" if up else f"1 0 0 1 {x + pen} {y}"
            out += f"BT /T3 {size} Tf {matrix} Tm <{ALPHABET.index(char) + 1:02x}> Tj ET ".encode()
        pen += (3.5 if char == " " else _ADVANCE.get(char, 7.5)) * _UNIT / 1000 * size
    return out


def type0_text(
    text: str, x: float, y: float, glyphs: dict[int, int], size: float = 8.0, *, up: bool = False
) -> bytes:
    """`text` shown in the Type0 font (`/T0`) as two-byte glyph numbers, one show for the row."""
    matrix = f"0 1 -1 0 {x} {y}" if up else f"1 0 0 1 {x} {y}"
    codes = "".join(f"{glyphs[ord(c)]:04x}" for c in text)
    return f"BT /T0 {size} Tf {matrix} Tm <{codes}> Tj ET ".encode()


def write_pdf(path: Path, body: bytes, *, width: float = 600, height: float = 400) -> Path:
    """One page of `body` with Helvetica (`/F1`), the Type3 font (`/T3`) and the Type0 font
    (`/T0`) as its fonts."""
    pdf = pikepdf.new()
    type0, _glyphs = _type0(pdf)
    resources = pikepdf.Dictionary(
        Font=pikepdf.Dictionary(
            F1=pikepdf.Dictionary(
                Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
            ),
            T3=pdf.make_indirect(_type3(pdf, ALPHABET)),
            T0=pdf.make_indirect(type0),
        )
    )
    page = pikepdf.Dictionary(
        Type=pikepdf.Name.Page,
        MediaBox=[0, 0, width, height],
        Resources=resources,
        Contents=pikepdf.Stream(pdf, body),
    )
    pdf.pages.append(pikepdf.Page(page))
    buffer = io.BytesIO()
    pdf.save(buffer)
    path.write_bytes(buffer.getvalue())
    return path


def type0_glyphs() -> dict[int, int]:
    """Each letter's glyph number in the Type0 font (its code in `type0_text`)."""
    return _type0(pikepdf.new())[1]
