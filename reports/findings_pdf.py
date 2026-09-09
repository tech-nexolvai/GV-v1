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
from io import BytesIO
from typing import Final
from uuid import UUID

from reportlab.lib.pagesizes import A4  # type: ignore[import-untyped]
from reportlab.pdfbase.pdfmetrics import stringWidth  # type: ignore[import-untyped]
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]

from reports.spreadsheet import NOT_RECORDED, StoredFinding

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
        if not isinstance(self.findings, tuple) or not self.findings:
            raise ValueError("findings must be a non-empty tuple")
        if not all(isinstance(finding, StoredFinding) for finding in self.findings):
            raise TypeError("findings must contain only StoredFinding values")


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

    def build(self) -> bytes:
        self.cover()
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
    return _Document(source).build()
