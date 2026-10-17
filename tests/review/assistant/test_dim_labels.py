"""Numbered dimension labels (#1558): code lists every printed dimension; the agent picks labels.

Every PDF here is a small synthetic one made in the test: dimensions printed as text over drawn
dimension lines with extension lines, a chain sharing one line, millimetres over inches, a label
turned to read up the page, a bare number with and without a dimension line, a note and a scale
note that are not dimensions, and (for the drawn route) character-sized ink read by scripted OCR
engines. Nothing here calls a model or needs an OCR engine installed.
"""

from __future__ import annotations

import io
import json
import zlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from app.review.assistant import chat_drawing
from app.review.assistant import dim_labels as dl
from app.review.assistant.chat_findings import FindingBook
from app.review.assistant.chat_tools import ChatTools, new_step
from app.review.assistant.chat_trust import apply_checks
from app.review.assistant.dim_findings import label_checks
from app.review.assistant.dim_index import DimLabels, checklist_label_gaps, labels_for, parse_ref
from app.review.assistant.full_check_brief import helper_brief, with_label_gaps
from app.review.assistant.sandbox import ChatWorkspace, SandboxRefused
from app.review.assistant.witness import trust_level, witnesses
from tests.review.assistant.chat_fakes import FakeSandbox

PAGE_W, PAGE_H = 600, 400
HALVES = [
    {"box": [0, 0, 250, 400], "role": "architect"},
    {"box": [250, 0, 600, 400], "role": "vendor"},
]


# ---- a synthetic PDF -----------------------------------------------------------------------------


def _stream(body: bytes) -> bytes:
    data = zlib.compress(body)
    return (
        b"<< /Filter /FlateDecode /Length "
        + str(len(data)).encode()
        + b" >>\nstream\n"
        + data
        + (b"\nendstream")
    )


def _pdf(pages: Sequence[bytes]) -> bytes:
    """Pages of 600 x 400 points, each with its own content (font /F1 Helvetica)."""
    count = len(pages)
    kids = " ".join(f"{3 + 2 * n} 0 R" for n in range(count)).encode()
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(count).encode() + b" >>",
    ]
    font = 3 + 2 * count
    for n, content in enumerate(pages):
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 400] /Resources << /Font << /F1 "
            + str(font).encode()
            + b" 0 R >> >> /Contents "
            + str(4 + 2 * n).encode()
            + b" 0 R >>"
        )
        objects.append(_stream(content))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
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


def _text(x: float, y: float, text: str, size: float = 8, up: bool = False) -> bytes:
    matrix = f"0 1 -1 0 {x} {y}" if up else f"1 0 0 1 {x} {y}"
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return f"BT /F1 {size} Tf {matrix} Tm ({escaped}) Tj ET ".encode("latin-1")


def _line(x0: float, y0: float, x1: float, y1: float) -> bytes:
    return f"0 0 0 RG 0.5 w {x0} {y0} m {x1} {y1} l S ".encode()


def _dimension(x0: float, x1: float, y: float, reach: float = 50) -> bytes:
    """A horizontal dimension line at `y` (PDF space) with extension lines down to `y - reach`."""
    return (
        _line(x0 - 1.5, y, x1 + 1.5, y)
        + _line(x0, y - reach, x0, y + 3)
        + (_line(x1, y - reach, x1, y + 3))
    )


def _page_one() -> bytes:
    body = b""
    # Architect half: 36" over its dimension line, and 30" reading up a vertical one.
    body += _dimension(100, 220, 300) + _text(150, 303, '36"')
    body += _line(62, 100, 62, 170) + _line(62, 100, 110, 100) + _line(62, 170, 110, 170)
    body += _text(60, 125, '30"', up=True)
    # Vendor half: a chain of two on one line (12" and 15"), and 457 over [18].
    body += _line(298.5, 300, 421.5, 300)
    for x in (300, 360, 420):
        body += _line(x, 250, x, 303)
    body += _text(322, 303, '12"') + _text(382, 303, '15"')
    body += _dimension(330, 410, 197) + _text(360, 200, "457") + _text(360, 189, "[18]")
    # Not dimensions: a note with a count, a scale note.
    body += _text(450, 100, "2 DRAWERS") + _text(450, 80, 'SCALE 1/2" = 1\'-0"')
    return body


