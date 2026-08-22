"""Issue #226: silence in a redline must never be readable as approval.

The fixtures contain no client material.  They are deliberately plain pages and polygons
because these tests verify report accounting and wording, not drawing recognition.
"""

from __future__ import annotations

import json
from decimal import Decimal
from fractions import Fraction
from io import BytesIO
from pathlib import Path
from uuid import UUID

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]

from evidence.coordinates import PageTransform
from reports.redline import OUTCOME_STYLES, RedlinePackage, RedlinePage, ReportMode, render_redline
from storage.local import LocalStore
from verdict.finding import Finding
from verdict.outcomes import Outcome, Severity
from verdict.trace import CalculationTrace, TracedOperand

DOCUMENT = UUID("33333333-3333-3333-3333-333333333333")
REVISION = UUID("44444444-4444-4444-4444-444444444444")
WIDTH = Decimal(400)
HEIGHT = Decimal(200)
BOX = (Decimal(0), Decimal(0), WIDTH, HEIGHT)


def source_pdf(pages: int = 2) -> bytes:
    """Return searchable synthetic pages so output text can be asserted directly."""

    writer = PdfWriter()
    for index in range(pages):
        buffer = BytesIO()
        canvas = Canvas(buffer, pagesize=(float(WIDTH), float(HEIGHT)), invariant=1)
        canvas.drawString(20, 100, f"SYNTHETIC PAGE {index + 1}")
        canvas.showPage()
        canvas.save()
        buffer.seek(0)
        writer.add_page(PdfReader(buffer).pages[0])
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def package() -> RedlinePackage:
    return RedlinePackage(
        package_revision_id=REVISION,
        source_pdf=source_pdf(),
        pages=tuple(
            RedlinePage(
                document_version_id=DOCUMENT,
                page=index,
                source_index=index,
                transform=PageTransform(72, 0, BOX, BOX),
            )
            for index in range(2)
        ),
    )


def reference(page: int = 0) -> str:
    return json.dumps(
        {
            "document_version_id": str(DOCUMENT),
            "page": page,
            "polygon": [["0.1", "0.2"], ["0.5", "0.2"], ["0.5", "0.4"]],
            "space": "stored",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def trace(outcome: Outcome) -> CalculationTrace:
    return CalculationTrace(
        operation="equals",
        operands=(TracedOperand("actual", Fraction(1), "SHOP", reference()),),
        intermediates=(),
        comparison="1 == 1",
        tolerance=None,
        arithmetic_unit=None,
        outcome=outcome,
        engine_version="test",
        operation_version="1",
    )


def finding(outcome: Outcome) -> Finding:
    rule_id = f"SYNTH-{outcome.value}"
    return Finding(
        rule_id=rule_id,
        outcome=outcome,
        severity=Severity.CRITICAL,
        reason=f"synthetic reason for {outcome.value}",
        snapshot_id="snapshot-226-test",
        engine_version="test",
        trace=trace(outcome) if outcome in (Outcome.PASS, Outcome.FAIL) else None,
        evidence_refs=(reference(),),
    )


def render_text(tmp_path: Path, findings: list[Finding]) -> str:
    store = LocalStore(tmp_path)
    artifact = render_redline(package(), findings, ReportMode.INTERNAL, store)
    with store.get(artifact.key) as handle:
        text = " ".join(page.extract_text() or "" for page in PdfReader(handle).pages)
    return " ".join(text.split())


def test_one_of_every_outcome_is_visible_and_visually_distinct(tmp_path: Path) -> None:
    """Input: all outcomes. Outcome: every label/reason is present and every style differs."""

    findings = [finding(outcome) for outcome in Outcome]

    text = render_text(tmp_path, findings)

    assert set(OUTCOME_STYLES) == set(Outcome)
    assert len({style.stroke for style in OUTCOME_STYLES.values()}) == len(Outcome)
    for item in findings:
        assert item.rule_id in text
        assert item.outcome.value in text


def test_all_abstentions_are_summarised_even_when_they_have_geometry(tmp_path: Path) -> None:
    """Input: three drawable abstentions. Outcome: summary names each reason, never silence."""

    abstentions = [
        finding(Outcome.NOT_FOUND),
        finding(Outcome.REVIEW_REQUIRED),
        finding(Outcome.NO_APPLICABLE_RULE),
    ]

    text = render_text(tmp_path, abstentions)

    assert "What was not checked or could not be decided" in text
    assert "must not be read as approval" in text
    for item in abstentions:
        assert item.reason in text


def test_a_page_with_no_findings_is_explicitly_not_approved(tmp_path: Path) -> None:
    """Input: finding only on page 1. Outcome: page 2 says no finding is not approval."""

    text = render_text(tmp_path, [finding(Outcome.FAIL)])

    assert "Page 1: one or more findings are marked on this page" in text
    assert "Page 2: NO FINDINGS WERE PRODUCED OR PLACED ON THIS PAGE" in text
    assert "This is not an approval" in text


def test_no_abstentions_is_stated_rather_than_an_absent_section(tmp_path: Path) -> None:
    """Input: decisive results only. Outcome: report explicitly says no checks abstained."""

    text = render_text(tmp_path, [finding(Outcome.PASS), finding(Outcome.FAIL)])

    assert "None. Every finding in this report reached PASS or FAIL" in text
