"""The cloud drawing looks like a reviewer's markup (#1537), on synthetic PDFs, $0.

- tight clouds: snapped to the value's printed text, else cut down to 2.5 in;
- notes in clear space: never overlapping, never off the page, wrapped to 3 lines;
- only Wrong items clouded by default ("Your decision": a numbered marker only);
- a clean copy without earlier review markup, keeping the pasted drawings (`/Stamp`);
- notes pages grouped by page, with counts, a legend and small pictures; today's words.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import numpy as np
import pikepdf
import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pytest
from pypdf import PdfReader
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from app.review.assistant import chat_drawing
from app.review.assistant.chat_drawing import (
    CLEAN_WORDS,
    LEGEND,
    Mark,
    clone_pdf,
    markup_pdf,
    note_text,
)
from app.review.assistant.cloud_layout import (
    EDGE_PT,
    MAX_CLOUD_PT,
    NOTE_LINES,
    InkMap,
    capped,
    gap,
    overlaps,
    place_notes,
    wrap,
)
from app.review.assistant.proof_drawing import proof_markup_pdf
from app.review.assistant.verify_value import printed_spots

WIDTH, HEIGHT = 1000.0, 700.0
#: Where the sheet prints its values (shown points, origin top-left: x, baseline y).
VALUE_46 = (600.0, 520.0)
VALUE_12 = (200.0, 200.0)
#: A big AI box round most of the sheet's lower half, with the 46" near its right end.
BIG_BOX = (120.0, 330.0, 680.0, 560.0)


def _sheet(path: Path, *, rotate: int = 0, ink: bool = True) -> Path:
    """A one-page sheet: two printed values and a block of drawing lines on the left half."""
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(WIDTH, HEIGHT), invariant=1)
    c.setFont("Helvetica", 10)
    c.drawString(VALUE_46[0], HEIGHT - VALUE_46[1], '46"')
    c.drawString(VALUE_12[0], HEIGHT - VALUE_12[1], '12"')
    if ink:
        c.setLineWidth(1)
        for n in range(40):
            y = 120 + n * 12
            c.line(60, y, 480, y)
    c.showPage()
    c.save()
    with pikepdf.open(io.BytesIO(out.getvalue())) as pdf:
        if rotate:
            pdf.pages[0].obj["/Rotate"] = rotate
        pdf.save(path)
    return path


def _annotation(pdf: pikepdf.Pdf, subtype: str, rect: tuple[float, ...], stream: bytes) -> Any:
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
        )
    )
    appearance = pdf.make_stream(stream)
    appearance["/Type"] = pikepdf.Name.XObject
    appearance["/Subtype"] = pikepdf.Name.Form
    appearance["/BBox"] = pikepdf.Array([0, 0, rect[2] - rect[0], rect[3] - rect[1]])
    appearance["/Resources"] = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    return pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Annot,
            Subtype=pikepdf.Name(subtype),
            Rect=pikepdf.Array(list(rect)),
            AP=pikepdf.Dictionary(N=appearance),
            Contents=pikepdf.String("GV note"),
        )
    )


def _marked_up_sheet(path: Path) -> Path:
    """The sheet with a pasted drawing (`/Stamp`) and three of a reviewer's marks on it."""
    _sheet(path)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        stamp = _annotation(
            pdf,
            "/Stamp",
            (700, 100, 900, 200),
            b"0 0 0 RG 10 10 180 80 re S BT /F1 9 Tf 20 40 Td (DRAWING) Tj ET",
        )
        note = _annotation(
            pdf, "/FreeText", (700, 300, 900, 330), b"1 0 0 rg BT /F1 9 Tf 2 10 Td (Fix this) Tj ET"
        )
        box = _annotation(pdf, "/Square", (700, 400, 800, 450), b"1 1 0 rg 0 0 100 50 re f")
        line = _annotation(pdf, "/Line", (700, 500, 800, 510), b"1 0 0 RG 0 5 m 100 5 l S")
        pdf.pages[0].obj["/Annots"] = pikepdf.Array([stamp, note, box, line])
        pdf.save(path)
    return path


def _box(x0: float, y0: float, x1: float, y1: float, page: int = 1) -> dict[str, Any]:
    return {"page": page, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "file": "sheet.pdf"}


