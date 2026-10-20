"""Separate uploads: a page is a drawing by itself, decided page by page (#1772). Synthetic, $0.

The probe cases of the final review:

- **a stamp on one page of a separate file** (the vendor's or the architect's approval stamp) must
  not switch off the whole-page drawings of the whole file, and a stamp never decides a page;
- **a page whose numbers are pen strokes only** (no text) is a drawing: the drawn route's labels
  count, not only the raw text layer;
- **a page whose numbers are AutoCAD SHX comments** is a drawing, and the drawings-only copy
  keeps them as the vendor's text (as `verify_value` reads them);
- a page of words with a stamp on it stays a general page, and GV's combined sheets are read as
  before (`test_set_pack_roles.py`).
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pikepdf
import pytest
from pypdf import PdfReader, PdfWriter

from app.review.assistant import dim_labels, set_pack
from tests.review.assistant.separate_files import Sheet, cover, plain_pdf
from tests.review.assistant.stroke_text import StrokeReader, stroke_ops
from tests.review.assistant.test_dim_labels import _dimension, _pdf
from tests.review.assistant.test_set_pack import _annotation
from tests.review.assistant.test_set_pack_roles import ARCHITECT_KIND, SHOP_KIND

STAMP = (980.0, 40.0, 1180.0, 140.0)  # an approval stamp in the title-block corner


def _stamped(data: bytes, pages: Sequence[int], rect: Sequence[float] = STAMP) -> bytes:
    """`data` with an approval stamp (a `/Stamp` annotation, no GV heading) on `pages` (from 1)."""
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    for number in pages:
        _annotation(writer, writer.pages[number - 1], "Stamp", rect, colour=b"0.8 0.1 0.1")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _listing(uploads: Path, *files: tuple[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "sha256": hashlib.sha256((uploads / name).read_bytes()).hexdigest(),
            "pages": len(PdfReader(str(uploads / name)).pages),
            "kind": kind,
        }
        for name, kind in files
    ]


def _pack(tmp_path: Path, uploads: Path, listing: list[dict[str, Any]]) -> Path:
    folder = set_pack.pack_folder(tmp_path / "chats", listing)
    set_pack.ensure_pack(folder, uploads, listing, background=False)
    assert set_pack.read_status(folder)["state"] == "ready"
    return folder


def _read(folder: Path, name: str) -> Any:
    return json.loads((folder / name).read_text())


def _roles(page_map: dict[str, Any]) -> list[tuple[str, int, list[str]]]:
    return [
        (entry["file"], entry["page"], [half["role"] for half in entry["halves"]])
        for entry in page_map["pages"]
    ]


@pytest.fixture
def uploads(tmp_path: Path) -> Path:
    folder = tmp_path / "uploads"
    folder.mkdir()
    return folder


# ---- B1: a stamp never switches a file off ------------------------------------------------------


def test_a_vendor_stamp_on_one_page_keeps_every_page_a_drawing(
    tmp_path: Path, uploads: Path
) -> None:
    shop = plain_pdf([Sheet("VENDOR ELEVATION", (24, 30, 18)), Sheet("CABINET RUN 2", (36, 36))])
    (uploads / "shop.pdf").write_bytes(_stamped(shop, [1]))
    (uploads / "arch.pdf").write_bytes(plain_pdf([Sheet("ARCH ELEVATION", (24, 30, 18))]))
    listing = _listing(uploads, ("shop.pdf", SHOP_KIND), ("arch.pdf", ARCHITECT_KIND))
    page_map = _read(_pack(tmp_path, uploads, listing), "page_map.json")
    assert _roles(page_map) == [
        ("shop.pdf", 1, ["vendor"]),
        ("shop.pdf", 2, ["vendor"]),
        ("arch.pdf", 1, ["architect"]),
    ]
    # The stamped page is one whole-page drawing: the stamp is not a drawing of its own.
    assert page_map["pages"][0]["halves"][0]["box"] == [0.0, 0.0, 1224.0, 792.0]
    assert page_map["upload_roles"] == {"shop.pdf": "vendor", "arch.pdf": "architect"}


def test_an_architect_stamp_keeps_the_architects_pages_drawings(
    tmp_path: Path, uploads: Path
) -> None:
    arch = plain_pdf([cover(), Sheet("ARCH ELEVATION", (24, 30, 18))])
    (uploads / "arch.pdf").write_bytes(_stamped(arch, [1, 2]))
    (uploads / "shop.pdf").write_bytes(plain_pdf([Sheet("VENDOR ELEVATION", (24, 30, 18))]))
    listing = _listing(uploads, ("shop.pdf", SHOP_KIND), ("arch.pdf", ARCHITECT_KIND))
    folder = _pack(tmp_path, uploads, listing)
    page_map = _read(folder, "page_map.json")
    assert _roles(page_map)[1:] == [
        # The stamped cover prints no dimension: its stamp is the only drawing, as before.
        ("arch.pdf", 1, ["architect"]),
        ("arch.pdf", 2, ["architect"]),
    ]
    assert page_map["pages"][2]["halves"][0]["box"] == [0.0, 0.0, 1224.0, 792.0]
    dims = _read(folder, "dims.json")
    sides = {
        (e["file"], e["page"]): {label["side"] for label in e["labels"]}
        for e in dims["pages"]
        if e["labels"]
    }
    assert sides == {("shop.pdf", 1): {"vendor"}, ("arch.pdf", 2): {"architect"}}


def test_a_page_of_words_with_a_stamp_stays_general(tmp_path: Path, uploads: Path) -> None:
    shop = plain_pdf([cover(), Sheet("VENDOR ELEVATION", (24, 30, 18))])
    (uploads / "shop.pdf").write_bytes(_stamped(shop, [1]))
    page_map = _read(
        _pack(tmp_path, uploads, _listing(uploads, ("shop.pdf", SHOP_KIND))), "page_map.json"
    )
    # The stamp on the cover is no vendor drawing (no GV heading names it), and the cover prints
    # no dimension: no drawing is compared there.
    assert _roles(page_map) == [("shop.pdf", 1, ["general"]), ("shop.pdf", 2, ["vendor"])]
    assert page_map["pages"][0]["role"] == "general"
    assert "candidate" not in page_map["pages"][0]


# ---- B2: every reading route counts -------------------------------------------------------------


def _pen_page() -> bytes:
    """A 600 x 400 page whose three dimensions are drawn as pen strokes only (no text)."""
    body = b""
    for x0, x1, y, value in (
        (100, 220, 300, '24"'),
        (300, 420, 200, '30"'),
        (150, 270, 100, '18"'),
    ):
        body += _dimension(x0, x1, y) + stroke_ops(value, (x0 + x1) / 2 - 8, y + 2, height=9)
    return body


def _engines() -> list[Any]:
    return [StrokeReader("RapidOCR"), StrokeReader("Tesseract")]


def test_a_pen_stroke_only_vendor_page_is_a_drawing(
    tmp_path: Path, uploads: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GV_DIM_LABELS_IN_PROCESS", "1")
    monkeypatch.setattr(dim_labels, "drawn_readers", _engines)
    words = b"0 g BT /F1 12 Tf 40 30 Td (VENDOR ELEVATION) Tj ET\n"
    (uploads / "shop.pdf").write_bytes(_pdf([words + _pen_page(), words]))
    folder = _pack(tmp_path, uploads, _listing(uploads, ("shop.pdf", SHOP_KIND)))
    page_map = _read(folder, "page_map.json")
    assert _roles(page_map) == [("shop.pdf", 1, ["vendor"]), ("shop.pdf", 2, [])]
    assert page_map["pages"][0]["halves"][0]["from_upload"] is True
    assert page_map["upload_roles"] == {"shop.pdf": "vendor"}
    assert not any("candidate" in entry for entry in page_map["pages"])
    labels = _read(folder, "dims.json")["pages"][0]["labels"]
    assert [(lab["text"], lab["route"], lab["side"]) for lab in labels] == [
        ('24"', "drawn", "vendor"),
        ('30"', "drawn", "vendor"),
        ('18"', "drawn", "vendor"),
    ]


def test_a_candidate_page_with_too_few_labels_stays_general(
    tmp_path: Path, uploads: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GV_DIM_LABELS_IN_PROCESS", "1")
    monkeypatch.setattr(dim_labels, "drawn_readers", _engines)
    one = _dimension(100, 220, 300) + stroke_ops('24"', 152, 302, height=9)
    (uploads / "shop.pdf").write_bytes(_pdf([one]))
    folder = _pack(tmp_path, uploads, _listing(uploads, ("shop.pdf", SHOP_KIND)))
    page_map = _read(folder, "page_map.json")
    assert _roles(page_map) == [("shop.pdf", 1, [])]
    assert page_map["upload_roles"] == {}
    # Its label read while it was a candidate keeps no drawing's side.
    labels = _read(folder, "dims.json")["pages"][0]["labels"]
    assert {lab["side"] for lab in labels} <= {"unknown"}


def _strokes(x: float, y: float, width: float = 22.0, height: float = 9.0) -> bytes:
    """Letter-like strokes filling a box from `(x, y)`: what an SHX label draws."""
    out = b"0 0 0 RG 0.6 w "
    for n in range(4):
        left = x + n * width / 4
        out += (
            f"{left} {y} m {left + width / 8} {y + height} l {left + width / 5} {y} l S ".encode()
        )
    return out


def _shx_page(path: Path, values: Sequence[tuple[float, float, str]]) -> None:
    """A CAD page whose dimensions are SHX strokes with AutoCAD's `AutoCAD SHX Text` comments."""
    pdf = pikepdf.new()
    content = b"".join(_dimension(x - 20, x + 42, y - 2) + _strokes(x, y) for x, y, _ in values)
    page = pikepdf.Dictionary(
        Type=pikepdf.Name.Page,
        MediaBox=[0, 0, 600, 400],
        Resources=pikepdf.Dictionary(),
        Contents=pdf.make_stream(content),
    )
    pdf.pages.append(pikepdf.Page(page))
    pdf.pages[0].obj["/Annots"] = pikepdf.Array(
        [
            pdf.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name.Annot,
                    Subtype=pikepdf.Name.Text,
                    Rect=[x, y, x + 22, y + 9],
                    T=pikepdf.String("AutoCAD SHX Text"),
                    Contents=pikepdf.String(text),
                    F=2,
                )
            )
            for x, y, text in values
        ]
    )
    pdf.save(path)


