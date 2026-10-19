"""Only what the page shows is read; AutoCAD's SHX text is (#1745), on synthetic PDFs, $0.

Each sheet prints one visible dimension (`36"`, always read) beside one case:
- a value on a layer switched off, set invisible (render mode 3), or under a white-out box: never
  read, by the labels or by `verify_value`;
- AutoCAD SHX text, stored as invisible text over its drawn strokes or as a comment authored
  `AutoCAD SHX Text` over them: read, at the strokes;
- a stale SHX comment with nothing drawn under it: not read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pikepdf
import pytest

from app.review.assistant import dim_labels
from app.review.assistant import verify_value as vv

WIDTH, HEIGHT = 600.0, 400.0


def _strokes(x: float, y: float, width: float = 22.0, height: float = 9.0) -> bytes:
    """Letter-like strokes filling a box from `(x, y)`: what an SHX label draws."""
    out = b"0 0 0 RG 0.6 w "
    for n in range(4):
        left = x + n * width / 4
        out += (
            f"{left} {y} m {left + width / 8} {y + height} l {left + width / 5} {y} l S ".encode()
        )
    return out


def _sheet(path: Path, extra: bytes, annotate: Any = None, layer_off: bool = False) -> Path:
    pdf = pikepdf.new()
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
        )
    )
    content = b'0 g BT /F1 10 Tf 100 300 Td (36") Tj ET\n' + extra
    resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    if layer_off:
        layer = pdf.make_indirect(
            pikepdf.Dictionary(Type=pikepdf.Name.OCG, Name=pikepdf.String("A-ANNO-OLD"))
        )
        pdf.Root.OCProperties = pikepdf.Dictionary(
            OCGs=[layer], D=pikepdf.Dictionary(OFF=[layer], Order=[layer])
        )
        resources["/Properties"] = pikepdf.Dictionary(L1=layer)
    page = pikepdf.Dictionary(
        Type=pikepdf.Name.Page,
        MediaBox=[0, 0, WIDTH, HEIGHT],
        Resources=resources,
        Contents=pdf.make_stream(content),
    )
    pdf.pages.append(pikepdf.Page(page))
    if annotate is not None:
        annotate(pdf, pdf.pages[0])
    pdf.save(path)
    vv._PAGE_CACHE.clear()
    return path


def _labels(path: Path) -> list[str]:
    return sorted(str(label["text"]) for label in dim_labels.page_labels(path, 1).labels)


def _box_at(x: float, y: float) -> dict[str, float]:
    """A box round a value printed at PDF `(x, y)` (shown points, origin top-left)."""
    return {"x0": x - 3, "y0": HEIGHT - y - 12, "x1": x + 30, "y1": HEIGHT - y + 4}


@pytest.mark.parametrize(
    "hidden",
    [
        b'/OC /L1 BDC 0 g BT /F1 10 Tf 300 200 Td (99") Tj ET EMC\n',  # a layer switched off
        b'0 g BT 3 Tr /F1 10 Tf 300 200 Td (99") Tj ET\n',  # invisible text
        b'0 g BT /F1 10 Tf 300 200 Td (99") Tj ET 1 g 290 190 60 30 re f\n',  # under a white-out
    ],
    ids=["layer-off", "render-mode-3", "white-out"],
)
def test_text_the_page_does_not_show_is_never_read(tmp_path: Path, hidden: bytes) -> None:
    sheet = _sheet(tmp_path / "sheet.pdf", hidden, layer_off=b"/OC" in hidden)
    assert _labels(sheet) == ['36"']
    assert vv.verify_value(sheet, 1, _box_at(300, 200), '99"', drawn=False).result != "match"
    assert vv.verify_value(sheet, 1, _box_at(100, 300), '36"', drawn=False).result == "match"


def test_the_same_text_shown_is_read(tmp_path: Path) -> None:
    """The control: the value printed plainly is a label."""
    sheet = _sheet(tmp_path / "sheet.pdf", b'0 g BT /F1 10 Tf 300 200 Td (99") Tj ET\n')
    assert _labels(sheet) == ['36"', '99"']


def test_autocad_hidden_shx_text_over_its_strokes_is_read(tmp_path: Path) -> None:
    extra = _strokes(300, 200) + b'0 g BT 3 Tr /F1 10 Tf 300 200 Td (24") Tj ET\n'
    sheet = _sheet(tmp_path / "sheet.pdf", extra)
    assert _labels(sheet) == ['24"', '36"']
    assert vv.verify_value(sheet, 1, _box_at(300, 200), '24"', drawn=False).result == "match"


def _shx_comment(rect: list[float], text: str) -> Any:
    def annotate(pdf: pikepdf.Pdf, page: pikepdf.Page) -> None:
        page.obj["/Annots"] = pikepdf.Array(
            [
                pdf.make_indirect(
                    pikepdf.Dictionary(
                        Type=pikepdf.Name.Annot,
                        Subtype=pikepdf.Name.Text,
                        Rect=rect,
                        T=pikepdf.String("AutoCAD SHX Text"),
                        Contents=pikepdf.String(text),
                        F=2,  # hidden: AutoCAD writes them so
                    )
                )
            ]
        )

    return annotate


def test_an_autocad_shx_comment_over_its_strokes_is_read(tmp_path: Path) -> None:
    sheet = _sheet(
        tmp_path / "sheet.pdf", _strokes(300, 200), _shx_comment([300, 200, 322, 209], '18"')
    )
    assert _labels(sheet) == ['18"', '36"']
    label = next(lab for lab in dim_labels.page_labels(sheet, 1).labels if lab["text"] == '18"')
    x0, y0, x1, y1 = label["box"]
    # Its box is the comment's, over the strokes (shown points).
    assert x0 == pytest.approx(300, abs=1) and x1 == pytest.approx(322, abs=1.5)
    assert HEIGHT - 209 - 1 <= y0 < y1 <= HEIGHT - 200 + 2


def test_a_stale_shx_comment_with_nothing_drawn_under_it_is_not_read(tmp_path: Path) -> None:
    sheet = _sheet(tmp_path / "sheet.pdf", b"", _shx_comment([300, 200, 322, 209], '42"'))
    assert _labels(sheet) == ['36"']


def test_any_other_comment_is_never_read(tmp_path: Path) -> None:
    def annotate(pdf: pikepdf.Pdf, page: pikepdf.Page) -> None:
        _shx_comment([300, 200, 322, 209], '18"')(pdf, page)
        page.obj["/Annots"][0]["/T"] = pikepdf.String("Reviewer")

    sheet = _sheet(tmp_path / "sheet.pdf", _strokes(300, 200), annotate)
    assert _labels(sheet) == ['36"']