def _finding(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "page": 1,
        "item": "Outlet height",
        "architect": "3'-1\"",
        "vendor": '46"',
        "status": "different",
        "trust_level": "confirmed",
        "why": "Outlet 9 in higher than the design",
        "inches": {"architect": "37", "vendor": "46", "difference": "9"},
        "vendor_box": _box(*BIG_BOX),
    }
    base.update(changes)
    return base


def _placed(findings: list[dict[str, Any]]) -> list[tuple[int, Any, int | None, Any, str]]:
    out: list[tuple[int, Any, int | None, Any, str]] = []
    for number, finding in enumerate(findings, start=1):
        box = finding.get("vendor_box")
        if box is None:
            out.append((number, finding, None, None, "Not marked on the drawing: no box."))
        else:
            coords = tuple(float(box[k]) for k in ("x0", "y0", "x1", "y1"))
            out.append(
                (number, finding, int(box["page"]), coords, f"Marked on page {box['page']}.")
            )
    return out


def _render(pdf: Path, page: int = 1) -> np.ndarray:
    document = pdfium.PdfDocument(str(pdf))
    try:
        return np.asarray(document[page - 1].render(scale=1).to_pil().convert("RGB")).astype(int)
    finally:
        document.close()


def _red(picture: np.ndarray, box: tuple[float, float, float, float]) -> int:
    x0, y0, x1, y1 = (int(v) for v in box)
    area = picture[max(0, y0) : y1, max(0, x0) : x1]
    r, g, b = area[..., 0], area[..., 1], area[..., 2]
    return int(((r > 170) & (g < 70) & (b < 70)).sum())


def _amber(picture: np.ndarray, box: tuple[float, float, float, float]) -> int:
    x0, y0, x1, y1 = (int(v) for v in box)
    area = picture[max(0, y0) : y1, max(0, x0) : x1]
    r, g, b = area[..., 0], area[..., 1], area[..., 2]
    return int(((r > 160) & (g > 80) & (g < 160) & (b < 70)).sum())


# ---- tight clouds --------------------------------------------------------------------------------


def test_the_value_is_found_by_its_printed_text(tmp_path: Path) -> None:
    sheet = _sheet(tmp_path / "sheet.pdf")
    spots = printed_spots(sheet, 1, _box(*BIG_BOX), '46"')
    assert len(spots) == 1
    x0, y0, x1, y1 = spots[0]
    assert VALUE_46[0] - 2 <= x0 < VALUE_46[0] + 4 and x1 < VALUE_46[0] + 25
    assert VALUE_46[1] - 12 < y0 < y1 <= VALUE_46[1] + 4
    # The other value is not it, and a value outside the box (and its margin) is not found.
    assert printed_spots(sheet, 1, _box(*BIG_BOX), '13"') == []
    assert printed_spots(sheet, 1, _box(700, 50, 900, 120), '46"') == []
    # A sentence: its first dimension is the value looked for.
    assert printed_spots(sheet, 1, _box(*BIG_BOX), '46" to the outlet centre') == spots


def test_a_big_box_gets_a_tight_cloud_at_the_value(tmp_path: Path) -> None:
    sheet = _sheet(tmp_path / "sheet.pdf")
    out = tmp_path / "out.pdf"
    markup_pdf(sheet, _placed([_finding()]), out)
    picture = _render(out)
    x, y = VALUE_46
    assert _red(picture, (x - 20, y - 25, x + 40, y + 15)) > 20
    # Nothing round the AI's big box: its far left end, under the lines, has no red.
    assert _red(picture, (BIG_BOX[0] - 12, BIG_BOX[1] + 40, BIG_BOX[0] + 12, BIG_BOX[3] - 40)) == 0


