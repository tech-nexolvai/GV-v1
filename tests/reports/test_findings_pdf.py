"""The branded reviewer PDF is presentation over stored facts, never a second calculator."""

from __future__ import annotations

from fractions import Fraction
from io import BytesIO
from uuid import UUID

from pypdf import PdfReader

from reports.findings_pdf import FINDINGS_PDF_MEDIA_TYPE, FindingsPdfInput, write_findings_pdf
from reports.spreadsheet import StoredFinding


def _finding(**overrides: object) -> StoredFinding:
    defaults: dict[str, object] = {
        "rule_id": "CT-DEPTH-001",
        "outcome": "FAIL",
        "severity": "FLAG",
        "snapshot_id": "sha256:rule-snapshot",
        "engine_version": "verdict-1.2.3",
        "trace": {
            # This deliberately does not agree with either operand. A PDF that rebuilt values from
            # display prose would expose a different number than the structured records below.
            "comparison": "display text is not a value parser: 99 in",
            "operands": [
                {"name": "approved_depth", "value": "25 in", "source": "ARCH"},
                {"name": "vendor_depth", "value": "25 1/2 in", "source": "SHOP"},
            ],
        },
        "reason": "The deterministic comparison differs by 1/2 in.",
        "delta": "1/2 in",
        "variant": None,
        "notes": (),
        "reviewer_summary": "CT-DEPTH-001: FAIL. The recorded values require reviewer attention.",
    }
    defaults.update(overrides)
    return StoredFinding(**defaults)  # type: ignore[arg-type]


def _source(*findings: StoredFinding) -> FindingsPdfInput:
    return FindingsPdfInput(
        package_revision_id=UUID("11111111-1111-1111-1111-111111111111"),
        revision_number=1,
        vendor="Apex Glass & Stone",
        findings=tuple(findings),
    )


def test_the_pdf_is_a_branded_reviewer_handoff_with_exact_recorded_values() -> None:
    document = write_findings_pdf(_source(_finding()))

    assert document.startswith(b"%PDF-")
    reader = PdfReader(BytesIO(document))
    assert reader.metadata is not None
    assert reader.metadata.title == "Graniti + Nexolv review findings"
    assert reader.metadata.author == "Graniti + Nexolv"
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "GRANITI + NEXOLV" in text
    assert "FINDINGS REPORT" in text
    assert "PASS" in text and "FAIL" in text and "REVIEW" in text
    assert "APPROVED VALUE (ARCH)" in text
    assert "25 in" in text
    assert "VENDOR VALUE (SHOP)" in text
    assert "25 1/2 in" in text
    assert "display text is not a value parser: 99 in" in text
    assert "CT-DEPTH-001: FAIL." in text


def test_unchanged_stored_facts_produce_identical_content_addressable_pdf_bytes() -> None:
    source = _source(_finding())

    assert write_findings_pdf(source) == write_findings_pdf(source)
    assert FINDINGS_PDF_MEDIA_TYPE == "application/pdf"


def test_an_absent_arch_or_shop_source_is_reported_not_inferred_from_order() -> None:
    finding = _finding(
        trace={
            "comparison": "25 1/2 in vs 25 in",
            "operands": [
                {"name": "vendor_depth", "value": str(Fraction(51, 2)), "source": "USER_INPUT"}
            ],
        }
    )

    document = write_findings_pdf(_source(finding))
    text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(document)).pages)

    assert text.count("not recorded in the database") >= 2
    assert "51/2" not in text