def test_an_shx_comment_page_is_a_drawing_and_keeps_its_text(tmp_path: Path, uploads: Path) -> None:
    _shx_page(uploads / "shop.pdf", [(100, 300, '24"'), (300, 200, '30"'), (150, 100, '18"')])
    folder = _pack(tmp_path, uploads, _listing(uploads, ("shop.pdf", SHOP_KIND)))
    page_map = _read(folder, "page_map.json")
    assert _roles(page_map) == [("shop.pdf", 1, ["vendor"])]
    # The drawings-only copy keeps the SHX text, as `verify_value` reads it.
    copy = folder / "shop_drawings_only.pdf"
    text = set_pack._page_texts(copy)[0]
    assert all(value in text for value in ('24"', '30"', '18"'))
    labels = _read(folder, "dims.json")["pages"][0]["labels"]
    assert sorted(lab["text"] for lab in labels) == ['18"', '24"', '30"']
    assert {lab["side"] for lab in labels} == {"vendor"}


def _candidate_map(halves: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "upload_roles": {},
        "pages": [
            {
                "file": "shop.pdf",
                "page": 1,
                "role": "general",
                "halves": halves,
                "rounds": [],
                "candidate": {
                    "box": [0.0, 0.0, 600.0, 400.0],
                    "role": "vendor",
                    "heading": None,
                    "round": 2,
                    "from_upload": True,
                    "upload": "vendor",
                },
            }
        ],
    }