def test_a_big_box_without_its_value_is_cut_down_to_two_and_a_half_inches(tmp_path: Path) -> None:
    box = (100.0, 100.0, 700.0, 400.0)
    cut, was_cut = capped(box)
    assert was_cut and cut == (310.0, 160.0, 490.0, 340.0)
    assert max(cut[2] - cut[0], cut[3] - cut[1]) <= MAX_CLOUD_PT
    assert capped((10.0, 10.0, 60.0, 30.0)) == ((10.0, 10.0, 60.0, 30.0), False)
    # Round a given spot, but never outside the box.
    assert capped(box, at=(110.0, 390.0))[0] == (100.0, 220.0, 280.0, 400.0)

    sheet = _sheet(tmp_path / "sheet.pdf", ink=False)
    mark = Mark(1, _finding(vendor='99"'), 1, (100.0, 100.0, 700.0, 300.0), "wrong", True, '99"')
    spot, area = chat_drawing._spots(sheet, [mark])[(1, 1)]
    assert spot == (310.0, 110.0, 490.0, 290.0) and area == (100.0, 100.0, 700.0, 300.0)
    out = tmp_path / "out.pdf"
    markup_pdf(sheet, _placed([_finding(vendor='99"', vendor_box=_box(100, 100, 700, 300))]), out)
    picture = _render(out)
    assert _red(picture, (300, 95, 500, 125)) > 20
    # The AI's whole area is too big to outline (more than twice the cloud): nothing at its ends.
    assert _red(picture, (95, 150, 140, 250)) == 0
    assert not chat_drawing._outlined((100.0, 100.0, 700.0, 300.0))
    assert chat_drawing._outlined((100.0, 100.0, 400.0, 300.0))


def test_a_rotated_page_gets_its_cloud_where_the_page_shows_the_box(tmp_path: Path) -> None:
    sheet = _sheet(tmp_path / "sheet.pdf", rotate=90, ink=False)
    shown = pdfium.PdfDocument(str(sheet))
    width, height = shown[0].get_size()
    shown.close()
    assert (width, height) == (HEIGHT, WIDTH)
    box = (300.0, 400.0, 340.0, 420.0)
    out = tmp_path / "out.pdf"
    markup_pdf(sheet, _placed([_finding(vendor='99"', vendor_box=_box(*box))]), out)
    picture = _render(out)
    assert picture.shape[1] == int(HEIGHT) and picture.shape[0] == int(WIDTH)
    assert _red(picture, (box[0] - 14, box[1] - 4, box[0] - 2, box[3] + 4)) > 5


# ---- notes in clear space ------------------------------------------------------------------------


def test_notes_never_overlap_never_leave_the_page_and_avoid_the_drawing() -> None:
    size = (600.0, 400.0)
    grey = np.full((200, 300), 255, dtype=np.uint8)
    grey[:, :150] = 0  # the left half is all drawing
    ink = InkMap(grey, 0.5)
    cloud = (280.0, 180.0, 320.0, 200.0)
    notes = [(cloud, 150.0, 34.0)] * 12
    placed = place_notes(size, notes, ink, blocked=[cloud])
    boxes = [box for box in placed if box is not None]
    assert len(boxes) == 12
    for n, box in enumerate(boxes):
        assert EDGE_PT <= box[0] and box[2] <= size[0] - EDGE_PT
        assert EDGE_PT <= box[1] and box[3] <= size[1] - EDGE_PT
        assert not overlaps(box, cloud)
        for other in boxes[n + 1 :]:
            assert not overlaps(box, other)
    # The first note is in clear space (the right half), right beside its cloud; the next ones too
    # while the clear half has room.
    assert ink.density(boxes[0]) == 0.0 and gap(boxes[0], cloud) < 20
    assert all(ink.density(box) == 0.0 for box in boxes[:6])
    # A full page: the notes that do not fit are left off the sheet, never put off the page.
    many = place_notes(size, [(cloud, 150.0, 34.0)] * 60, ink, blocked=[cloud])
    assert None in many
    kept = [box for box in many if box is not None]
    assert all(box[2] <= size[0] - EDGE_PT and box[3] <= size[1] - EDGE_PT for box in kept)


def test_the_page_layout_keeps_notes_clear_of_clouds_markers_and_each_other(tmp_path: Path) -> None:
    findings = [
        _finding(item=f"Item {n}", vendor_box=_box(100 + 80 * n, 300, 140 + 80 * n, 316))
        for n in range(8)
    ]
    marks = [
        Mark(n, f, 1, (100 + 80 * (n - 1), 300, 140 + 80 * (n - 1), 316), "wrong", True, '46"')
        for n, f in enumerate(findings, start=1)
    ]
    placed = [chat_drawing._Placed(m, m.box, None) for m in marks]
    chat_drawing._layout((WIDTH, HEIGHT), placed, None, first_page=True)
    notes = [item.note for item in placed]
    assert all(note is not None for note in notes)
    clouds = [item.spot for item in placed]
    markers = [item.marker for item in placed]
    for n, note in enumerate(notes):
        assert note is not None
        assert 0 <= note[0] and note[2] <= WIDTH and 0 <= note[1] and note[3] <= HEIGHT
        assert not any(overlaps(note, cloud, 4) for cloud in clouds)
        assert not any(overlaps(note, (x - 7, y - 7, x + 7, y + 7)) for x, y in markers)
        assert not overlaps(note, (20, 14, 464, 38))  # the page-1 stamp
        for other in notes[n + 1 :]:
            assert other is not None and not overlaps(note, other)
        assert len(placed[n].lines) <= NOTE_LINES


