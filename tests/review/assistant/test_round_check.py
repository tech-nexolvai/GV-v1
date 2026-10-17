"""The automatic round-2 check (#1603), on synthetic labels and sheets, $0.

- the fit lines round 1 up with round 2 by the values printed once in both, and refuses a
  different view rather than guess;
- Fixed / Still wrong (same value, or changed but still not right) / New / also in round 1;
- what code cannot compare goes to the grey list with the reason;
- the drawing: a green check at a fixed item, a red cloud at a still-wrong one, "NEW" on a new
  round-2 item, the cover's round-2 summary and the grey list.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import numpy as np
import pypdfium2 as pdfium  # type: ignore[import-untyped]
from pypdf import PdfReader
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from app.review.assistant.proof_drawing import GREEN, RED, proof_markup_pdf
from app.review.assistant.round_check import (
    MAX_FIT_PT,
    fit,
    labels_by_page,
    round_two,
)

WIDTH, HEIGHT = 1000.0, 700.0
#: Round 1 (page 1, left half) and round 2 (page 2, the same drawing shifted and scaled).
SCALE, SHIFT = 1.1, (40.0, 25.0)
ROUND_ONE: dict[str, tuple[float, float]] = {
    '10"': (100.0, 100.0),
    '20"': (200.0, 120.0),
    '30"': (150.0, 300.0),
    '40"': (300.0, 250.0),
    '46"': (250.0, 400.0),  # the wrong value, fixed in round 2
    '12"': (120.0, 450.0),  # the wrong value, still wrong in round 2
    '50"': (380.0, 150.0),
}
ROUND_TWO_VALUES = {'46"': '37"', '12"': '12"'}


def _moved(point: tuple[float, float]) -> tuple[float, float]:
    return SCALE * point[0] + SHIFT[0], SCALE * point[1] + SHIFT[1]


def _label(text: str, at: tuple[float, float], n: int, side: str = "vendor") -> dict[str, Any]:
    inches = text.rstrip('"')
    x, y = at
    return {
        "id": f"D{n}",
        "text": text,
        "inches": inches,
        "box": [x - 8, y - 5, x + 8, y + 5],
        "side": side,
        "orientation": "h",
    }


def _dims(second: dict[str, tuple[float, float]] | None = None) -> dict[str, Any]:
    first = [_label(t, at, n) for n, (t, at) in enumerate(ROUND_ONE.items(), start=1)]
    if second is None:
        second = {ROUND_TWO_VALUES.get(t, t): _moved(at) for t, at in ROUND_ONE.items()}
    later = [_label(t, at, n) for n, (t, at) in enumerate(second.items(), start=1)]
    later.append(_label('8"', (900.0, 650.0), 99))  # round 2's new value (a new item)
    return {
        "pages": [
            {"file": "sheet.pdf", "page": 1, "labels": first},
            {"file": "sheet.pdf", "page": 2, "labels": later},
        ]
    }


def _page_map(rounds: tuple[int, int] = (1, 2)) -> dict[str, Any]:
    return {
        "files": ["sheet.pdf"],
        "pages": [
            {
                "file": "sheet.pdf",
                "page": page,
                "area": "Reception",
                "section": "1. Lobby",
                "halves": [{"role": "vendor", "round": r, "box": [0, 0, WIDTH, HEIGHT]}],
            }
            for page, r in zip((1, 2), rounds, strict=True)
        ],
    }


def _finding(fid: str, text: str, architect: str, page: int = 1, **more: Any) -> dict[str, Any]:
    points = ROUND_ONE.get(text) if page == 1 else (900.0, 650.0)
    assert points is not None
    x, y = points
    finding: dict[str, Any] = {
        "id": fid,
        "page": page,
        "round": page,
        "item": f"Item {fid}",
        "architect": architect,
        "vendor": text,
        "status": "different",
        "source": "architect",
        "trust_level": "confirmed",
        "witnesses": ["printed"],
        "checks": {"printed_architect": "match", "printed_vendor": "match", "lead_checked": True},
        # Both numbers measure the same thing, proved by code (#1581): so it is clouded.
        "pairing": {"proved": True, "method": "code_geometry"},
        "inches": {"architect": architect.rstrip('"'), "vendor": text.rstrip('"')},
        "vendor_box": {
            "page": page,
            "x0": x - 10,
            "y0": y - 8,
            "x1": x + 10,
            "y1": y + 8,
            "file": "sheet.pdf",
        },
    }
    finding.update(more)
    return finding


def test_the_fit_lines_the_rounds_up_and_refuses_another_view() -> None:
    labels = labels_by_page(_dims())
    first, second = labels[("sheet.pdf", 1)], labels[("sheet.pdf", 2)]
    found, why = fit(first, second)
    assert why is None and found is not None
    assert found.anchors == 6  # 10, 20, 30, 40, 50 and the unchanged 12": printed once in both
    assert found.error_pt < 0.01
    assert np.allclose(found.place((100.0, 100.0)), _moved((100.0, 100.0)))
    # A different view: the same values, scattered.
    scattered = {t: (at[1] * 1.7, at[0] * 0.6 + 90) for t, at in ROUND_ONE.items()}
    other = labels_by_page(_dims(scattered))[("sheet.pdf", 2)]
    refused, why = fit(first, other)
    assert refused is None and why is not None and "different view" in why
    assert MAX_FIT_PT == 2.0
    # Too few values in both: refused, never a guess.
    none, why = fit(first[:2], second[:2])
    assert none is None and why is not None and "too few" in why


def test_fixed_still_wrong_and_new_are_decided_by_the_printed_values() -> None:
    findings = [
        _finding("F1", '46"', '37"'),  # round 2 prints 37": fixed
        _finding("F2", '12"', '14"'),  # round 2 prints 12" again: still wrong
        _finding("F3", '8"', '6"', page=2),  # round 2 only: new
        _finding("F4", '30"', '31"', trust_level="to_check", witnesses=[]),  # not clouded
    ]
    report = round_two(findings, _dims(), _page_map())
    by = report.by_finding()
    assert by["F1"].status == "fixed" and (by["F1"].was, by["F1"].now) == ('46"', '37"')
    assert by["F1"].page == 2 and by["F1"].spot is not None
    assert by["F2"].status == "still_wrong" and by["F2"].now == '12"'
    assert by["F3"].status == "new" and by["F3"].reason == "round 1 printed nothing here"
    assert "F4" not in by  # only what code clouded is followed
    assert report.counts() == {"fixed": 1, "still_wrong": 1, "new": 1, "not_compared": 0}
    assert report.line() == "Round 2: 1 fixed, 1 still wrong, 1 new."


def test_a_change_that_is_still_not_right_is_still_wrong() -> None:
    second = {ROUND_TWO_VALUES.get(t, t): _moved(at) for t, at in ROUND_ONE.items()}
    second['36"'] = second.pop('37"')
    item = round_two([_finding("F1", '46"', '37"')], _dims(second), _page_map()).items[0]
    assert item.status == "still_wrong" and item.now == '36"'


def test_what_code_cannot_compare_is_grey_with_the_reason() -> None:
    scattered = {t: (at[1] * 1.7, at[0] * 0.6 + 90) for t, at in ROUND_ONE.items()}
    item = round_two([_finding("F1", '46"', '37"')], _dims(scattered), _page_map()).items[0]
    assert item.status == "not_compared" and item.reason is not None
    assert "code would not guess" in item.reason
    # A rule item code cannot put a number on, changed in round 2: never called fixed.
    rule = _finding(
        "F2", '46"', '37"', inches={"architect": None, "vendor": "46"}, witnesses=["rule"]
    )
    item = round_two([rule], _dims(), _page_map()).items[0]
    assert item.status == "not_compared" and "does not know the value" in str(item.reason)
    # The drawings' ink changed where the value did not: the two witnesses disagree.
    still = _finding("F3", '12"', '14"')
    item = round_two(
        [still],
        _dims(),
        _page_map(),
        zones=lambda *_pages: [(100.0, 440.0, 140.0, 460.0)],
    ).items[0]
    assert item.status == "not_compared" and "ink" in str(item.reason)
    # A set with one round says nothing about rounds.
    assert round_two([_finding("F1", '46"', '37"')], _dims(), _page_map((1, 1))).items == []


def _sheet(path: Path) -> Path:
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(WIDTH, HEIGHT), invariant=1)
    for page in (1, 2):
        c.setFont("Helvetica", 9)
        values = ROUND_ONE if page == 1 else _dims()["pages"][1]["labels"]
        if page == 1:
            for text, (x, y) in ROUND_ONE.items():
                c.drawString(x - 6, HEIGHT - y - 3, text)
        else:
            for label in values:
                x0, _y0, _x1, y1 = label["box"]
                c.drawString(x0 + 2, HEIGHT - y1 + 2, label["text"])
        c.showPage()
    c.save()
    path.write_bytes(out.getvalue())
    return path


def _colour_near(pdf: Path, index: int, at: tuple[float, float], colour: Any) -> int:
    document = pdfium.PdfDocument(str(pdf))
    try:
        picture = np.asarray(document[index].render(scale=1).to_pil().convert("RGB")).astype(int)
    finally:
        document.close()
    x, y = (int(v) for v in at)
    area = picture[max(0, y - 20) : y + 20, max(0, x - 25) : x + 25]
    target = np.array([colour.red, colour.green, colour.blue]) * 255
    return int((np.abs(area - target).max(axis=2) < 40).sum())


def test_the_drawing_marks_round_two_and_summarises_it(tmp_path: Path) -> None:
    sheet = _sheet(tmp_path / "sheet.pdf")
    findings = [
        _finding("F1", '46"', '37"'),
        _finding("F2", '12"', '14"'),
        _finding("F3", '8"', '6"', page=2),
    ]
    report = round_two(findings, _dims(), _page_map())
    placed = []
    for n, f in enumerate(findings, start=1):
        box = f["vendor_box"]
        coords = tuple(float(box[k]) for k in ("x0", "y0", "x1", "y1"))
        placed.append((n, f, int(box["page"]), coords, f"Marked on page {box['page']}."))
    out = tmp_path / "proof.pdf"
    proof_markup_pdf(sheet, placed, out, set_name="sheet.pdf", stamp="Checked", rounds=report)
    reader = PdfReader(str(out))
    cover = " ".join(reader.pages[0].extract_text().split())
    assert "Round 2: what the vendor did" in cover
    assert "Round 2: 1 fixed, 1 still wrong, 1 new." in cover
    round_two_page = " ".join(reader.pages[2].extract_text().split())
    assert 'Fixed in round 2: was 46"' in round_two_page and 'now 37"' in round_two_page
    assert 'Still wrong in round 2: now 12"' in round_two_page
    assert "NEW Item F3" in round_two_page
    fixed_at, still_at = _moved(ROUND_ONE['46"']), _moved(ROUND_ONE['12"'])
    assert _colour_near(out, 2, fixed_at, GREEN) > 10  # the green box at the fixed value
    assert _colour_near(out, 2, fixed_at, RED) == 0  # no red cloud there
    assert _colour_near(out, 2, still_at, RED) > 10  # the red cloud at the still-wrong one


def test_the_grey_list_names_what_round_two_could_not_compare(tmp_path: Path) -> None:
    sheet = _sheet(tmp_path / "sheet.pdf")
    scattered = {t: (at[1] * 1.7, at[0] * 0.6 + 90) for t, at in ROUND_ONE.items()}
    finding = _finding("F1", '46"', '37"')
    report = round_two([finding], _dims(scattered), _page_map())
    box = finding["vendor_box"]
    coords = tuple(float(box[k]) for k in ("x0", "y0", "x1", "y1"))
    out = tmp_path / "proof.pdf"
    proof_markup_pdf(
        sheet,
        [(1, finding, 1, coords, "Marked on page 1.")],
        out,
        set_name="sheet.pdf",
        stamp="Checked",
        rounds=report,
    )
    notes = " ".join(PdfReader(str(out)).pages[-1].extract_text().split())
    assert "Round 2: could not be compared by code (1)" in notes
    assert "code would not guess" in notes


def test_the_full_check_reads_the_set_pack_for_round_two(tmp_path: Path) -> None:
    """`make_proof_file`'s round-2 step: the pack's labels and page map, the ink check run on the
    real pages, the same answer."""
    import json
    from types import SimpleNamespace

    from app.review.assistant.chat_findings import round_two_report

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    _sheet(uploads / "sheet.pdf")
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "dims.json").write_text(json.dumps(_dims()), encoding="utf-8")
    (pack / "page_map.json").write_text(json.dumps(_page_map()), encoding="utf-8")
    workspace = SimpleNamespace(uploads=uploads)
    findings = [_finding("F1", '46"', '37"'), _finding("F2", '12"', '14"')]
    report = round_two_report(workspace, "sheet.pdf", findings, pack)  # type: ignore[arg-type]
    assert report is not None
    assert {i.finding_id: i.status for i in report.items} == {"F1": "fixed", "F2": "still_wrong"}
    assert round_two_report(workspace, "sheet.pdf", findings, None) is None  # type: ignore[arg-type]
