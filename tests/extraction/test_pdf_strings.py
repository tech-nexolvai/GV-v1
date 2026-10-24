"""A backslash before an ordinary character in PDF text keeps the character (`extraction.pdf_strings`).

pdfminer.six drops it, so a CAD label written `(15 1\\/2")` read as `15 12"`. Every PDF here is a
small synthetic one made in the test.
"""

from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path

import pdfplumber
import pytest
from pdfminer import psparser

from extraction import pdf_strings

ROOT = Path(__file__).resolve().parents[2]
#: The packages whose `__init__` installs the fix.
PACKAGES = ("app", "extraction", "workflow", "reports")


def _token(raw: bytes) -> bytes:
    _, token = psparser.PSBaseParser(BytesIO(raw)).nexttoken()
    assert isinstance(token, bytes)
    return token


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        (rb'(15 1\/2")', b'15 1/2"'),
        (rb"(\[394mm\])", b"[394mm]"),
        (rb'(5\/32" \[4mm\])', b'5/32" [4mm]'),
        (rb"(\a\z)", b"az"),
        # The PDF reference's own escapes read as before.
        (rb"(a\nb\tc)", b"a\nb\tc"),
        (rb"(\(x\) \\)", b"(x) \\"),
        (rb"(a\053b)", b"a+b"),
        (rb"(\5x)", b"\x05x"),
        # A backslash before a line end joins two lines.
        (b"(ab\\\ncd)", b"abcd"),
        (b"(ab\\\r\ncd)", b"abcd"),
        (b"(ab\\\rcd)", b"abcd"),
    ],
)
def test_an_escaped_ordinary_character_is_kept(raw: bytes, text: bytes) -> None:
    assert _token(raw) == text


def test_the_fix_is_installed_once_and_only_while_pdfminer_needs_it() -> None:
    assert pdf_strings.installed() and not pdf_strings.drops_escaped()
    replacement = psparser.PSBaseParser._parse_string_1
    pdf_strings.install()
    assert psparser.PSBaseParser._parse_string_1 is replacement


def _pdf(content: bytes) -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(start).encode() + b"\n%%EOF\n"
    return bytes(out)


def test_pdfplumber_reads_the_slash_and_brackets_a_cad_program_escaped(tmp_path: Path) -> None:
    path = tmp_path / "escaped.pdf"
    path.write_bytes(_pdf(rb'BT /F1 10 Tf 1 0 0 1 20 100 Tm (15 1\/2" \[394mm\]) Tj ET'))
    with pdfplumber.open(path) as pdf:
        assert pdf.pages[0].extract_text() == '15 1/2" [394mm]'


def test_every_module_that_opens_a_pdf_with_pdfminer_gets_the_fix() -> None:
    for package in PACKAGES:
        init = (ROOT / package / "__init__.py").read_text()
        assert "from extraction import pdf_strings" in init, package
    uses = re.compile(r"^\s*(?:import|from) (?:pdfplumber|pdfminer)\b", re.MULTILINE)
    ours = re.compile(rf"^\s*(?:import|from) (?:{'|'.join(PACKAGES)})\b", re.MULTILINE)
    missing = [
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("*.py")
        if not {"tests", ".venv", "node_modules", "frontend"} & set(path.relative_to(ROOT).parts)
        and path.relative_to(ROOT).parts[0] not in PACKAGES
        and uses.search(text := path.read_text(errors="ignore"))
        and not ours.search(text)
    ]
    assert missing == []