def test_long_notes_wrap_to_three_lines_and_end_with_an_ellipsis() -> None:
    def measure(text: str) -> float:
        return 5.0 * len(text)

    text = (
        'Countertop wall to wall run: vendor 107 3/4 in; architect 1\'-0", 1\'-6", 8", 1\'-1" ' * 3
    )
    lines = wrap(text, 100.0, measure)
    assert len(lines) == 3 and all(measure(line) <= 100.0 for line in lines)
    assert lines[-1].endswith("…")
    assert wrap("Short note", 100.0, measure) == ["Short note"]
    cut = wrap("A" * 50, 100.0, measure, lines=2)
    assert cut[0] == "A" * 20 and cut[1].endswith("…") and len(cut) == 2


# ---- only Wrong items are clouded ----------------------------------------------------------------


def test_only_wrong_items_are_clouded_and_decisions_get_a_numbered_marker(tmp_path: Path) -> None:
    sheet = _sheet(tmp_path / "sheet.pdf", ink=False)
    wrong_box, check_box = (300.0, 300.0, 340.0, 316.0), (600.0, 150.0, 640.0, 166.0)
    findings = [
        _finding(vendor='99"', vendor_box=_box(*wrong_box)),
        _finding(
            item="Bay sizes",
            status="check",
            trust_level="to_check",
            vendor='98"',
            vendor_box=_box(*check_box),
        ),
    ]
    out = tmp_path / "out.pdf"
    markup_pdf(sheet, _placed(findings), out)
    picture = _render(out)
    assert (
        _red(picture, (wrong_box[2] + 2, wrong_box[1] - 4, wrong_box[2] + 14, wrong_box[3] + 4)) > 5
    )
    # The decision: an amber numbered marker at its top-left corner, no cloud round its right end.
    assert (
        _amber(picture, (check_box[0] - 16, check_box[1] - 16, check_box[0] + 2, check_box[1] + 2))
        > 20
    )
    assert (
        _amber(picture, (check_box[2] + 2, check_box[1] - 2, check_box[2] + 14, check_box[3] + 2))
        == 0
    )
    text = PdfReader(str(out)).pages[0].extract_text()
    assert 'Outlet height: vendor 99", make it 37"' in text and "Bay sizes" not in text

    clouded = tmp_path / "clouded.pdf"
    markup_pdf(sheet, _placed(findings), clouded, cloud_decisions=True)
    picture = _render(clouded)
    assert (
        _amber(picture, (check_box[2] + 2, check_box[1] - 2, check_box[2] + 14, check_box[3] + 2))
        > 5
    )
    assert "Not compared: Bay sizes" in PdfReader(str(clouded)).pages[0].extract_text()


def test_the_remade_drawing_clouds_agreed_items_and_leaves_ai_only_ones_grey(
    tmp_path: Path,
) -> None:
    """#1581: the reviewer's Agree is an override (clouded whatever code proved); an undecided
    Wrong item only AI readings back is not clouded, it is listed in grey."""
    sheet = _sheet(tmp_path / "sheet.pdf", ink=False)
    agreed_box, open_box = (300.0, 300.0, 340.0, 316.0), (600.0, 150.0, 640.0, 166.0)
    findings = [
        _finding(
            vendor='99"',
            trust_level="to_check",
            vendor_box=_box(*agreed_box),
            decision={"decision": "agree", "by": "Reviewer One"},
        ),
        _finding(
            item="Bay sizes", vendor='98"', trust_level="to_check", vendor_box=_box(*open_box)
        ),
    ]
    out = tmp_path / "out.pdf"
    proof_markup_pdf(
        sheet, _placed(findings), out, set_name="sheet.pdf", stamp="Checked automatically"
    )
    picture = _render(out, page=2)  # page 1 is the cover
    assert (
        _red(
            picture,
            (agreed_box[0] - 16, agreed_box[1] - 16, agreed_box[2] + 16, agreed_box[3] + 16),
        )
        > 5
    )
    assert (
        _red(picture, (open_box[0] - 16, open_box[1] - 16, open_box[2] + 16, open_box[3] + 16)) == 0
    )
    words = "".join(page.extract_text() for page in PdfReader(str(out)).pages)
    # The reviewer's decision says so, never by login (#1803, #1804).
    assert "Decided by GV's reviewer" in words and "Agreed by Reviewer One" not in words
    # #1619: what code could not prove is not on the drawing: the separate Not-compared list.
    assert "Not compared (1)" in " ".join(words.split()) and "Bay sizes" not in words
    assert "check before sending" not in words


