"""Countertop summaries repeat recorded result data without re-judging it."""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction
from io import BytesIO
from uuid import UUID

from openpyxl import load_workbook
from pypdf import PdfReader

from app.schemas.visual_ui import (
    AgreementFactsOut,
    CountertopPieceOut,
    CountertopResultOut,
    ExactValueOut,
    HoldOut,
    ReviewerDecisionOut,
    WallLayoutOut,
)
from reports.findings_pdf import FindingsPdfInput, write_findings_pdf
from reports.spreadsheet import StoredFinding, write_stored_workbook
from units.imperial import format_inches
from verdict.outcomes import Outcome


def _exact(value: Fraction) -> ExactValueOut:
    return ExactValueOut(
        numerator=str(value.numerator),
        denominator=str(value.denominator),
        display=f'{format_inches(value)}"',
    )


def _result(
    label: str,
    outcome: Outcome | None,
    *,
    printed: Fraction | None,
    pieces: tuple[Fraction | None, ...],
    expected: Fraction | None,
    delta: Fraction | None,
    hold: HoldOut | None = None,
    decision: ReviewerDecisionOut | None = None,
    wall: WallLayoutOut | None = None,
) -> CountertopResultOut:
    return CountertopResultOut(
        finding_id=UUID(int=1) if outcome is not None else None,
        row_id=UUID(int=2 + len(label)),
        page_number=2,
        label=label,
        row_location=None,
        outcome=outcome,
        reviewer_decision=decision,
        needs_decision=outcome is None or outcome in (Outcome.FAIL, Outcome.REVIEW_REQUIRED),
        printed_overall=None if printed is None else _exact(printed),
        pieces=tuple(
            CountertopPieceOut(
                index=index,
                value=None if piece is None else _exact(piece),
                source="missing" if piece is None else "sealed",
                kind="cabinet",
            )
            for index, piece in enumerate(pieces)
        ),
        field_cut_per_end=_exact(Fraction(1)),
        field_cut_count=0,
        expected_total=None if expected is None else _exact(expected),
        delta=None if delta is None else _exact(delta),
        wall_layout=wall
        or WallLayoutOut(config="back_only", label="back wall only", source="reviewer"),
        agreement=AgreementFactsOut(
            both_readers_agreed_on_row=True,
            values_agreed=(True, *(True for _ in pieces)),
            code_clue_used=False,
        ),
        hold=hold,
    )


def _stored() -> StoredFinding:
    return StoredFinding(
        rule_id="CT-WIDTH-001",
        outcome="PASS",
        severity="CRITICAL",
        snapshot_id="snapshot",
        engine_version="test",
        trace={},
    )


def _pdf_text(data: bytes) -> str:
    return " ".join(
        " ".join(page.extract_text() or "" for page in PdfReader(BytesIO(data)).pages).split()
    )


def test_pdf_lists_checked_held_and_reviewed_countertops_before_findings() -> None:
    at = datetime(2026, 10, 9, 12, tzinfo=UTC)
    results = (
        _result(
            "Countertop row A",
            Outcome.PASS,
            printed=Fraction(24),
            pieces=(Fraction(10), Fraction(14)),
            expected=Fraction(24),
            delta=Fraction(0),
        ),
        _result(
            "Countertop row B",
            Outcome.FAIL,
            printed=Fraction(25),
            pieces=(Fraction(10), Fraction(14)),
            expected=Fraction(24),
            delta=Fraction(1),
            wall=WallLayoutOut(
                config="back_left_right", label="back wall and both ends", source="drawing clues"
            ),
        ),
        _result(
            "Countertop row C",
            None,
            printed=None,
            pieces=(Fraction(10), None),
            expected=None,
            delta=None,
            hold=HoldOut(
                code="row-partial", reason="A piece is missing; reviewer input is needed."
            ),
            decision=ReviewerDecisionOut(
                action="dismiss",
                note="Not checkable from the supplied drawing.",
                actor="Reviewer Example",
                time=at,
            ),
        ),
        _result(
            "Countertop between panels",
            Outcome.PASS,
            printed=Fraction(37),
            pieces=(Fraction(7), Fraction(23), Fraction(7)),
            expected=Fraction(37),
            delta=Fraction(0),
            wall=WallLayoutOut(
                config="back_only",
                label="back wall only; no field cut at the ends",
                source="between panels",
            ),
        ),
    )
    source = FindingsPdfInput(
        package_revision_id=UUID(int=11),
        revision_number=1,
        vendor=None,
        findings=(_stored(),),
        countertop_results=results,
    )

    text = _pdf_text(write_findings_pdf(source))

    assert text.index("COUNTERTOPS") < text.index("CT-WIDTH-001")
    assert "Countertop row A" in text and "PASS" in text
    assert '24"' in text and 'Difference 0"' in text
    assert "Countertop row B" in text and "FAIL" in text and 'Difference 1"' in text
    assert "back wall and both ends" in text and "drawing clues" in text
    assert "Countertop row C" in text and "A piece is missing" in text
    assert "Not checkable from the supplied drawing." in text
    assert "Reviewer Example" in text and "2026-10-09" in text
    assert "Countertop between panels" in text
    assert "between panels" in text and "no field cut at the ends" in text


def test_pdf_strip_uses_vector_drawing_for_a_result() -> None:
    from reports.findings_pdf import _draw_countertop_strip

    result = _result(
        "Countertop row A",
        Outcome.PASS,
        printed=Fraction(24),
        pieces=(Fraction(10), Fraction(14)),
        expected=Fraction(24),
        delta=Fraction(0),
    )

    class Recorder:
        def __init__(self) -> None:
            self.rectangles = 0
            self.lines = 0

        def rect(self, *_args: object, **_kwargs: object) -> None:
            self.rectangles += 1

        def line(self, *_args: object, **_kwargs: object) -> None:
            self.lines += 1

        def __getattr__(self, _name: str) -> object:
            return lambda *_args, **_kwargs: None

    canvas = Recorder()
    _draw_countertop_strip(canvas, result, x=0, y=0, width=420)

    assert canvas.rectangles >= len(result.pieces)
    assert canvas.lines >= 4


def test_pdf_countertop_card_keeps_strip_below_all_wrapped_details() -> None:
    from reports.findings_pdf import _countertop_strip_y

    top = 700.0
    detail_lines = 9
    last_detail_y = top - 31 - (detail_lines - 1) * 9
    strip_y = _countertop_strip_y(top, detail_lines)

    assert strip_y + 48 < last_detail_y
    assert strip_y - 25 > strip_y - 34


def test_workbook_has_exact_countertop_text_and_numeric_columns() -> None:
    result = _result(
        "Countertop row B",
        Outcome.FAIL,
        printed=Fraction(25),
        pieces=(Fraction(10), Fraction(14)),
        expected=Fraction(24),
        delta=Fraction(1),
    )

    book = load_workbook(BytesIO(write_stored_workbook((_stored(),), countertop_results=(result,))))

    assert "Countertops" in book.sheetnames
    sheet = book["Countertops"]
    headers = [cell.value for cell in sheet[1]]
    row = {header: sheet.cell(2, index + 1).value for index, header in enumerate(headers)}
    assert row["label"] == "Countertop row B"
    assert row["outcome"] == "FAIL"
    assert row["printed_overall"] == '25"'
    assert row["printed_overall_in"] == 25
    assert row["piece_1_in"] == 10
    assert row["piece_2_in"] == 14
    assert row["expected_total"] == '24"'
    assert row["difference"] == '1"'
    assert row["field_cut_per_end"] == '1"'
    assert row["field_cut_per_end_in"] == 1