def _value(x: float, **more: Any) -> dict[str, Any]:
    return {"box": [x, 100.0, x + 20, 110.0], **more}


def test_settling_counts_every_route_but_never_a_derived_part_or_a_pasted_drawing() -> None:
    stamp = {"box": [0.0, 0.0, 200.0, 400.0], "role": "general", "heading": None, "round": None}
    page_map = _candidate_map([stamp])
    dims = {
        "pages": [
            {
                "file": "shop.pdf",
                "page": 1,
                "labels": [
                    _value(50),  # inside the pasted stamp: not the page's own
                    _value(300, route="text"),
                    _value(350, route="drawn"),
                    _value(400, derived=True),
                ],
                "metric": [_value(450, mm="794")],  # a millimetre-only value counts
            }
        ]
    }
    decided, promoted = set_pack.settle_candidates(page_map, dims)
    assert decided == promoted == [("shop.pdf", 1)]
    entry = page_map["pages"][0]
    assert entry["role"] == "vendor" and entry["rounds"] == [2]
    assert entry["halves"] == [
        {
            "box": [0.0, 0.0, 600.0, 400.0],
            "role": "vendor",
            "heading": None,
            "round": 2,
            "from_upload": True,
        }
    ]
    assert page_map["upload_roles"] == {"shop.pdf": "vendor"}
    # Two values of its own: it stays as it was.
    page_map = _candidate_map([stamp])
    dims["pages"][0]["metric"] = []
    assert set_pack.settle_candidates(page_map, dims) == ([("shop.pdf", 1)], [])
    assert page_map["pages"][0]["halves"] == [stamp] and "candidate" not in page_map["pages"][0]