# ---- a clean copy --------------------------------------------------------------------------------


def _subtypes(pdf: Path) -> list[str]:
    with pikepdf.open(pdf) as opened:
        return [str(a.get("/Subtype")) for a in opened.pages[0].obj.get("/Annots") or []]


def test_a_clean_copy_drops_earlier_review_markup_and_keeps_the_pasted_drawings(
    tmp_path: Path,
) -> None:
    sheet = _marked_up_sheet(tmp_path / "sheet.pdf")
    assert _subtypes(sheet) == ["/Stamp", "/FreeText", "/Square", "/Line"]
    base = clone_pdf(sheet)
    assert base.dropped == 3
    assert [str(a.get("/Subtype")) for a in base.pdf.pages[0].obj["/Annots"]] == ["/Stamp"]
    base.pdf.close()

    out = tmp_path / "out.pdf"
    markup_pdf(sheet, _placed([_finding()]), out)
    assert _subtypes(out) == ["/Stamp"]
    notes = PdfReader(str(out)).pages[-1].extract_text()
    assert f"{CLEAN_WORDS} (3 marks left out)." in notes
    # The upload itself is untouched.
    assert _subtypes(sheet) == ["/Stamp", "/FreeText", "/Square", "/Line"]

    kept = clone_pdf(sheet, clean=False)
    assert kept.dropped == 0 and len(kept.pdf.pages[0].obj["/Annots"]) == 4
    kept.pdf.close()

    plain = _sheet(tmp_path / "plain.pdf")
    out2 = tmp_path / "out2.pdf"
    markup_pdf(plain, _placed([_finding()]), out2)
    assert CLEAN_WORDS not in PdfReader(str(out2)).pages[-1].extract_text()


# ---- the notes pages and the words ---------------------------------------------------------------


def test_the_notes_pages_group_items_by_page_with_counts_legend_and_pictures(
    tmp_path: Path,
) -> None:
    sheet = tmp_path / "sheet.pdf"
    _sheet(sheet)
    with pikepdf.open(sheet, allow_overwriting_input=True) as pdf:
        pdf.pages.append(pdf.pages[0])
        pdf.save(sheet)
    findings = [
        _finding(page=2, item="Second sheet outlet", vendor_box=_box(*BIG_BOX, page=2)),
        _finding(
            item="Bay sizes",
            status="check",
            trust_level="to_check",
            vendor='12"',
            vendor_box=_box(180, 180, 240, 210),
        ),
        _finding(
            item="Sink centre", status="same", vendor='12"', vendor_box=_box(180, 180, 240, 210)
        ),
        _finding(item="Backsplash height", vendor_box=None),
    ]
    out = tmp_path / "out.pdf"
    markup_pdf(sheet, _placed(findings), out)
    reader = PdfReader(str(out))
    notes = "".join(page.extract_text() for page in reader.pages[2:])
    counts = (
        "4 items: 2 wrong (red clouds), 1 not compared (amber numbers, not clouded), "
        "1 right (listed, not marked)."
    )
    assert counts in " ".join(notes.split())
    assert " ".join(LEGEND.split()[:6]) in notes
    # The biggest Wrong items named first, by their numbers (#1569).
    assert "Biggest first: AI 1 p.2 Second sheet outlet" in " ".join(notes.split())
    on_drawing = notes.index("On the drawing (2)")
    page1, page2 = notes.index("Page 1", on_drawing), notes.index("Page 2", on_drawing)
    assert (
        on_drawing
        < page1
        < notes.index("Bay sizes", on_drawing)
        < page2
        < notes.index("Second sheet outlet", on_drawing)
    )
    elsewhere = notes.index("Not on the drawing (1)")
    assert elsewhere < notes.index("Backsplash height", on_drawing)
    assert notes.index("Right (not marked) (1)") < notes.index("Sink centre")
    assert "Not compared, AI reading only" in notes and 'Needs: 37"' in notes
    assert "Check" not in notes
    # A small picture of each drawn spot, none for the others.
    with pikepdf.open(out) as opened:
        images = [
            name
            for page in opened.pages[2:]
            for name, obj in (page.resources.get("/XObject") or {}).items()
            if obj.get("/Subtype") == pikepdf.Name.Image
        ]
    assert len(images) == 2