def _page_two() -> bytes:
    # A bare 24 over a dimension line (a label), a bare 5 with no line (not one).
    return _dimension(300, 360, 300) + _text(325, 303, "24") + _text(450, 200, "5")


@pytest.fixture
def pdf(tmp_path: Path) -> Path:
    path = tmp_path / "set.pdf"
    path.write_bytes(_pdf([_page_one(), _page_two()]))
    return path


def _by_text(labels: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {str(label["text"]): label for label in labels}


# ---- extraction ----------------------------------------------------------------------------------


def test_every_printed_dimension_is_listed_with_its_value_box_side_and_lines(pdf: Path) -> None:
    labels = dl.page_labels(pdf, 1, HALVES).labels
    found = _by_text(labels)

    assert set(found) == {'36"', '30"', '12"', '15"', "457 [18]"}
    assert {k: found[k]["inches"] for k in found} == {
        '36"': "36",
        '30"': "30",
        '12"': "12",
        '15"': "15",
        "457 [18]": "18",
    }
    assert found['36"']["side"] == "architect" and found['12"']["side"] == "vendor"
    assert found['36"']["orientation"] == "h" and found['30"']["orientation"] == "v"
    assert found["457 [18]"]["mm"] == "457" and found["457 [18]"]["units"] == "agrees"
    # The box is the printed text, in shown points (origin top-left).
    x0, y0, x1, y1 = found['36"']["box"]
    assert 149 <= x0 < 152 and 160 < x1 < 166 and 88 < y0 < y1 < 100
    # Its dimension line runs between its two extension lines, whose far ends are below it.
    assert found['36"']["dim_line"] == [[100.0, 100.0], [220.0, 100.0]]
    assert found['36"']["extension"] == [
        [[100.0, 150.0], [100.0, 100.0]],
        [[220.0, 150.0], [220.0, 100.0]],
    ]
    # A chain on one line: each segment between its own extension lines.
    assert found['12"']["dim_line"] == [[300.0, 100.0], [360.0, 100.0]]
    assert found['15"']["dim_line"] == [[360.0, 100.0], [420.0, 100.0]]
    # The vertical one: its line and its two extension lines across.
    assert found['30"']["dim_line"] == [[62.0, 230.0], [62.0, 300.0]]


def test_a_bare_number_needs_a_dimension_line(pdf: Path) -> None:
    labels = dl.page_labels(pdf, 2).labels
    assert [(label["text"], label["inches"]) for label in labels] == [("24", "24")]
    assert labels[0]["side"] == "unknown"


def test_ids_are_stable_and_in_reading_order(pdf: Path) -> None:
    first = dl.page_labels(pdf, 1, HALVES).labels
    again = dl.page_labels(pdf, 1, HALVES).labels
    assert [(label["id"], label["text"]) for label in first] == [
        (label["id"], label["text"]) for label in again
    ]
    assert [label["id"] for label in first] == [f"D{n}" for n in range(1, len(first) + 1)]
    # Read top to bottom: no label starts a band (4 points) above the one before it.
    bands = [round(label["box"][1] / 4) for label in first]
    assert bands == sorted(bands)


class _Scripted:
    """An OCR engine that reads the same words in every picture it is shown."""

    def __init__(self, name: str, lines: list[str]) -> None:
        self.name = name
        self.lines = lines

    def __call__(self, picture: np.ndarray, *, single_line: bool) -> list[str]:
        return list(self.lines)


def _drawn_page() -> bytes:
    """`_page_one`, plus a number drawn as lines (no text) over a dimension line of its own."""
    glyphs = b"".join(f"0 0 0 rg {x} 52 2.4 6 re f ".encode() for x in (300.0, 303.5, 307.0))
    return _page_one() + _dimension(280, 340, 50) + glyphs


def test_the_drawn_route_adds_labels_after_the_text_ones(tmp_path: Path) -> None:
    path = tmp_path / "drawn.pdf"
    path.write_bytes(_pdf([_drawn_page()]))
    readers = [_Scripted("Tesseract", ['24"']), _Scripted("RapidOCR", ['24"'])]

    text_only = dl.page_labels(path, 1, HALVES).labels
    both = dl.page_labels(path, 1, HALVES, drawn_budget_s=60, readers=readers)

    assert both.drawn == "done"
    drawn = [label for label in both.labels if label["route"] == "drawn"]
    assert [(label["text"], label["side"]) for label in drawn] == [('24"', "vendor")]
    # The text labels keep their ids: drawn labels are numbered after them.
    assert [(lab["id"], lab["text"]) for lab in both.labels[: len(text_only)]] == [
        (lab["id"], lab["text"]) for lab in text_only
    ]
    assert drawn[0]["id"] == f"D{len(text_only) + 1}"


def test_the_drawn_route_needs_two_engines_that_agree(tmp_path: Path) -> None:
    path = tmp_path / "drawn.pdf"
    path.write_bytes(_pdf([_drawn_page()]))
    split = [_Scripted("Tesseract", ['24"']), _Scripted("RapidOCR", ['21"'])]
    one = [_Scripted("Tesseract", ['24"'])]

    assert not [
        label
        for label in dl.page_labels(path, 1, drawn_budget_s=60, readers=split).labels
        if label["route"] == "drawn"
    ]
    assert dl.page_labels(path, 1, drawn_budget_s=60, readers=one).drawn == "no_engine"


def test_build_dims_lists_every_page(pdf: Path) -> None:
    seen: list[int] = []
    page_map = {"pages": [{"file": "set.pdf", "page": 1, "halves": HALVES}]}
    document = dl.build_dims(
        {"set.pdf": pdf}, page_map, on_page=lambda doc: seen.append(len(doc["pages"]))
    )
    assert [(e["file"], e["page"], len(e["labels"])) for e in document["pages"]] == [
        ("set.pdf", 1, 5),
        ("set.pdf", 2, 1),
    ]
    assert seen == [1, 2] and document["version"] == dl.LABELS_VERSION


# ---- the index -----------------------------------------------------------------------------------


def test_label_ids_parse_with_or_without_a_page() -> None:
    assert parse_ref("D17") == (None, 17)
    assert parse_ref("p23.D17") == (23, 17)
    assert parse_ref("p 23 D4") == (23, 4)
    assert parse_ref("17") is None and parse_ref(None) is None


def test_labels_come_from_the_pack_or_are_read_now(pdf: Path, tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    pack.mkdir()
    made = {
        "file": "set.pdf",
        "page": 1,
        "labels": [
            {"id": "D1", "text": '9"', "inches": "9", "box": [1, 2, 3, 4], "side": "vendor"}
        ],
    }
    (pack / "dims.json").write_text(json.dumps({"version": 1, "pages": [made]}))
    labels = DimLabels(pack, pdf.parent)

    assert labels.resolve("D1", "set.pdf", 1)["text"] == '9"'
    # Page 2 is not in the pack yet: read now, by the text route.
    assert labels.resolve("D1", "set.pdf", 2)["text"] == "24"
    assert labels.resolve("p2.D1", "set.pdf", 1)["page"] == 2
    with pytest.raises(LookupError, match="has no label D9"):
        labels.resolve("D9", "set.pdf", 2)
    with pytest.raises(LookupError, match="not a label id"):
        labels.resolve("36", "set.pdf", 1)


# ---- zoom ----------------------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path, pdf: Path) -> ChatWorkspace:
    # A set pack with a page map (whose drawings give each label its side) and no dims.json yet:
    # the labels are read when first asked for.
    pack = tmp_path / "pack"
    pack.mkdir()
    page_map = {"pages": [{"file": "set.pdf", "page": 1, "halves": HALVES}]}
    (pack / "page_map.json").write_text(json.dumps(page_map))
    opened = ChatWorkspace.open(tmp_path / "chat", set_pack=pack)
    (opened.uploads / "set.pdf").write_bytes(pdf.read_bytes())
    listing = [{"name": "set.pdf", "pages": 2, "kind": "the vendor's shop drawings"}]
    (opened.root / "uploads.json").write_text(json.dumps(listing), encoding="utf-8")
    return opened


def _colour_count(png: bytes, colour: tuple[int, int, int]) -> int:
    with Image.open(io.BytesIO(png)) as picture:
        pixels = np.asarray(picture.convert("RGB")).reshape(-1, 3).astype(int)
    return int((np.abs(pixels - np.array(colour)).sum(axis=1) < 60).sum())


def test_zoom_marks_numbers_every_label_and_lists_them(workspace: ChatWorkspace) -> None:
    tools = ChatTools(workspace, FakeSandbox(workspace), uploads=[{"name": "set.pdf", "pages": 2}])
    raw = {
        "crops": [{"page": 1, "box": {"x0": 280, "y0": 60, "x1": 440, "y1": 230}}],
        "marks": True,
    }

    result = tools.zoom(raw, new_step("z", "zoom", raw), "lead")

    assert not result.is_error
    assert [block["type"] for block in result.content] == ["text", "image", "text", "image"]
    listed = result.content[2]["text"]
    assert listed.startswith("Crop 1 numbered: ")
    for text in ('12" (vendor, h', '15" (vendor, h', "457 [18] (vendor, h"):
        assert text in listed
    assert '36"' not in listed  # outside this crop
    clean = (workspace.pictures / f"{result.content[1]['source']['picture_id']}.png").read_bytes()
    marked = (workspace.pictures / f"{result.content[3]['source']['picture_id']}.png").read_bytes()
    assert _colour_count(clean, dl._MARK_COLOUR) == 0 < _colour_count(marked, dl._MARK_COLOUR)
    assert len(tools.seen) == 1  # the clean crop is what was seen


def test_zoom_guides_draw_a_dimensions_extension_lines_across_the_crop(
    workspace: ChatWorkspace,
) -> None:
    tools = ChatTools(workspace, FakeSandbox(workspace), uploads=[{"name": "set.pdf", "pages": 2}])
    labels = labels_for(workspace.set_pack, workspace.uploads).on_page("set.pdf", 1) or []
    twelve = next(label for label in labels if label["text"] == '12"')
    raw = {
        "crops": [
            {
                "page": 1,
                "box": {"x0": 280, "y0": 20, "x1": 440, "y1": 230},
                "guides": [twelve["id"], "D99"],
            }
        ]
    }

    result = tools.zoom(raw, new_step("z", "zoom", raw), "lead")

    words = result.content[2]["text"]
    assert f"Guide {twelve['id']}: drawn through its extension lines" in words
    assert "Not drawn: D99" in words
    marked = (workspace.pictures / f"{result.content[3]['source']['picture_id']}.png").read_bytes()
    with Image.open(io.BytesIO(marked)) as picture:
        rgb = np.asarray(picture.convert("RGB")).astype(int)
        scale = picture.width / 160
    magenta = np.abs(rgb - np.array(dl._GUIDE_COLOUR)).sum(axis=2) < 60
    # The two extension lines (x = 300 and 360) run the crop's full height, past the drawing.
    for x in (300, 360):
        column = magenta[:, max(0, round((x - 280) * scale) - 2) : round((x - 280) * scale) + 3]
        assert column.any(axis=1).mean() > 0.95


def test_zoom_refuses_bad_guides(workspace: ChatWorkspace) -> None:
    tools = ChatTools(workspace, FakeSandbox(workspace), uploads=[{"name": "set.pdf", "pages": 2}])
    raw = {"crops": [{"page": 1, "box": {"x0": 0, "y0": 0, "x1": 50, "y1": 50}, "guides": "D1"}]}
    result = tools.zoom(raw, new_step("z", "zoom", raw), "lead")
    assert result.is_error and "guides must be a list" in result.content[0]["text"]


# ---- findings and cites by label -----------------------------------------------------------------


def _book(workspace: ChatWorkspace) -> FindingBook:
    listing = json.loads((workspace.root / "uploads.json").read_text())
    pdfs = {"set.pdf": workspace.uploads / "set.pdf"}
    return FindingBook(
        workspace=workspace,
        listing=listing,
        page_count=2,
        checker=lambda finding: apply_checks(finding, pdfs, None),
    )


def _ids(workspace: ChatWorkspace) -> dict[str, str]:
    labels = labels_for(workspace.set_pack, workspace.uploads).on_page("set.pdf", 1) or []
    return {str(label["text"]): str(label["id"]) for label in labels}


def _finding(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "page": 1,
        "item": "Top width",
        "category": "countertop",
        "status": "different",
        "verified": True,
        "source": "architect",
    }
    base.update(changes)
    return base


def test_report_finding_by_label_fills_values_and_boxes(workspace: ChatWorkspace) -> None:
    book = _book(workspace)
    ids = _ids(workspace)

    finding, notes = book.report(
        _finding(architect_dim=ids['36"'], vendor_dim=ids["457 [18]"]), "lead"
    )

    assert finding["architect"] == '36"' and finding["vendor"] == "457 [18]"
    assert finding["vendor_box"]["page"] == 1 and finding["vendor_box"]["file"] == "set.pdf"
    found = labels_for(workspace.set_pack, workspace.uploads).on_page("set.pdf", 1) or []
    labels = {label["id"]: label for label in found}
    vendor = labels[ids["457 [18]"]]
    assert [finding["vendor_box"][k] for k in ("x0", "y0", "x1", "y1")] == vendor["box"]
    assert finding["dims"]["vendor"]["id"] == ids["457 [18]"]
    assert finding["inches"] == {"architect": "36", "vendor": "18", "difference": "-18"}
    assert notes == []


def test_a_label_built_finding_is_printed_and_confirmed(workspace: ChatWorkspace) -> None:
    book = _book(workspace)
    ids = _ids(workspace)

    finding, _ = book.report(_finding(architect_dim=ids['36"'], vendor_dim=ids['12"']), "lead")

    checks = finding["checks"]
    assert checks["printed_architect"] == checks["printed_vendor"] == "match"
    assert "printed" in finding["witnesses"] and finding["evidence_kind"] == "observed"
    assert finding["trust_level"] == "confirmed"  # code read both, and the lead checked it


def test_a_typed_value_that_is_not_the_labels_is_refused(workspace: ChatWorkspace) -> None:
    book = _book(workspace)
    ids = _ids(workspace)
    with pytest.raises(SandboxRefused, match="is not what"):
        book.report(_finding(architect='36"', vendor='13"', vendor_dim=ids['12"']), "lead")
    with pytest.raises(SandboxRefused, match="has no label D99"):
        book.report(_finding(architect='36"', vendor_dim="D99"), "lead")
    # The same value typed another way is kept as typed.
    finding, _ = book.report(
        _finding(architect="3'-0\"", architect_dim=ids['36"'], vendor_dim=ids['12"']), "lead"
    )
    assert finding["architect"] == "3'-0\""
    # Without a label the old path stands: a missing value is still refused.
    with pytest.raises(SandboxRefused, match="missing argument 'architect'"):
        book.report(_finding(vendor_dim=ids['12"']), "lead")


def test_a_label_on_the_other_drawing_is_noted(workspace: ChatWorkspace) -> None:
    book = _book(workspace)
    ids = _ids(workspace)
    _finding_made, notes = book.report(
        _finding(architect_dim=ids['36"'], vendor_dim=ids['30"']), "lead"
    )
    turned = ids['30"']
    assert notes == [
        f"{turned} is on the drawing the page map calls the architect's: check it is the vendor's"
    ]


def test_changing_a_value_without_a_label_drops_its_witness(workspace: ChatWorkspace) -> None:
    book = _book(workspace)
    ids = _ids(workspace)
    finding, _ = book.report(_finding(architect_dim=ids['36"'], vendor_dim=ids['12"']), "lead")

    changed, _ = book.update({"finding_id": finding["id"], "vendor": '13"'})

    assert "vendor" not in (changed["dims"] or {})
    assert changed["checks"]["printed_vendor"] != "match"
    again, _ = book.update({"finding_id": finding["id"], "vendor_dim": ids['15"']})
    assert again["vendor"] == '15"' and again["checks"]["printed_vendor"] == "match"


def test_cite_by_label(workspace: ChatWorkspace) -> None:
    book = _book(workspace)
    ids = _ids(workspace)

    def printed(pdf: Path, page: int, box: Mapping[str, Any], value: str) -> str:
        from app.review.assistant.finding_checks import printed_at_spot

        return printed_at_spot(pdf, page, box, value)

    cite = book.cite({"value": "3'-0\"", "dim": ids['36"'], "page": 1}, printed)
    assert cite["value"] == "3'-0\"" and cite["printed"] == "match" and cite["dim"] == ids['36"']
    assert cite["box"]["page"] == 1
    with pytest.raises(SandboxRefused, match="give the page too"):
        book.cite({"value": '36"', "dim": "D1"}, printed)
    with pytest.raises(SandboxRefused, match="is not what"):
        book.cite({"value": '35"', "dim": ids['36"'], "page": 1}, printed)


def test_a_drawn_label_is_the_drawn_ink_witness_never_more() -> None:
    box = [300.0, 342.0, 310.0, 348.0]
    finding = {
        "architect": '24"',
        "vendor": '24"',
        "arch_box": {"page": 1, "x0": 1.0, "y0": 2.0, "x1": 3.0, "y1": 4.0},
        "vendor_box": {"page": 1, "x0": 300.0, "y0": 342.0, "x1": 310.0, "y1": 348.0},
        "dims": {
            "architect": {
                "id": "D1",
                "page": 1,
                "text": '24"',
                "inches": "24",
                "route": "text",
                "box": [1.0, 2.0, 3.0, 4.0],
            },
            "vendor": {
                "id": "D7",
                "page": 1,
                "text": '24"',
                "inches": "24",
                "route": "drawn",
                "box": box,
            },
        },
    }
    checks = label_checks(
        finding,
        {"printed_architect": "not_found", "printed_vendor": "no_text", "lead_checked": True},
    )
    assert checks["verify_vendor"]["route"] == "drawn" and checks["printed_vendor"] == "no_text"
    assert witnesses(checks) == ["ocr_ink"]
    assert trust_level(checks) == "likely"  # the lead's check alone never confirms drawn ink
    assert trust_level({**checks, "second_read": "agrees"}) == "confirmed"


def test_the_cloud_goes_round_the_label(workspace: ChatWorkspace) -> None:
    book = _book(workspace)
    ids = _ids(workspace)
    finding, _ = book.report(_finding(architect_dim=ids['36"'], vendor_dim=ids['12"']), "lead")
    box = tuple(finding["vendor_box"][k] for k in ("x0", "y0", "x1", "y1"))
    mark = chat_drawing.Mark(1, finding, 1, box, "wrong", True, finding["vendor"])

    spots = chat_drawing._spots(workspace.uploads / "set.pdf", [mark])

    assert spots[(1, 1)] == (box, None)


# ---- the full check's to-do ----------------------------------------------------------------------


def _labels() -> dict[int, list[dict[str, Any]]]:
    def label(ident: str, inches: str, side: str, box: list[float]) -> dict[str, Any]:
        return {"id": ident, "text": f'{inches}"', "inches": inches, "side": side, "box": box}

    return {
        5: [
            label("D1", "36", "architect", [10, 10, 20, 15]),
            label("D2", "12", "vendor", [300, 10, 310, 15]),
            label("D3", "15", "vendor", [320, 10, 330, 15]),
            label("D4", "27", "vendor", [340, 10, 350, 15]),
            label("D5", "4", "vendor", [360, 10, 365, 15]),
            label("D6", "9", "vendor", [380, 10, 385, 15]),
        ]
    }


def test_unhandled_vendor_labels_are_checklist_gaps() -> None:
    findings = [{"page": 5, "dims": {"vendor": {"id": "D2", "page": 5}}}]
    strings = [{"page": 5, "side": "vendor", "parts": ['15"', "12"], "printed_total": '27"'}]
    report = "CHECKLIST p5: architect 1/1; notes 0/0; strings 1/1; not compared: none\nLABELS p5 not on architect: D5"

    gaps = checklist_label_gaps(report, _labels(), findings, strings)

    assert gaps == {
        5: "1 of 5 vendor dimension labels not handled (match, string, or 'not on architect'): D6"
    }
    readings = [{"page": 5, "box": {"x0": 378, "y0": 8, "x1": 390, "y1": 18}}]
    assert checklist_label_gaps(report, _labels(), findings, strings, readings) == {}


def test_label_gaps_join_the_checklists_gaps() -> None:
    merged = with_label_gaps({5: "no 'not compared' part"}, "", [5, 6], {**_labels(), 6: None}, [])
    assert merged[5].startswith("no 'not compared' part; 5 of 5 vendor dimension labels")
    assert 6 not in merged  # a page whose labels are not known is not counted


def test_the_helper_brief_lists_the_labels_and_the_rule() -> None:
    brief = helper_brief([5, 6], file_name="set.pdf", labels={5: _labels()[5], 6: None})
    assert "- p5: 6 labels (architect 1, vendor 5)" in brief
    assert "- p6: not listed yet (zoom with marks: true)" in brief
    assert "LABELS p<N> not on architect: D3, D9" in brief
    assert "CHECKLIST p<N>: architect a/b; notes a/b; strings a/b; not compared:" in brief
