"""Every red mark proved on its own card, in one sentence the letter and the register share (#1804).

On synthetic drawings and findings, $0:

- the sentence: the vendor's value, the should-be, the reference and by how much it is off, for an
  architect size, the vendor's own sum (written out), GV's rule range, a missing finish, the brand
  prototype as the reference and the reviewer's own value (#1803);
- how it was checked, in the vendor's two words, never the reviewer's login or internal words;
- the rendered proof card: the C-number and title, the sentence, both snippets ringed (red and
  green), the reference; a small value zoomed to a readable size; the cover's drawn legend;
- the sheet's note never cut, and placed clear of the sheet's printed numbers;
- the letter and the register print the card's sentence word for word.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from app.review.assistant import proof, proof_drawing, proof_words
from app.review.assistant.proof_drawing import GREEN, RED
from app.review.assistant.send_package import (
    PackageContext,
    check_state,
    letter_of,
    register_rows,
    round_reason_words,
)
from app.review.assistant.vendor_text import internal
from tests.review.assistant.test_proof_drawing import (
    ARCHITECT_37,
    VENDOR_30,
    VENDOR_46,
    _box,
    _count,
    _decision,
    _draw,
    _proved,
    _render,
    _words,
)
from tests.review.assistant.test_send_package import FORBIDDEN, IDENTITY

#: Never on a vendor's file (the reviewer's login in the fixtures, the check's machinery).
GONE = ("ann@example.com", "blind", "advocate", "Agreed by", "AI reading", "witness")


def _context() -> PackageContext:
    return PackageContext(
        identity=IDENTITY,
        date="2026-10-20",
        round_no=1,
        rounds=None,
        state=check_state(incomplete=None, page_map=None, rounds=None),
        labels={"F1": "C-02", "F5": "C-05", "F3": "C-09"},
    )


def _summed(**changes: Any) -> dict[str, Any]:
    return _proved(
        id="F5",
        item="Run height string",
        source="vendor_arithmetic",
        created_by="consistency",
        architect='90 1/2"',
        vendor='88 1/2"',
        parts=['2"', '86 1/2"', '2"'],
        printed_total='88 1/2"',
        parts_sum="90.5",
        witnesses=["arithmetic"],
        inches={"architect": None, "vendor": "88.5"},
        arch_box=None,
        critic=None,
        **changes,
    )


def _reviewer(**changes: Any) -> dict[str, Any]:
    """A Not-compared item the reviewer made Wrong with their own value (#1803)."""
    return _decision(
        inches={"architect": None, "vendor": "30"},
        decision={
            "decision": "agree",
            "should_be": '28"',
            "note": "Per the appliance cut sheet.",
            "by": "ann@example.com",
        },
        **changes,
    )


# ---- the words ----------------------------------------------------------------------------------


def test_one_sentence_says_what_is_wrong_what_it_should_be_and_by_how_much() -> None:
    assert proof_words.sentence(_proved()) == (
        'Vendor shows 46" for the buffet outlet height; the architect shows 37" (printed 3\'-1"). '
        'Change it to 37" (9" over).'
    )
    assert proof_words.sentence(_proved(vendor='30"', inches={"architect": "37", "vendor": "30"}))
    assert proof_words.difference_words(_proved(inches={"architect": "37", "vendor": "30"})) == (
        '7" short'
    )
    # The vendor's own sum, written out.
    summed = _summed()
    assert proof_words.sentence(summed) == (
        'Vendor prints 88 1/2" for the run height string, but its own parts add up to 90 1/2" '
        '(2" more than the total). Correct the total to 90 1/2", or the parts so they add up to '
        '88 1/2".'
    )
    assert proof_words.sum_line(summed) == '2" + 86 1/2" + 2" = 90 1/2", not the 88 1/2" printed'
    assert proof_words.reference_words(summed) == "The vendor's own parts (their sum)"
    assert proof_words.sum_line(_proved()) is None


def test_a_rule_range_a_missing_finish_and_the_prototype_read_plainly() -> None:
    filler = _proved(
        item="End filler width",
        source="rules",
        architect="GV rule",
        vendor='5" filler each end',
        witnesses=["rule"],
        inches={"architect": None, "vendor": "5"},
        settled={
            "outcome": "wrong",
            "method": "gv_limits",
            "how": 'GV\'s filler limits are 1" to 2" (rules.md, the settings in force for this '
            'set); the vendor draws 5" fillers.',
        },
    )
    assert proof_words.sentence(filler) == (
        'Vendor shows 5" for the end filler width; GV\'s filler rule requires 1" to 2". Change it '
        'so it is between 1" and 2" (3" over).'
    )
    # GV's working, without GV's own file names.
    assert proof_words.worked_line(filler) == (
        'GV\'s filler limits are 1" to 2"; the vendor draws 5" fillers'
    )
    assert proof_words.reference_words(filler) == "GV filler rule"
    finish = _proved(
        item="Breakroom: cabinet finish FIN-1 missed",
        architect="FIN-1",
        vendor="not on the vendor's sheets",
        witnesses=["text"],
        inches={},
        vendor_box=None,
        arch_box={**_box(ARCHITECT_37), "page": 2},
    )
    assert proof_words.sentence(finish) == (
        "The vendor's sheets do not show it; the architect shows FIN-1. Add FIN-1."
    )
    assert proof_words.reference_words(finish) == "Architect drawing, page 2"
    prototype = _proved(source="brand_prototype", reference="brand_prototype_reference")
    assert proof_words.reference_kind(prototype) == "prototype"
    assert 'the brand prototype shows 37"' in proof_words.sentence(prototype)


def test_the_reviewers_value_is_the_green_should_be_and_never_their_login() -> None:
    finding = _reviewer()
    # The reviewer's Agree with a value makes a Not-compared item a red mark (#1803).
    assert proof.should_be_value(finding) == '28"'
    assert proof.proof_group(finding) == "clouded"
    assert proof_words.reference_kind(finding) == "reviewer"
    assert proof_words.sentence(finding) == (
        'Vendor shows 30" for the counter overall width; GV\'s reviewer requires 28". Change it '
        'to 28" (2" over).'
    )
    assert proof_words.reference_words(finding) == (
        "Decided by GV's reviewer: Per the appliance cut sheet."
    )
    assert proof_words.checked_words(finding) == proof_words.REVIEWER_WORDS
    # An Agree on a code-proved item keeps code's proof and says the reviewer agrees.
    agreed = _proved(decision={"decision": "agree", "by": "ann@example.com"})
    assert proof_words.checked_words(agreed).endswith("GV's reviewer agrees")
    for found in (finding, agreed, _proved(), _summed()):
        words = " ".join(
            (
                proof_words.sentence(found),
                proof_words.reference_words(found),
                proof_words.checked_words(found),
            )
        )
        assert not internal(words), words
        for gone in GONE:
            assert gone not in words, (gone, words)


def test_numbers_are_read_as_a_reviewer_writes_them() -> None:
    read = proof_words.inches_of
    assert [read(t) for t in ('97 1/2"', '98"', "3'-1\"", "1' - 2\"", '3/4"', "37")] == [
        pytest.approx(97.5),
        98,
        37,
        14,
        pytest.approx(0.75),
        37,
    ]
    assert read("407 [16]") is None and read("FIN-1") is None
    assert (
        round_reason_words(
            "round 2 is a different view or redrawn (its values miss by 83 pt when lined up); "
            "code would not guess"
        )
        == "round 2 is a different view or redrawn, so it could not be lined up with round 1"
    )


# ---- the rendered card ----------------------------------------------------------------------------


def test_the_proof_card_rings_both_values_and_says_it_in_one_sentence(tmp_path: Path) -> None:
    out = _draw(tmp_path, [_proved(comment_no="C-02"), _reviewer(comment_no="C-09")])
    reader = PdfReader(str(out))
    assert list(reader.page_labels) == ["Cover", "1", "Proof 1", "Notes 1"]
    cover = _words(reader.pages[0].extract_text())
    for words in ("Red cloud:", "Green:", "07 = C-07:", "C-02", "C-09"):
        assert words in cover, words
    card = _words(reader.pages[2].extract_text())
    for words in (
        "C-02 Buffet outlet height",
        proof_words.sentence(_proved()),
        "VENDOR'S DRAWING · PAGE 1",
        "ARCHITECT DRAWING · SAME SHEET",
        "Reference Architect drawing, on the same sheet",
        "C-09 Counter overall width",
        "GV'S REVIEWER",
        "Reference Decided by GV's reviewer: Per the appliance cut sheet.",
    ):
        assert words in card, words
    whole = _words("".join(page.extract_text() for page in reader.pages))
    for gone in GONE:
        assert gone not in whole, gone
    assert FORBIDDEN.search(card) is None
    picture = _render(out, 2)
    # Both rings: the vendor's value in red, the architect's in green.
    assert _count(picture, RED) > 150 and _count(picture, GREEN) > 150
    sheet = _words(reader.pages[1].extract_text())
    assert 'C-02 Buffet outlet height 46" should be 37"' in sheet
    assert '30" should be 28"' in sheet and "…" not in sheet


def _small_sheet(path: Path) -> tuple[Path, tuple[float, float, float, float]]:
    """An A3 sheet with a 4 pt value among other numbers."""
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(1191, 842), invariant=1)
    c.setFont("Helvetica", 4)
    for n in range(40):
        c.drawString(100 + 25 * n, 842 - 400, f"{n}")
    c.drawString(600, 842 - 420, '46"')
    c.save()
    path.write_bytes(out.getvalue())
    width = c.stringWidth('46"', "Helvetica", 4)
    return path, (599.0, 420 - 3.5, 601 + width, 420 + 1.5)