def test_the_words_match_the_vocabulary() -> None:
    wrong = note_text(1, _finding())
    assert wrong == 'Outlet height: vendor 46", make it 37" (architect 3\'-1")'
    chain = note_text(1, _finding(architect="1'-0\", 1'-6\" wall to wall", inches={}))
    assert chain == 'Outlet height: vendor 46"; architect 1\'-0", 1\'-6" wall to wall'
    decision = note_text(2, _finding(status="check", trust_level="to_check"))
    assert decision.startswith("Not compared: Outlet height") and "Check" not in decision
    # An AI "different" that no independent check confirmed is still Wrong (the screen's count);
    # its note ends with the grey trust words.
    unconfirmed = _finding(trust_level="to_check")
    assert note_text(3, unconfirmed).startswith('Outlet height: vendor 46", make it 37"')
    assert chat_drawing.trust_tag(unconfirmed) == "AI reading only"
    assert chat_drawing.trust_tag(_finding(trust_level="likely")) == "Likely"
    assert chat_drawing.trust_tag(_finding()) == ""
    assert chat_drawing.trust_tag(_finding(status="check", trust_level="to_check")) == ""


@pytest.mark.parametrize("rotate", [180, 270])
def test_other_rotations_keep_the_stamp_and_notes_on_the_page(tmp_path: Path, rotate: int) -> None:
    sheet = _sheet(tmp_path / "sheet.pdf", rotate=rotate, ink=False)
    box = (300.0, 300.0, 340.0, 316.0)
    out = tmp_path / "out.pdf"
    markup_pdf(sheet, _placed([_finding(vendor='99"', vendor_box=_box(*box))]), out)
    picture = _render(out)
    assert _red(picture, (box[2] + 2, box[1] - 4, box[2] + 14, box[3] + 4)) > 5
    assert _red(picture, (20, 10, 300, 40)) > 50  # the stamp, top left as the page is shown


def test_every_wrong_status_is_a_red_cloud_with_its_trust_words(tmp_path: Path) -> None:
    """The drawing's Wrong count is the screen's (#1541): status "different" is a red cloud
    whatever its trust; a note not Confirmed ends with the grey trust words."""
    sheet = _sheet(tmp_path / "sheet.pdf", ink=False)
    boxes = [
        (300.0, 300.0, 340.0, 316.0),
        (600.0, 150.0, 640.0, 166.0),
        (700.0, 450.0, 740.0, 466.0),
    ]
    findings = [
        _finding(item="Confirmed one", vendor='99"', vendor_box=_box(*boxes[0])),
        _finding(
            item="Read only", vendor='98"', trust_level="to_check", vendor_box=_box(*boxes[1])
        ),
        _finding(item="Likely one", vendor='97"', trust_level="likely", vendor_box=_box(*boxes[2])),
        _finding(item="Bay sizes", status="check", trust_level="to_check", vendor_box=None),
    ]
    out = tmp_path / "out.pdf"
    markup_pdf(sheet, _placed(findings), out)
    picture = _render(out)
    for box in boxes:
        assert _red(picture, (box[2] + 2, box[1] - 4, box[2] + 14, box[3] + 4)) > 5
    reader = PdfReader(str(out))
    page = " ".join(reader.pages[0].extract_text().split())
    assert "AI reading only" in page and "Likely" in page
    assert page.count('make it 37"') == 3
    notes = " ".join("".join(p.extract_text() for p in reader.pages[1:]).split())
    assert (
        "4 items: 3 wrong (red clouds; 1 AI reading only, 1 Likely), "
        "1 not compared (amber numbers, not clouded), 0 right (listed, not marked)."
    ) in notes
    assert "On the drawing (3)" in notes
