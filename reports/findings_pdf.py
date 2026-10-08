"""Render the reviewer-facing findings handoff without rebuilding any verdict value.

This is an internal review report, not a redline and not a vendor instruction.  It is generated
after the deterministic engine has persisted its findings and shows those recorded facts in a
compact Graniti + Nexolv layout.  The renderer receives ``StoredFinding`` values because they are
the database-shaped facts the output stage already has.  In particular, it never reparses a
comparison string to recover a number: ARCH and SHOP values are copied directly from the recorded
operand objects, whose ``value`` fields are already exact display text.

The PDF is deliberately presentation-only.  It does not import the rule engine, calculate an
outcome, infer an operand role, or decide whether an item needs review.  The only grouping it does
is the explicitly labelled ``ARCH`` and ``SHOP`` sources; an absent source is reported as not
recorded rather than guessed from operand order.

Source: docs/DEMO_PLAN.md, ADR-0001.  Verification: tests/reports/test_findings_pdf.py.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from io import BytesIO
from typing import Final
from uuid import UUID

from reportlab.lib.pagesizes import A4  # type: ignore[import-untyped]
from reportlab.pdfbase.pdfmetrics import stringWidth  # type: ignore[import-untyped]
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]

from app.schemas.visual_ui import CountertopResultOut, ExactValueOut
from reports.signed_review import SignedReview, with_review_pdf
from reports.spreadsheet import NOT_RECORDED, StoredFinding
from verdict.outcomes import Outcome
from workflow.changed_values import ChangedValues

__all__ = ["FINDINGS_PDF_MEDIA_TYPE", "FindingsPdfInput", "write_findings_pdf"]

FINDINGS_PDF_MEDIA_TYPE: Final = "application/pdf"

_PAGE_WIDTH, _PAGE_HEIGHT = A4
_MARGIN: Final = 46.0
_CONTENT_WIDTH: Final = _PAGE_WIDTH - (_MARGIN * 2)
_BLACK: Final = 0.0
_WHITE: Final = 1.0
_BODY_FONT: Final = "Courier"
_BOLD_FONT: Final = "Courier-Bold"


@dataclass(frozen=True, slots=True)
class FindingsPdfInput:
    """The immutable stored facts one reviewer report presents.

    ``package_revision_id`` and ``revision_number`` identify what the report covers.  ``vendor`` is
    optional because a revision can exist before a vendor has been named; the cover says "not
    recorded" in that case rather than turning the absence into a made-up project label.
    """

    package_revision_id: UUID
    revision_number: int
    vendor: str | None
    findings: tuple[StoredFinding, ...]
    countertop_results: tuple[CountertopResultOut, ...] = ()
    changed_values: ChangedValues | None = None
    signed_review: SignedReview | None = None
    product_type: str | None = None
    """What the reviewer said the drawing set is for (#994), e.g. ``countertop``. When stated, only
    that product's checks were run and the cover says so, so a check that was not run can never be
    read as one that passed. ``None`` — a set from before #994 — draws nothing: every product's
    checks ran, and the cover is exactly what it was."""

    def __post_init__(self) -> None:
        if not isinstance(self.package_revision_id, UUID):
            raise TypeError("package_revision_id must be a UUID")
        if isinstance(self.revision_number, bool) or not isinstance(self.revision_number, int):
            raise TypeError("revision_number must be an integer")
        if self.revision_number < 1:
            raise ValueError("revision_number must be positive")
        if self.vendor is not None and (
            not isinstance(self.vendor, str) or not self.vendor.strip()
        ):
            raise ValueError("vendor must be a non-empty string when supplied")
        if self.product_type is not None and (
            not isinstance(self.product_type, str) or not self.product_type.strip()
        ):
            raise ValueError("product_type must be a non-empty string when supplied")
        if not isinstance(self.findings, tuple) or not self.findings:
            raise ValueError("findings must be a non-empty tuple")
        if not all(isinstance(finding, StoredFinding) for finding in self.findings):
            raise TypeError("findings must contain only StoredFinding values")
        if not isinstance(self.countertop_results, tuple) or not all(
            isinstance(result, CountertopResultOut) for result in self.countertop_results
        ):
            raise TypeError("countertop_results must contain CountertopResultOut values")


def _text(value: object, *, absent: str = NOT_RECORDED) -> str:
    """Return one stored value verbatim, marking absence without rebuilding it."""
    if value is None:
        return absent
    rendered = str(value)
    return rendered if rendered else absent


def _stored_operands(finding: StoredFinding) -> tuple[Mapping[str, object], ...]:
    """The recorded operands, preserving their stored order and values."""
    operands = finding.trace.get("operands")
    if not isinstance(operands, list):
        return ()
    return tuple(operand for operand in operands if isinstance(operand, Mapping))


def _source_values(finding: StoredFinding, source: str) -> str:
    """Exact values whose stored source explicitly names ``source``.

    There is no fallback to the first or second operand.  Operand order is rule-specific, while
    ARCH/SHOP is provenance a renderer may report directly.  Multiple values remain separate rather
    than being summed or otherwise reconstructed.
    """
    values = [
        _text(operand.get("value"))
        for operand in _stored_operands(finding)
        if operand.get("source") == source
    ]
    return "; ".join(values) if values else NOT_RECORDED


def _reason(finding: StoredFinding) -> str:
    """Use guarded reviewer prose when present, otherwise the stored deterministic reason."""
    if finding.reviewer_summary:
        return finding.reviewer_summary
    if finding.reason:
        return finding.reason
    return _text(finding.trace.get("reason"))


def _counts(findings: Sequence[StoredFinding]) -> tuple[int, int, int]:
    """Count deterministic outcomes as pass, fail, or reviewer attention without re-judging them."""
    passed = sum(finding.outcome == "PASS" for finding in findings)
    failed = sum(finding.outcome == "FAIL" for finding in findings)
    review = len(findings) - passed - failed
    return (passed, failed, review)


def _exact_fraction(value: ExactValueOut | None) -> Fraction | None:
    if value is None:
        return None
    try:
        return Fraction(int(value.numerator), int(value.denominator))
    except (ValueError, ZeroDivisionError):
        return None


def _draw_countertop_strip(
    canvas: Canvas, result: CountertopResultOut, *, x: float, y: float, width: float
) -> None:
    """Draw a proportional, presentation-only strip from exact API values."""
    left_wall = result.wall_layout.config == "back_left_right"
    right_wall = result.wall_layout.config == "back_left_right"
    wall_width = 14.0
    inner_x = x + (wall_width if left_wall else 0.0)
    inner_width = width - (wall_width if left_wall else 0.0) - (wall_width if right_wall else 0.0)
    inner_width = max(inner_width, 20.0)
    values = [_exact_fraction(piece.value) for piece in result.pieces]
    weights = [max(float(value), 1.0) if value is not None else 18.0 for value in values]
    total_weight = sum(weights) or 1.0
    canvas.setStrokeColorRGB(_BLACK, _BLACK, _BLACK)
    canvas.setFillColorRGB(_BLACK, _BLACK, _BLACK)
    if result.wall_layout.config in {"back_only", "back_left_right"}:
        canvas.setLineWidth(0.7)
        canvas.line(inner_x, y + 29, inner_x + inner_width, y + 29)
    if left_wall:
        canvas.rect(x, y + 7, wall_width - 2, 16, fill=0, stroke=1)
        for offset in (2, 6, 10):
            canvas.line(x + offset, y + 8, x + min(offset + 8, wall_width - 2), y + 22)
    if right_wall:
        right_x = x + width - wall_width + 2
        canvas.rect(right_x, y + 7, wall_width - 2, 16, fill=0, stroke=1)
        for offset in (2, 6, 10):
            canvas.line(right_x + offset, y + 8, right_x + min(offset + 8, wall_width - 2), y + 22)
    if result.field_cut_count and result.field_cut_per_end is not None:
        for end_x in (
            (inner_x,) if result.field_cut_count == 1 else (inner_x, inner_x + inner_width)
        ):
            canvas.line(end_x, y + 27, end_x, y + 34)
            canvas.setFont(_BODY_FONT, 6)
            canvas.drawCentredString(end_x, y + 25, f"+{result.field_cut_per_end.display}")
    cursor = inner_x
    piece_y = y + 7
    piece_height = 16.0
    for piece, value, weight in zip(result.pieces, values, weights, strict=True):
        piece_width = inner_width * weight / total_weight
        kind = (piece.kind or "").casefold()
        appliance = "appliance" in kind
        if appliance:
            canvas.setDash(3, 2)
        canvas.rect(cursor, piece_y, piece_width, piece_height, fill=0, stroke=1)
        canvas.setDash()
        if "filler" in kind:
            hatch_x = cursor + 2
            while hatch_x < cursor + piece_width - 1:
                canvas.line(
                    hatch_x,
                    piece_y + 1,
                    min(hatch_x + piece_height, cursor + piece_width - 1),
                    piece_y + piece_height - 1,
                )
                hatch_x += 5
        display = "?" if piece.value is None else piece.value.display
        canvas.setFont(_BODY_FONT, 6.5)
        canvas.drawCentredString(cursor + piece_width / 2, y - 4, display)
        cursor += piece_width
    if result.hold is not None or result.outcome is None:
        canvas.saveState()
        canvas.setDash(2, 2)
        hatch = inner_x
        while hatch < inner_x + inner_width:
            canvas.line(
                hatch, piece_y, min(hatch + 16, inner_x + inner_width), piece_y + piece_height
            )
            hatch += 8
        canvas.restoreState()
    if result.printed_overall is not None:
        canvas.line(inner_x, y + 39, inner_x, y + 34)
        canvas.line(inner_x, y + 36, inner_x + inner_width, y + 36)
        canvas.line(inner_x + inner_width, y + 39, inner_x + inner_width, y + 34)
        canvas.setFont(_BODY_FONT, 7)
        canvas.drawCentredString(
            inner_x + inner_width / 2, y + 41, f"Printed {result.printed_overall.display}"
        )
    if result.expected_total is not None:
        canvas.line(inner_x, y + 1, inner_x, y - 3)
        canvas.line(inner_x, y - 1, inner_x + inner_width, y - 1)
        canvas.line(inner_x + inner_width, y + 1, inner_x + inner_width, y - 3)
        canvas.setFont(_BODY_FONT, 7)
        canvas.drawCentredString(
            inner_x + inner_width / 2, y - 12, f"Needed {result.expected_total.display}"
        )
    if result.delta is not None:
        delta = _exact_fraction(result.delta)
        if delta == 0:
            canvas.setFont(_BOLD_FONT, 8)
            canvas.drawRightString(x + width, y - 25, 'Difference 0"')
            mark_x = x + width - 69
            canvas.line(mark_x, y - 22, mark_x + 2, y - 24)
            canvas.line(mark_x + 2, y - 24, mark_x + 6, y - 18)
        else:
            canvas.setFont(_BOLD_FONT, 8)
            difference = f"Difference {result.delta.display}"
            canvas.drawRightString(x + width, y - 25, difference)
            mark_x = x + width - stringWidth(difference, _BOLD_FONT, 8) - 9
            canvas.line(mark_x, y - 23, mark_x + 5, y - 18)
            canvas.line(mark_x, y - 18, mark_x + 5, y - 23)


def _countertop_heading(result: CountertopResultOut) -> str:
    return f"PAGE {result.page_number} — {result.label}"


def _countertop_strip_y(top: float, detail_line_count: int) -> float:
    """Place the strip below every wrapped detail line with a small visual gap."""
    if detail_line_count < 1:
        raise ValueError("a countertop card needs at least one detail line")
    last_detail_y = top - 31 - (detail_line_count - 1) * 9
    # Strip labels extend 48 points above and 25 below its baseline.
    return last_detail_y - 56


def _wrapped(value: str, *, width: float, font: str, size: float) -> tuple[str, ...]:
    """Wrap display text for a PDF line without changing the text it represents."""

    def split_word(word: str) -> tuple[str, ...]:
        """Break only an overlong unspaced token; concatenating the pieces restores it exactly."""
        pieces: list[str] = []
        piece = ""
        for character in word:
            candidate = f"{piece}{character}"
            if piece and stringWidth(candidate, font, size) > width:
                pieces.append(piece)
                piece = character
            else:
                piece = candidate
        if piece:
            pieces.append(piece)
        return tuple(pieces or [""])

    lines: list[str] = []
    for paragraph in value.splitlines() or [""]:
        words = paragraph.split(" ")
        line = ""
        for word in words:
            candidate = word if not line else f"{line} {word}"
            if stringWidth(candidate, font, size) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            pieces = split_word(word)
            lines.extend(pieces[:-1])
            line = pieces[-1]
        lines.append(line)
    return tuple(lines or [""])


class _Document:
    """A small deterministic layout helper for the cover and paginated finding cards."""

    def __init__(self, source: FindingsPdfInput) -> None:
        self.source = source
        self._buffer = BytesIO()
        self.canvas = Canvas(
            self._buffer,
            pagesize=A4,
            pageCompression=1,
            invariant=1,
        )
        self.canvas.setTitle("Graniti + Nexolv review findings")
        self.canvas.setAuthor("Graniti + Nexolv")
        self.canvas.setSubject("Reviewer handoff artifact")
        self.page_number = 0
        self._findings_started = False
        self.y = _PAGE_HEIGHT - _MARGIN

    def _footer(self) -> None:
        self.canvas.setStrokeColorRGB(_BLACK, _BLACK, _BLACK)
        self.canvas.line(_MARGIN, 32, _PAGE_WIDTH - _MARGIN, 32)
        self.canvas.setFont(_BODY_FONT, 7)
        self.canvas.drawString(_MARGIN, 20, "GRANITI + NEXOLV - REVIEW FINDINGS")
        self.canvas.drawRightString(_PAGE_WIDTH - _MARGIN, 20, f"PAGE {self.page_number}")

    def _new_findings_page(self) -> None:
        if self._findings_started:
            self._footer()
            self.canvas.showPage()
        self.page_number += 1
        self._findings_started = True
        self.y = _PAGE_HEIGHT - _MARGIN
        self.canvas.setFillColorRGB(_BLACK, _BLACK, _BLACK)
        self.canvas.rect(_MARGIN, self.y - 28, _CONTENT_WIDTH, 28, fill=1, stroke=0)
        self.canvas.setFillColorRGB(_WHITE, _WHITE, _WHITE)
        self.canvas.setFont(_BOLD_FONT, 11)
        self.canvas.drawString(_MARGIN + 12, self.y - 18, "FINDINGS")
        self.canvas.setFont(_BODY_FONT, 7)
        self.canvas.drawRightString(
            _PAGE_WIDTH - _MARGIN - 12,
            self.y - 18,
            f"REVISION {self.source.revision_number}",
        )
        self.canvas.setFillColorRGB(_BLACK, _BLACK, _BLACK)
        self.y -= 48

    def _need(self, height: float) -> None:
        if self.y - height < 48:
            self._new_findings_page()

    def _field(self, label: str, value: str) -> None:
        self.canvas.setFont(_BOLD_FONT, 7.5)
        self.canvas.setFillColorRGB(_BLACK, _BLACK, _BLACK)
        self.canvas.drawString(_MARGIN + 12, self.y, label.upper())
        self.y -= 11
        self.canvas.setFont(_BODY_FONT, 8.5)
        for line in _wrapped(value, width=_CONTENT_WIDTH - 24, font=_BODY_FONT, size=8.5):
            self._need(13)
            self.canvas.drawString(_MARGIN + 12, self.y, line)
            self.y -= 12
        self.y -= 4

    def _finding(self, number: int, finding: StoredFinding) -> None:
        self._need(116)
        self.canvas.setStrokeColorRGB(_BLACK, _BLACK, _BLACK)
        self.canvas.line(_MARGIN, self.y + 4, _PAGE_WIDTH - _MARGIN, self.y + 4)
        self.y -= 12
        self.canvas.setFont(_BOLD_FONT, 10)
        self.canvas.drawString(_MARGIN + 12, self.y, f"{number:02d}  {finding.rule_id}")
        self.canvas.setFont(_BODY_FONT, 8)
        self.canvas.drawRightString(
            _PAGE_WIDTH - _MARGIN - 12,
            self.y,
            f"{finding.outcome} - {finding.severity}",
        )
        self.y -= 18
        self._field("Subject", finding.scope_label or "Package revision")
        layout = next(
            (note for note in (finding.notes or ()) if note.startswith("Wall layout:")),
            None,
        )
        if layout is not None:
            self._field("Wall layout", layout)
        self._field("Approved value (ARCH)", _source_values(finding, "ARCH"))
        self._field("Vendor value (SHOP)", _source_values(finding, "SHOP"))
        self._field("Recorded comparison", _text(finding.trace.get("comparison")))
        self._field("Reason", _reason(finding))
        self.y -= 8

    def cover(self) -> None:
        self.page_number = 1
        passed, failed, review = _counts(self.source.findings)
        self.canvas.setFillColorRGB(_BLACK, _BLACK, _BLACK)
        self.canvas.rect(0, _PAGE_HEIGHT - 260, _PAGE_WIDTH, 260, fill=1, stroke=0)
        self.canvas.setFillColorRGB(_WHITE, _WHITE, _WHITE)
        self.canvas.setFont(_BOLD_FONT, 18)
        self.canvas.drawString(_MARGIN, _PAGE_HEIGHT - 90, "GRANITI + NEXOLV")
        self.canvas.setFont(_BODY_FONT, 11)
        self.canvas.drawString(_MARGIN, _PAGE_HEIGHT - 116, "SHOP DRAWING REVIEW")
        self.canvas.drawString(
            _MARGIN,
            _PAGE_HEIGHT - 138,
            "SIGNED REVIEW" if self.source.signed_review else "BEFORE REVIEW — NOT A FINAL REPORT",
        )
        self.canvas.setFont(_BOLD_FONT, 27)
        self.canvas.drawString(_MARGIN, _PAGE_HEIGHT - 180, "FINDINGS REPORT")

        self.canvas.setFillColorRGB(_BLACK, _BLACK, _BLACK)
        self.canvas.setFont(_BODY_FONT, 9)
        self.canvas.drawString(_MARGIN, _PAGE_HEIGHT - 304, "REVIEW PACKAGE")
        self.canvas.setFont(_BOLD_FONT, 10)
        self.canvas.drawString(
            _MARGIN, _PAGE_HEIGHT - 324, f"REVISION {self.source.revision_number}"
        )
        self.canvas.setFont(_BODY_FONT, 8)
        self.canvas.drawString(
            _MARGIN, _PAGE_HEIGHT - 344, f"PACKAGE {self.source.package_revision_id}"
        )
        self.canvas.drawString(
            _MARGIN,
            _PAGE_HEIGHT - 362,
            f"VENDOR {_text(self.source.vendor)}",
        )
        if self.source.product_type is not None:
            product = self.source.product_type.strip().upper()
            self.canvas.drawString(
                _MARGIN,
                _PAGE_HEIGHT - 380,
                f"DRAWING SET FOR {product} — ONLY {product} CHECKS WERE RUN",
            )

        cards = (("PASS", passed), ("FAIL", failed), ("REVIEW", review))
        card_width = (_CONTENT_WIDTH - 20) / 3
        card_y = _PAGE_HEIGHT - 462
        for index, (label, count) in enumerate(cards):
            x = _MARGIN + index * (card_width + 10)
            self.canvas.rect(x, card_y, card_width, 72, fill=0, stroke=1)
            self.canvas.setFont(_BOLD_FONT, 22)
            self.canvas.drawCentredString(x + card_width / 2, card_y + 34, str(count))
            self.canvas.setFont(_BODY_FONT, 8)
            self.canvas.drawCentredString(x + card_width / 2, card_y + 15, label)

        self.canvas.setFont(_BODY_FONT, 8)
        for index, line in enumerate(
            _wrapped(
                "This reviewer handoff reports deterministic findings. Values are copied from "
                "stored operands; the report does not calculate or reconstruct measurements.",
                width=_CONTENT_WIDTH,
                font=_BODY_FONT,
                size=8,
            )
        ):
            self.canvas.drawString(_MARGIN, _PAGE_HEIGHT - 570 - (index * 12), line)
        self._footer()
        self.canvas.showPage()

    def changed_values(self) -> None:
        summary = self.source.changed_values
        if summary is None:
            return
        self.page_number += 1
        self.y = _PAGE_HEIGHT - _MARGIN

        def new_page() -> None:
            self._footer()
            self.canvas.showPage()
            self.page_number += 1
            self.y = _PAGE_HEIGHT - _MARGIN

        def line(value: str, *, heading: bool = False) -> None:
            self.canvas.setFont(_BOLD_FONT if heading else _BODY_FONT, 10 if heading else 8)
            for part in _wrapped(
                value,
                width=_CONTENT_WIDTH,
                font=_BOLD_FONT if heading else _BODY_FONT,
                size=10 if heading else 8,
            ):
                if self.y < 60:
                    new_page()
                self.canvas.drawString(_MARGIN, self.y, part)
                self.y -= 15 if heading else 12
            self.y -= 6

        line("PROJECT VALUES THAT DIFFER FROM GV STANDARDS", heading=True)
        if summary.message is not None:
            line(summary.message)
        else:
            if summary.company_standards_displaced:
                for value in summary.company_standards_displaced:
                    line(value)
            else:
                line("None")
            line("REQUIRED VALUES NOT SET", heading=True)
            if summary.outstanding:
                for value in summary.outstanding:
                    line(value)
            else:
                line("None")
        self._footer()
        self.canvas.showPage()

    def _countertops(self) -> None:
        results = self.source.countertop_results
        if not results:
            return

        def new_page(continued: bool = False) -> None:
            if self.page_number:
                self._footer()
                self.canvas.showPage()
            self.page_number += 1
            self.y = _PAGE_HEIGHT - _MARGIN
            self.canvas.setFillColorRGB(_BLACK, _BLACK, _BLACK)
            self.canvas.rect(_MARGIN, self.y - 28, _CONTENT_WIDTH, 28, fill=1, stroke=0)
            self.canvas.setFillColorRGB(_WHITE, _WHITE, _WHITE)
            self.canvas.setFont(_BOLD_FONT, 11)
            title = "COUNTERTOPS — CONTINUED" if continued else "COUNTERTOPS"
            self.canvas.drawString(_MARGIN + 12, self.y - 18, title)
            self.canvas.setFillColorRGB(_BLACK, _BLACK, _BLACK)
            self.y -= 48

        new_page()
        for result in sorted(results, key=lambda item: item.page_number):
            details = [
                f"Wall layout: {_text(result.wall_layout.label)} ({result.wall_layout.source})",
                f"Field cut: {_text(None if result.field_cut_per_end is None else result.field_cut_per_end.display)} per end x {_text(result.field_cut_count)}",
            ]
            if result.reviewer_decision is None:
                details.append(
                    "Decision: automatic recorded result"
                    if result.outcome is not None
                    else "Decision: no reviewer action recorded"
                )
            else:
                decision = result.reviewer_decision
                details.append(
                    f"Decision: {decision.action} by {decision.actor} on {decision.time.isoformat()}"
                )
                if decision.note:
                    details.append(f"Reviewer note: {decision.note}")
            if result.hold is not None:
                details.append(f"Hold: {result.hold.reason}")
            detail_lines = tuple(
                line
                for detail in details
                for line in _wrapped(detail, width=_CONTENT_WIDTH - 16, font=_BODY_FONT, size=7)
            )
            strip_y = _countertop_strip_y(self.y, len(detail_lines))
            card_bottom = strip_y - 34
            card_height = self.y - card_bottom
            if self.y - card_height < 48:
                new_page(continued=True)
                strip_y = _countertop_strip_y(self.y, len(detail_lines))
                card_bottom = strip_y - 34
            top = self.y
            self.canvas.setStrokeColorRGB(_BLACK, _BLACK, _BLACK)
            self.canvas.rect(
                _MARGIN, card_bottom, _CONTENT_WIDTH, top - card_bottom, fill=0, stroke=1
            )
            self.canvas.setFillColorRGB(_BLACK, _BLACK, _BLACK)
            self.canvas.setFont(_BOLD_FONT, 9)
            self.canvas.drawString(_MARGIN + 8, top - 17, _countertop_heading(result))
            status_text = (
                result.outcome.value if result.outcome is not None else "REVIEW — no recorded check"
            )
            self.canvas.setFont(_BODY_FONT, 8)
            self.canvas.drawRightString(_PAGE_WIDTH - _MARGIN - 8, top - 17, status_text)
            glyph_x = _PAGE_WIDTH - _MARGIN - 13
            glyph_y = top - 14
            self.canvas.setLineWidth(1.3)
            if result.outcome is Outcome.PASS:
                self.canvas.line(glyph_x - 3, glyph_y, glyph_x - 1, glyph_y - 2)
                self.canvas.line(glyph_x - 1, glyph_y - 2, glyph_x + 3, glyph_y + 3)
            elif result.outcome is Outcome.FAIL:
                self.canvas.line(glyph_x - 2, glyph_y - 2, glyph_x + 2, glyph_y + 2)
                self.canvas.line(glyph_x - 2, glyph_y + 2, glyph_x + 2, glyph_y - 2)
            elif result.outcome is Outcome.REVIEW_REQUIRED:
                self.canvas.line(glyph_x, glyph_y + 4, glyph_x - 4, glyph_y - 3)
                self.canvas.line(glyph_x - 4, glyph_y - 3, glyph_x + 4, glyph_y - 3)
                self.canvas.line(glyph_x + 4, glyph_y - 3, glyph_x, glyph_y + 4)
                self.canvas.setFont(_BOLD_FONT, 6)
                self.canvas.drawCentredString(glyph_x, glyph_y - 1, "!")
            elif result.outcome is Outcome.NOT_FOUND:
                self.canvas.setDash(2, 1)
                self.canvas.rect(glyph_x - 4, glyph_y - 4, 8, 8, fill=0, stroke=1)
                self.canvas.setDash()
                self.canvas.setFont(_BOLD_FONT, 6)
                self.canvas.drawCentredString(glyph_x, glyph_y - 2, "?")
            elif result.outcome is Outcome.NO_APPLICABLE_RULE:
                self.canvas.setDash(2, 1)
                self.canvas.line(glyph_x, glyph_y + 4, glyph_x + 4, glyph_y)
                self.canvas.line(glyph_x + 4, glyph_y, glyph_x, glyph_y - 4)
                self.canvas.line(glyph_x, glyph_y - 4, glyph_x - 4, glyph_y)
                self.canvas.line(glyph_x - 4, glyph_y, glyph_x, glyph_y + 4)
                self.canvas.setDash()
            self.canvas.setFont(_BODY_FONT, 7)
            for index, line in enumerate(detail_lines):
                self.canvas.drawString(_MARGIN + 8, top - 31 - (index * 9), line)
            _draw_countertop_strip(
                self.canvas,
                result,
                x=_MARGIN + 8,
                y=strip_y,
                width=_CONTENT_WIDTH - 16,
            )
            self.y = card_bottom - 12

    def build(self) -> bytes:
        self.cover()
        self.changed_values()
        self._countertops()
        self._new_findings_page()
        for index, finding in enumerate(self.source.findings, start=1):
            self._finding(index, finding)
        self._footer()
        self.canvas.save()
        return self._buffer.getvalue()


def write_findings_pdf(source: FindingsPdfInput) -> bytes:
    """Return deterministic branded PDF bytes for the supplied stored findings.

    ReportLab's ``invariant=1`` removes volatile creation metadata so unchanged stored facts produce
    unchanged bytes and therefore one content-addressed output artifact.  No value in the PDF is
    parsed as a measurement or sent to the deterministic engine.
    """
    if not isinstance(source, FindingsPdfInput):
        raise TypeError("source must be a FindingsPdfInput")
    document = _Document(source).build()
    return (
        with_review_pdf(document, source.signed_review)
        if source.signed_review is not None
        else document
    )
