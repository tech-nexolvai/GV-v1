"""The zero-click red/green proof drawing (#1581), on synthetic PDFs, $0.

- which items code clouds by itself (only Wrong items code proved), and where the rest go;
- the colours: red and green legible on white and in greyscale print;
- the words: "Vendor: 46"" in red, "Should be: 37" (architect 3'-1", page 2)" in green;
- the rendered PDF: cover counts, stamp, assumptions, the sheet's note, the proof entry, the grey
  lists, the page labels; the reviewer's decisions as overrides;
- the plan check never waits: code's likely answers, written on the cover.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import numpy as np
import pikepdf
import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pytest
from pypdf import PdfReader
from reportlab.lib.colors import Color  # type: ignore[import-untyped]
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from app.review.assistant.proof import (
    RULE_LINE,
    code_proved,
    governs,
    proof_counts,
    proof_group,
    proof_line,
    proof_of,
    should_be,
    vendor_value,
)
from app.review.assistant.proof_drawing import (
    GREEN,
    RED,
    STAMP_WORDS,
    contrast,
    proof_markup_pdf,
    stamp_line,
)

WIDTH, HEIGHT = 1000.0, 700.0
#: Where the sheet prints its values (shown points, origin top-left: x, baseline y).
VENDOR_46 = (600.0, 520.0)
ARCHITECT_37 = (200.0, 200.0)
VENDOR_12 = (400.0, 600.0)
VENDOR_30 = (800.0, 300.0)


def _sheet(path: Path, pages: int = 1) -> Path:
    """A sheet: the architect's 3'-1" and the vendor's 46", 12" and 30" printed as text."""
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(WIDTH, HEIGHT), invariant=1)
    for _ in range(pages):
        c.setFont("Helvetica", 10)
        c.drawString(ARCHITECT_37[0], HEIGHT - ARCHITECT_37[1], "3'-1\"")
        c.drawString(VENDOR_46[0], HEIGHT - VENDOR_46[1], '46"')
        c.drawString(VENDOR_12[0], HEIGHT - VENDOR_12[1], '12"')
        c.drawString(VENDOR_30[0], HEIGHT - VENDOR_30[1], '30"')
        c.showPage()
    c.save()
    path.write_bytes(out.getvalue())
    return path


def _box(at: tuple[float, float], page: int = 1, file: str = "sheet.pdf") -> dict[str, Any]:
    x, y = at
    return {"page": page, "x0": x - 2, "y0": y - 10, "x1": x + 22, "y1": y + 3, "file": file}


def _checks(**changes: Any) -> dict[str, Any]:
    checks: dict[str, Any] = {
        "printed_architect": "match",
        "printed_vendor": "match",
        "second_read": "agrees",
        "rule": None,
        "arithmetic": None,
        "lead_checked": True,
        "ledger": None,
        "units": None,
        "adjudication": None,
        "critic": None,
    }
    checks.update(changes)
    return checks


def _proved(**changes: Any) -> dict[str, Any]:
    """A Wrong item code proved: both values printed at their spots, read again by a model."""
    finding: dict[str, Any] = {
        "id": "F1",
        "page": 1,
        "vs_page": None,
        "item": "Buffet outlet height",
        "category": "cabinet",
        "architect": "3'-1\"",
        "vendor": '46"',
        "status": "different",
        "source": "architect",
        "why": "outlet 9 in higher",
        "trust_level": "confirmed",
        "witnesses": ["printed"],
        "checks": _checks(),
        "inches": {"architect": "37", "vendor": "46", "difference": "9"},
        "vendor_box": _box(VENDOR_46),
        "arch_box": _box(ARCHITECT_37),
    }
    finding.update(changes)
    return finding


def _ai_only(**changes: Any) -> dict[str, Any]:
    return (
        _proved(
            id="F2",
            item="Base cabinet height",
            architect='34"',
            vendor='12"',
            trust_level="to_check",
            witnesses=[],
            checks=_checks(printed_architect="no_text", printed_vendor="no_text"),
            inches={"architect": "34", "vendor": "12", "difference": "-22"},
            vendor_box=_box(VENDOR_12),
            arch_box=None,
        )
        | changes
    )


def _decision(**changes: Any) -> dict[str, Any]:
    return (
        _proved(
            id="F3",
            item="Counter overall width",
            architect="no width printed",
            vendor='30"',
            status="check",
            trust_level="to_check",
            witnesses=[],
            keep_reason="architect_prints_nothing",
            question="The architect prints no size: is the vendor's 30\" right?",
            vendor_box=_box(VENDOR_30),
            arch_box=None,
        )
        | changes
    )


def _right(**changes: Any) -> dict[str, Any]:
    return (
        _proved(
            id="F4",
            item="Toe kick height",
            architect='4"',
            vendor='4"',
            status="same",
            compared=True,
            vendor_box=None,
            arch_box=None,
        )
        | changes
    )


# ---- which items code clouds by itself ---------------------------------------------------------


@pytest.mark.parametrize(
    ("finding", "group"),
    [
        (_proved(), "clouded"),
        (_proved(trust_level="likely", checks=_checks(second_read="not_run")), "clouded"),
        (_proved(witnesses=["rule"], source="rules", rule_check_id="C1"), "clouded"),
        (_ai_only(), "not_verified"),
        # Two AI readings agreeing are Likely, but no code witness: never clouded alone.
        (_ai_only(trust_level="likely"), "not_verified"),
        # A check that disagrees makes it "AI reading only".
        (_proved(trust_level="to_check"), "not_verified"),
        (_decision(), "could_not_compare"),
        (_right(), "right"),
        (_right(compared=False), "not_compared"),
        # The reviewer's decision overrides, both ways.
        (_ai_only(decision={"decision": "agree", "by": "R"}), "clouded"),
        (_decision(decision={"decision": "agree", "by": "R"}), "clouded"),
        (_proved(decision={"decision": "not_a_problem", "by": "R"}), "dismissed"),
    ],
)
def test_each_item_goes_to_its_group_by_trust_and_decision(
    finding: dict[str, Any], group: str
) -> None:
    assert proof_group(finding) == group


def test_a_scaled_value_or_a_vendor_side_only_witness_is_never_clouded() -> None:
    # The printed scales are wrong wherever GV resized a pasted drawing: never proof.
    assert not code_proved(_proved(architect='no size printed; scales about 74"'))
    assert not code_proved(_proved(check_reason="scaled"))
    # Millimetres and inches agreeing back the vendor's reading only: with an architect chain
    # code cannot read as one size, code does not know what it should be.
    chain = _proved(
        architect='1\'-0", 1\'-6", 8" wall to wall (no overall printed)',
        witnesses=["units"],
        inches={"architect": None, "vendor": "107.75", "difference": None},
    )
    assert not code_proved(chain)
    assert code_proved({**chain, "inches": {"architect": "106", "vendor": "107.75"}})


def test_counts_split_the_screen_counts_and_follow_decisions() -> None:
    findings = [_proved(), _ai_only(), _decision(), _right(), _right(compared=False)]
    assert proof_counts(findings) == {
        "clouded": 1,
        "not_verified": 1,
        "could_not_compare": 1,
        "right": 1,
        "not_compared": 1,
        "dismissed": 0,
    }
    decided = [_proved(decision={"decision": "not_a_problem", "by": "R"}), _ai_only()]
    assert proof_counts(decided)["dismissed"] == 1 and proof_counts(decided)["clouded"] == 0


# ---- colours and words ---------------------------------------------------------------------------


def test_red_and_green_read_on_white_and_stay_dark_in_greyscale() -> None:
    assert contrast(RED) >= 4.5
    assert contrast(GREEN) >= 4.5
    for colour in (RED, GREEN):
        grey = 0.299 * colour.red + 0.587 * colour.green + 0.114 * colour.blue
        assert grey < 0.45  # prints as a dark grey, never washed out
    assert contrast(Color(1, 1, 1)) == pytest.approx(1.0)


def test_the_words_say_what_is_wrong_and_what_it_should_be() -> None:
    finding = _proved(vs_page=2, arch_box={**_box(ARCHITECT_37), "page": 2})
    assert vendor_value(finding) == '46"'
    assert should_be(finding) == '37" (architect 3\'-1", page 2)'
    assert proof_line(finding) == (
        "Printed at both spots, read by code; a second reading agrees; checked on the drawing"
    )
    summed = _proved(
        source="vendor_arithmetic",
        parts=['40"', '40 1/8"'],
        printed_total='80"',
        parts_sum="80.125",
        witnesses=["arithmetic"],
        inches={"architect": None, "vendor": None},
    )
    assert vendor_value(summed) == '80"'
    assert should_be(summed) == "80 1/8\" (sum of the vendor's own parts)"
    assert proof_line(summed).startswith('Parts add up to 80 1/8", printed 80"')
    filler = _proved(
        source="rules",
        architect="GV rule 1-2",
        vendor='5" filler each end',
        witnesses=["rule"],
        inches={"architect": None, "vendor": None},
        settled={
            "outcome": "wrong",
            "method": "gv_limits",
            "how": 'GV\'s filler limits are 1" to 2" (rules.md, the settings in force for '
            'this set); the vendor draws 5" fillers.',
            "was": "check",
        },
    )
    assert vendor_value(filler) == '5"'
    assert should_be(filler) == '1" to 2" (GV filler rule)'
    assert governs(filler) == "GV filler rule"
    assert proof_line(filler).startswith("Settled by GV Review: GV's filler limits")
    assert proof_of(_decision())["reason"] == "the architect prints no size here"
    assert proof_of(_right())["should_be"] is None
    assert stamp_line("2026-10-18") == f"{STAMP_WORDS}: 2026-10-18"
    assert stamp_line("2026-10-18", ["Ann", "Ann", "Bo"]).endswith("and reviewed by Ann, Bo")


# ---- the rendered drawing ------------------------------------------------------------------------


def _placed(findings: list[dict[str, Any]]) -> list[tuple[int, Any, int | None, Any, str]]:
    out: list[tuple[int, Any, int | None, Any, str]] = []
    for number, finding in enumerate(findings, start=1):
        box = finding.get("vendor_box")
        if box is None:
            out.append((number, finding, None, None, "Not marked on the drawing: no box."))
            continue
        coords = tuple(float(box[k]) for k in ("x0", "y0", "x1", "y1"))
        out.append((number, finding, int(box["page"]), coords, f"Marked on page {box['page']}."))
    return out


def _render(pdf: Path, index: int) -> np.ndarray:
    document = pdfium.PdfDocument(str(pdf))
    try:
        return np.asarray(document[index].render(scale=1).to_pil().convert("RGB")).astype(int)
    finally:
        document.close()


def _count(picture: np.ndarray, colour: Color, box: tuple[float, ...] | None = None) -> int:
    area = picture
    if box is not None:
        x0, y0, x1, y1 = (int(v) for v in box)
        area = picture[max(0, y0) : y1, max(0, x0) : x1]
    target = np.array([colour.red, colour.green, colour.blue]) * 255
    return int((np.abs(area - target).max(axis=2) < 40).sum())


def _words(text: str) -> str:
    """The text with its line breaks as single spaces (a note's coloured pieces extract apart)."""
    return " ".join(text.split())


def _draw(tmp_path: Path, findings: list[dict[str, Any]], **kwargs: Any) -> Path:
    sheet = _sheet(tmp_path / "sheet.pdf")
    out = tmp_path / "proof.pdf"
    proof_markup_pdf(
        sheet,
        _placed(findings),
        out,
        set_name="sheet.pdf",
        stamp=stamp_line("2026-10-18", kwargs.pop("reviewers", ())),
        **kwargs,
    )
    return out


def test_the_drawing_clouds_only_what_code_proved_in_red_and_green(tmp_path: Path) -> None:
    findings = [_proved(), _ai_only(), _decision(), _right()]
    out = _draw(tmp_path, findings)
    reader = PdfReader(str(out))
    # Cover, the sheet, one proof page, notes.
    assert list(reader.page_labels) == ["Cover", "1", "Proof 1", "Notes 1"]
    cover = reader.pages[0].extract_text()
    for words in (
        "Checked automatically by GV Review: 2026-10-18",
        "1\nWrong, clouded",
        "1\nNot verified by code",
        "1\nCould not compare",
        "1\nRight",
        RULE_LINE[:60],
        "Buffet outlet height",
    ):
        assert words in cover, words
    assert "draft" not in cover.lower() and "AI markup" not in cover
    sheet = _words(reader.pages[1].extract_text())
    assert 'Vendor: 46"' in sheet
    assert 'Should be: 37" (architect 3\'-1")' in sheet
    assert "Base cabinet height" not in sheet and 'Vendor: 12"' not in sheet
    picture = _render(out, 1)
    x, y = VENDOR_46
    assert _count(picture, RED, (x - 20, y - 25, x + 40, y + 15)) > 20  # the cloud
    x, y = VENDOR_12
    assert _count(picture, RED, (x - 20, y - 25, x + 40, y + 15)) == 0  # AI reading only
    assert _count(picture, GREEN) > 20  # the "Should be" words
    proof = reader.pages[2].extract_text()
    assert "Proved by: Printed at both spots, read by code" in proof
    assert "Vendor page 1 (architect's drawing on the same sheet)" in proof
    proof_picture = _render(out, 2)
    assert _count(proof_picture, GREEN) > 50 and _count(proof_picture, RED) > 50  # the boxes
    notes = reader.pages[3].extract_text()
    assert "Not verified by code: check before sending (1)" in notes
    assert "Base cabinet height" in notes
    assert "Could not be compared (1)" in notes
    assert "Why: the architect prints no size here" in notes
    assert "Right (1)" in notes and "Toe kick height" in notes


def test_decisions_override_and_the_stamp_names_the_reviewer(tmp_path: Path) -> None:
    findings = [
        _proved(decision={"decision": "not_a_problem", "by": "Ann"}),
        _ai_only(decision={"decision": "agree", "by": "Ann"}),
    ]
    out = _draw(tmp_path, findings, reviewers=["Ann"])
    reader = PdfReader(str(out))
    assert "2026-10-18, and reviewed by Ann" in reader.pages[0].extract_text()
    sheet = _words(reader.pages[1].extract_text())
    assert 'Vendor: 12"' in sheet and 'Vendor: 46"' not in sheet
    words = "".join(page.extract_text() for page in reader.pages)
    assert "Agreed by Ann" in words
    assert "Not a problem (reviewer) (1)" in words


def test_a_drawing_with_nothing_proved_still_has_its_cover_and_lists(tmp_path: Path) -> None:
    out = _draw(tmp_path, [_ai_only(), _decision()], assumptions=["page 2 is the vendor's."])
    reader = PdfReader(str(out))
    assert list(reader.page_labels) == ["Cover", "1", "Notes 1"]
    cover = reader.pages[0].extract_text()
    assert "None: code proved no wrong value on this set." in cover
    assert "Assumed: page 2 is the vendor's." in cover


def test_the_proof_crops_leave_out_earlier_review_markup(tmp_path: Path) -> None:
    """GV's yellow box over the vendor's value is not in the proof crop (it is cut from the copy
    without earlier review markup), and the copy is removed afterwards."""
    sheet = _sheet(tmp_path / "sheet.pdf")
    x, y = VENDOR_46
    with pikepdf.open(sheet, allow_overwriting_input=True) as pdf:
        stream = pdf.make_stream(b"1 1 0 rg 0 0 60 30 re f")
        stream["/Type"] = pikepdf.Name.XObject
        stream["/Subtype"] = pikepdf.Name.Form
        stream["/BBox"] = pikepdf.Array([0, 0, 60, 30])
        square = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Annot,
                Subtype=pikepdf.Name.Square,
                Rect=pikepdf.Array([x - 20, HEIGHT - y - 15, x + 40, HEIGHT - y + 15]),
                AP=pikepdf.Dictionary(N=stream),
            )
        )
        pdf.pages[0].obj["/Annots"] = pikepdf.Array([square])
        pdf.save(sheet)
    out = tmp_path / "proof.pdf"
    proof_markup_pdf(sheet, _placed([_proved()]), out, set_name="sheet.pdf", stamp="x")
    yellow = Color(1, 1, 0)
    assert _count(_render(out, 2), yellow) == 0
    assert not list(tmp_path.glob("*.clean.pdf"))


