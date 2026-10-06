"""Worker-only rendering of the stored signed review."""

from app.review.signed_record import ReviewDisposition, SignedReview, SignedReviewFinding

__all__ = [
    "ReviewDisposition",
    "SignedReview",
    "SignedReviewFinding",
    "review_pdf",
    "with_review_pdf",
]


def review_pdf(review: SignedReview) -> bytes:
    """A readable publication record, generated solely from stored fields."""
    from io import BytesIO
    from xml.sax.saxutils import escape

    from reportlab.lib.styles import getSampleStyleSheet  # type: ignore[import-untyped]
    from reportlab.platypus import (  # type: ignore[import-untyped]
        Paragraph,
        SimpleDocTemplate,
        Spacer,
    )

    output = BytesIO()
    styles = getSampleStyleSheet()
    story = [
        Paragraph("GRANITI + NEXOLV", styles["Title"]),
        Paragraph("Signed review", styles["Heading1"]),
        Paragraph(escape(review.signoff), styles["Normal"]),
        Spacer(1, 16),
    ]
    for finding in review.findings:
        story.append(
            Paragraph(
                escape(
                    finding.rule_id + (f" - {finding.scope_label}" if finding.scope_label else "")
                ),
                styles["Heading2"],
            )
        )
        story.append(Paragraph(escape(finding.wording), styles["Normal"]))
        story.append(
            Paragraph(
                escape(
                    f"Recorded check outcome (unchanged): {finding.outcome}. Finding id: {finding.finding_id}"
                ),
                styles["Normal"],
            )
        )
        if len(finding.actions) > 1:
            story.append(Paragraph("Earlier recorded actions", styles["Heading3"]))
            for action in finding.actions[:-1]:
                story.append(Paragraph(escape(action.wording(finding.outcome)), styles["Normal"]))
        story.append(Spacer(1, 12))
    SimpleDocTemplate(output, title="Signed review", author="Graniti + Nexolv", invariant=1).build(
        story
    )
    return output.getvalue()


def with_review_pdf(document: bytes, review: SignedReview) -> bytes:
    """Prepend the signed record without changing the deterministic pages or drawing."""
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(review_pdf(review))))
    writer.append(PdfReader(BytesIO(document)))
    output = BytesIO()
    writer.write(output)
    return output.getvalue()