def test_a_small_value_on_a_large_sheet_is_zoomed_to_read(tmp_path: Path) -> None:
    sheet, spot = _small_sheet(tmp_path / "small.pdf")
    panel = proof_drawing.panel_size((1191.0, 842.0))
    shot = proof_drawing.snippet(sheet, 1, spot, panel, proof_drawing._Sizes())
    assert shot is not None
    left, top, right, bottom = shot.window
    drawn = min(panel[0] / (right - left), panel[1] / (bottom - top))
    assert 4 * drawn >= 12  # the 4 pt value is at least 12 pt on the card
    assert left <= spot[0] and spot[2] <= right and top <= spot[1] and spot[3] <= bottom


def test_the_note_keeps_clear_of_the_sheets_numbers(tmp_path: Path) -> None:
    import pypdfium2 as pdfium  # type: ignore[import-untyped]

    from app.review.assistant.chat_drawing import Mark

    sheet = tmp_path / "busy.pdf"
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(1000.0, 700.0), invariant=1)
    c.setFont("Helvetica", 10)
    for row in range(8):
        for col in range(6):
            c.drawString(450 + 40 * col, 700 - (440 + 18 * row), f"{row}{col}")
    c.save()
    sheet.write_bytes(out.getvalue())
    finding = _proved(comment_no="C-02", vendor_box=_box(VENDOR_46))
    x, y = VENDOR_46
    mark = Mark(1, finding, 1, (x - 2, y - 10, x + 22, y + 3), "wrong", True, '46"')
    placed = [proof_drawing._Placed(mark, mark.box, None)]
    document = pdfium.PdfDocument(str(sheet))
    try:
        numbers = proof_drawing.number_boxes(document, 0)
    finally:
        document.close()
    assert len(numbers) >= 40
    proof_drawing._layout((1000.0, 700.0), placed, None, numbers)
    note = placed[0].note
    assert note is not None
    assert not any(proof_drawing._overlap(note, box) for box in numbers)


# ---- the letter and the register say the same ---------------------------------------------------------


def test_the_letter_and_the_register_print_the_cards_sentence() -> None:
    findings = [_proved(), _summed(vendor_box=_box(VENDOR_30)), _reviewer()]
    context = _context()
    letter = letter_of(findings, context, drawing_set="sheet.pdf")
    by_label = {item.label: item for item in letter.corrections}
    for finding, label in zip(findings, ("C-02", "C-05", "C-09"), strict=True):
        assert by_label[label].words == proof_words.sentence(finding)
        assert by_label[label].checked == proof_words.checked_words(finding)
    assert by_label["C-05"].sum_line == proof_words.sum_line(findings[1])
    rows = {row[0]: row for row in register_rows(findings, context) if row[0]}
    assert rows["C-02"][7] == proof_words.sentence(findings[0])
    assert rows["C-05"][7] == (
        proof_words.sentence(findings[1]) + f" Sum: {proof_words.sum_line(findings[1])}."
    )
    assert rows["C-09"][9] == '28"' and rows["C-09"][10].startswith("Decided by GV's reviewer")
    for row in rows.values():
        for cell in row:
            assert FORBIDDEN.search(cell) is None, cell
            for gone in GONE:
                assert gone not in cell, (gone, cell)
