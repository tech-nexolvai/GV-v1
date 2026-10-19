"""The colour rule (#1751): colour decides nothing, except on GV's combined review sheets, $0.

- An ordinary upload (an architect's or a vendor's own CAD sheet) is read in every colour: a sheet
  that prints every dimension in red, blue or green gives its labels.
- A GV combined sheet (drawings pasted on it as stamps) keeps the black-or-grey rule inside its
  snapshots, exactly as before.
- Whatever the file, an annotation is never read as the drawing's text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pikepdf
import pytest

from app.review.assistant import dim_labels
from app.review.assistant import verify_value as vv

WIDTH, HEIGHT = 600.0, 400.0


def _dimension(x0: float, x1: float, y: float, text: str, colour: str) -> bytes:
    """A horizontal dimension: its line, two extension lines and its value, all in `colour`."""
    return (
        f"{colour.upper()} {colour} 0.5 w {x0 - 1.5} {y} m {x1 + 1.5} {y} l S "
        f"{x0} {y - 40} m {x0} {y + 3} l S {x1} {y - 40} m {x1} {y + 3} l S "
        f"BT /F1 9 Tf {(x0 + x1) / 2 - 8} {y + 3} Td ({text}) Tj ET\n"
    ).encode()


def _sheet(path: Path, content: bytes, annotate: Any = None) -> Path:
    pdf = pikepdf.new()
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
        )
    )
    resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    page = pikepdf.Dictionary(
        Type=pikepdf.Name.Page,
        MediaBox=[0, 0, WIDTH, HEIGHT],
        Resources=resources,
        Contents=pdf.make_stream(content),
    )
    pdf.pages.append(pikepdf.Page(page))
    if annotate is not None:
        annotate(pdf, pdf.pages[0], resources)
    pdf.save(path)
    vv._PAGE_CACHE.clear()
    return path


def _labels(path: Path) -> list[str]:
    return sorted(str(label["text"]) for label in dim_labels.page_labels(path, 1).labels)


#: Colours as PDF fill operators (the stroke is the same, upper-cased).
RED, BLUE, GREEN = "1 0 0 rg", "0 0 1 rg", "0 0.6 0 rg"


@pytest.mark.parametrize("colour", [RED, BLUE, GREEN], ids=["red", "blue", "green"])
def test_an_ordinary_sheet_drawn_all_in_one_colour_is_read(tmp_path: Path, colour: str) -> None:
    content = _dimension(100, 220, 300, '34"', colour) + _dimension(300, 360, 200, '2"', colour)
    sheet = _sheet(tmp_path / "shop.pdf", content)
    assert _labels(sheet) == ['2"', '34"']
    box = {"x0": 140.0, "y0": HEIGHT - 315, "x1": 190.0, "y1": HEIGHT - 300}
    assert vv.verify_value(sheet, 1, box, '34"', drawn=False).result == "match"


def test_an_ordinary_sheet_reads_black_and_red_alike(tmp_path: Path) -> None:
    content = _dimension(100, 220, 300, '34"', "0 g") + _dimension(300, 360, 200, '5"', RED)
    assert _labels(_sheet(tmp_path / "shop.pdf", content)) == ['34"', '5"']


def _pasted(pdf: pikepdf.Pdf, page: pikepdf.Page, resources: Any) -> None:
    """A drawing pasted as a stamp (GV's combined sheet): a black 30" and a red 12" inside it."""
    body = _dimension(40, 160, 80, '30"', "0 g") + _dimension(200, 260, 40, '12"', RED)
    appearance = pdf.make_stream(body)
    appearance["/Type"] = pikepdf.Name.XObject
    appearance["/Subtype"] = pikepdf.Name.Form
    appearance["/BBox"] = [0, 0, 300, 120]
    appearance["/Resources"] = resources
    page.obj["/Annots"] = pikepdf.Array(
        [
            pdf.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name.Annot,
                    Subtype=pikepdf.Name.Stamp,
                    Rect=[250, 250, 550, 370],
                    AP=pikepdf.Dictionary(N=appearance),
                )
            )
        ]
    )


def test_a_gv_combined_sheet_keeps_the_black_or_grey_rule(tmp_path: Path) -> None:
    combined = _sheet(tmp_path / "combined.pdf", b"", _pasted)
    assert vv.combined_sheet(combined.read_bytes()) is True
    assert _labels(combined) == ['30"']


def test_an_ordinary_sheet_is_not_a_combined_sheet(tmp_path: Path) -> None:
    sheet = _sheet(tmp_path / "shop.pdf", _dimension(100, 220, 300, '34"', RED))
    assert vv.combined_sheet(sheet.read_bytes()) is False


def test_an_annotation_in_any_colour_is_never_read(tmp_path: Path) -> None:
    def note(pdf: pikepdf.Pdf, page: pikepdf.Page, resources: Any) -> None:
        appearance = pdf.make_stream(b'1 0 0 rg BT /F1 9 Tf 2 4 Td (99") Tj ET')
        appearance["/Type"] = pikepdf.Name.XObject
        appearance["/Subtype"] = pikepdf.Name.Form
        appearance["/BBox"] = [0, 0, 40, 14]
        appearance["/Resources"] = resources
        page.obj["/Annots"] = pikepdf.Array(
            [
                pdf.make_indirect(
                    pikepdf.Dictionary(
                        Type=pikepdf.Name.Annot,
                        Subtype=pikepdf.Name.FreeText,
                        Rect=[140, 303, 180, 317],
                        Contents=pikepdf.String('99"'),
                        AP=pikepdf.Dictionary(N=appearance),
                    )
                )
            ]
        )

    sheet = _sheet(tmp_path / "shop.pdf", _dimension(100, 220, 300, '34"', RED), note)
    assert _labels(sheet) == ['34"']
