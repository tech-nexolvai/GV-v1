"""Worker-only rendering of the stored signed review.

**The cover of a format-3 snapshot (QA 2026-10-16)** (`ReviewCover`) adds what the draft's front
page already says: "Will be clouded as wrong" (every FAIL, and every result a person checked by
hand and found wrong, by the signed record's own mark), and "Compared with the architect" with
every difference. A rule left out (the old compare while it is switched off) is said once, never
dropped silently. With no `ReviewCover` (an older snapshot) the cover is drawn exactly as before.

**Format 4 (Set 3 QA 2026-10-16)** adds `not_checked`: the results whose check could not run and
that nobody answered are said once per reason ("NOT CHECKED (174): grouped by reason"), as the
draft's front page says them, instead of one heading, "no per-finding action recorded" and a raw
id each (28 pages). A format-3 cover passes none and is drawn exactly as before.
"""

from dataclasses import dataclass
from uuid import UUID

from app.review.signed_record import ReviewDisposition, SignedReview, SignedReviewFinding

__all__ = [
    "ReviewCover",
    "ReviewDisposition",
    "SignedReview",
    "SignedReviewFinding",
    "clouded_wrong",
    "review_pdf",
    "with_review_pdf",
]


@dataclass(frozen=True, slots=True)
class ReviewCover:
    """What a format-3 signed cover adds, every line from the frozen snapshot."""

    compare_title: str | None = None
    """E.g. "Compared with the architect (AI read) (2 different)"; `None` when nothing was
    compared."""
    compare_lines: tuple[str, ...] = ()
    """The counts line, the note, then one line per difference ("A1 ... — Page 2")."""
    left_out_rules: frozenset[str] = frozenset()
    """Rules whose results are not printed here (the old compare while it is switched off)."""
    left_out_note: str | None = None
    """Said once when a result was left out, so nothing vanishes silently."""
    not_checked: tuple[tuple[str, tuple[str, ...]], ...] = ()
    """Format 4: each reason once, with the labels of the not-checked results it covers."""
    not_checked_ids: frozenset[UUID] = frozenset()
    """Format 4: the results said in `not_checked`, so they are not listed one by one again."""
    left_for_person: tuple[tuple[str, tuple[str, ...]], ...] = ()
    """Format 5 (owner decision 2026-10-16): the questions nobody answered at sign-off, each kind
    once with the words of every question ("Not confirmed by a person — ... (AI: ...)")."""


def clouded_wrong(review: SignedReview) -> tuple[SignedReviewFinding, ...]:
    """Every FAIL, and every result a person checked by hand and found wrong (#1358), by the
    signed record's own mark (`ReviewDisposition.checked_wrong_by_hand`)."""
    return tuple(
        finding
        for finding in review.findings
        if finding.outcome == "FAIL"
        or (finding.actions and finding.actions[-1].checked_wrong_by_hand)
    )


def review_pdf(review: SignedReview, cover: ReviewCover | None = None) -> bytes:
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
    shown = review.findings
    if cover is not None:
        shown = tuple(f for f in review.findings if f.rule_id not in cover.left_out_rules)
        wrong = [f for f in clouded_wrong(review) if f.rule_id not in cover.left_out_rules]
        story.append(Paragraph(f"Will be clouded as wrong ({len(wrong)})", styles["Heading2"]))
        if not wrong:
            story.append(Paragraph("None. No result is wrong.", styles["Normal"]))
        for finding in wrong:
            label = finding.rule_id + (f" - {finding.scope_label}" if finding.scope_label else "")
            story.append(Paragraph(escape(f"{label}: {finding.wording}"), styles["Normal"]))
        story.append(Spacer(1, 12))
        if cover.compare_title is not None:
            story.append(Paragraph(escape(cover.compare_title), styles["Heading2"]))
            for text in cover.compare_lines:
                story.append(Paragraph(escape(text), styles["Normal"]))
            story.append(Spacer(1, 12))
        if cover.left_out_note is not None and len(shown) != len(review.findings):
            story.append(Paragraph(escape(cover.left_out_note), styles["Normal"]))
            story.append(Spacer(1, 12))
        if cover.not_checked:
            # Format 4: said once per reason, never one heading and raw id per result.
            total = sum(len(labels) for _reason, labels in cover.not_checked)
            story.append(Paragraph(f"NOT CHECKED ({total}): grouped by reason", styles["Heading2"]))
            for reason, labels in cover.not_checked:
                story.append(
                    Paragraph(escape(f"{len(labels)} check(s): {reason}"), styles["Normal"])
                )
                story.append(Paragraph(escape("; ".join(labels)), styles["Normal"]))
            story.append(Spacer(1, 12))
            shown = tuple(f for f in shown if f.finding_id not in cover.not_checked_ids)
        if cover.left_for_person:
            # Format 5: every question is optional, so what nobody answered is printed, never
            # dropped: grouped by kind, each with the AI's best guess or its reason.
            total = sum(len(lines) for _kind, lines in cover.left_for_person)
            story.append(Paragraph(f"Left for a person ({total})", styles["Heading2"]))
            story.append(
                Paragraph(
                    "Signed with these questions unanswered. Each is printed as not confirmed "
                    "by a person; a FAIL stays clouded as wrong.",
                    styles["Normal"],
                )
            )
            for kind, lines in cover.left_for_person:
                story.append(Paragraph(escape(f"{kind} ({len(lines)})"), styles["Heading3"]))
                for text in lines:
                    story.append(Paragraph(escape(text), styles["Normal"]))
            story.append(Spacer(1, 12))
    for finding in shown:
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


def with_review_pdf(
    document: bytes, review: SignedReview, cover: ReviewCover | None = None
) -> bytes:
    """Prepend the signed record without changing the deterministic pages or drawing."""
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(review_pdf(review, cover))))
    writer.append(PdfReader(BytesIO(document)))
    output = BytesIO()
    writer.write(output)
    return output.getvalue()