# ---- the plan check never waits ----------------------------------------------------------------


def test_unsure_page_map_questions_get_code_s_likely_answer(tmp_path: Path) -> None:
    from app.review.assistant.full_check_plan import plan_assumptions, read_page_map

    page_map = {
        "files": ["set.pdf"],
        "pages": [
            {
                "file": "set.pdf",
                "page": 28,
                "area": "Buffet",
                "halves": [
                    {"role": "architect", "box": [0, 0, 100, 100]},
                    {"role": "general", "box": [0, 200, 100, 300]},
                ],
            }
        ],
    }
    (tmp_path / "page_map.json").write_text(json.dumps(page_map), encoding="utf-8")
    assumed = plan_assumptions(tmp_path, "set.pdf")
    assert assumed == ["page 28, the lower drawing (no heading) is the vendor's drawing."]
    plain = read_page_map(tmp_path)
    run = read_page_map(tmp_path, assume=True, main_file="set.pdf")
    assert plain is not None and run is not None
    assert plain["pages"][0]["halves"][1]["role"] == "general"  # the popover still asks
    assert run["pages"][0]["halves"][1] == {
        "role": "vendor",
        "box": [0, 200, 100, 300],
        "heading_by": "code",
    }
    # A reviewer's answer wins over code's assumption, and is no longer assumed.
    (tmp_path / "plan_answers.jsonl").write_text(
        json.dumps({"id": "role:f0:p28:h1", "answer": "general"}) + "\n", encoding="utf-8"
    )
    assert plan_assumptions(tmp_path, "set.pdf") == []
    answered = read_page_map(tmp_path, assume=True, main_file="set.pdf")
    assert answered is not None and answered["pages"][0]["halves"][1]["role"] == "general"
