"""The vendor comment letter (#1622), drawn by code from `send_package.Letter`.

A transmittal-style letter, plain and polite, the same every time (research note
INDUSTRY-REVIEW.md, section 4):

1. **The heading**: "Submittal review comments", then the submittal's identity: project, vendor,
   submittal number with its resubmittal letter (25, 25A), spec section (blank when unknown),
   drawing set, round, products, dates received and reviewed, and the reviewer line "Checked
   automatically by GV Review" ("…, and reviewed by <name>" once a reviewer decided anything).
2. **The review result** (`review_action`) in a box: the stamp line, why, who must act, and the
   standard disclaimer (`review_action.DISCLAIMER`, one constant).
3. On a round 2 or later, **previous comments**: each one Closed or Previous comment not addressed.
4. **Comments**: one entry per cloud on the GV cloud drawing, with the same comment number (C-07):
   page, item, its action (Correct as noted / Submit specified item), "Vendor shows X", "Architect
   requires Y" (or GV rule, or the vendor's own parts) and the source; "New" on a later round.
5. **Not compared**: how many items, and where they are listed.
6. The next step for the vendor, and the enclosures.

US Letter, portrait. Loaded by name, never imported from `app/api/` (reportlab).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final
from xml.sax.saxutils import escape

from reportlab.lib.colors import Color  # type: ignore[import-untyped]
from reportlab.lib.pagesizes import letter as letter_size  # type: ignore[import-untyped]
from reportlab.lib.styles import ParagraphStyle  # type: ignore[import-untyped]
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]
from reportlab.platypus import (  # type: ignore[import-untyped]
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.review.assistant.send_package import LETTER_KIND, ROUND_WORDS, Letter

__all__ = ["ACTION_COLOURS", "comment_letter_pdf"]

_FONT: Final = "Helvetica"
_BOLD: Final = "Helvetica-Bold"
_DARK: Final = Color(0.1, 0.1, 0.1)
_GREY: Final = Color(0.4, 0.4, 0.4)
_RULE: Final = Color(0.82, 0.82, 0.82)
#: The same red and green as the cloud drawing (`proof_drawing.RED` / `GREEN`).
_RED: Final = Color(0.75, 0.1, 0.1)
_GREEN: Final = Color(0.0, 0.47, 0.2)
_AMBER: Final = Color(0.63, 0.29, 0.02)
#: The review result's bar colour: red (resubmit, rejected), dark amber (as noted), green
#: (approved), grey (the check did not finish).
ACTION_COLOURS: Final[Mapping[str, Color]] = {
    "revise_resubmit": _RED,
    "rejected": _RED,
    "approved_as_noted": _AMBER,
    "approved": _GREEN,
    "not_for_sending": _GREY,
}
_MARGIN: Final = 54.0


def _hex(colour: Color) -> str:
    return str(colour.hexval()).replace("0x", "#")


def _styles() -> dict[str, ParagraphStyle]:
    body = ParagraphStyle("body", fontName=_FONT, fontSize=10, leading=13.5, textColor=_DARK)
    return {
        "body": body,
        "brand": ParagraphStyle(
            "brand", parent=body, fontName=_BOLD, fontSize=9, leading=11, textColor=_GREY
        ),
        "title": ParagraphStyle(
            "title", parent=body, fontName=_BOLD, fontSize=18, leading=23, spaceAfter=10
        ),
        "label": ParagraphStyle("label", parent=body, fontSize=9, leading=12, textColor=_GREY),
        "value": ParagraphStyle("value", parent=body, fontName=_BOLD, fontSize=10, leading=12.5),
        "action": ParagraphStyle(
            "action", parent=body, fontName=_BOLD, fontSize=15, leading=19, spaceAfter=2
        ),
        "small": ParagraphStyle("small", parent=body, fontSize=8, leading=10.5, textColor=_GREY),
        "section": ParagraphStyle(
            "section",
            parent=body,
            fontName=_BOLD,
            fontSize=12,
            leading=16,
            spaceBefore=14,
            spaceAfter=6,
            keepWithNext=1,
        ),
        "number": ParagraphStyle("number", parent=body, fontName=_BOLD, fontSize=11, leading=14),
    }


def _head(letter: Letter, styles: Mapping[str, ParagraphStyle], width: float) -> Table:
    identity = letter.identity
    pairs = [
        ("Project", identity.project, "Vendor", identity.vendor),
        ("Submittal", letter.submittal, "Spec section", identity.spec_section),
        ("Drawing set", letter.drawing_set, "Round", str(letter.round_no)),
        ("Received", identity.received, "Reviewed", letter.date),
        ("Products", letter.products, "Reviewer", letter.reviewer_line),
    ]
    rows = [
        [
            Paragraph(escape(a), styles["label"]),
            Paragraph(escape(b), styles["value"]),
            Paragraph(escape(c), styles["label"]),
            Paragraph(escape(d), styles["value"]),
        ]
        for a, b, c, d in pairs
    ]
    half = width / 2
    table = Table(rows, colWidths=[78, half - 78, 78, half - 78], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, _RULE),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _action_box(letter: Letter, styles: Mapping[str, ParagraphStyle], width: float) -> Table:
    action = letter.action
    colour = ACTION_COLOURS[action.action]
    why = f"{action.reason} {action.note}" if action.note else action.reason
    inner = [
        Paragraph("Review result", styles["label"]),
        Paragraph(f'<font color="{_hex(colour)}">{escape(action.words)}</font>', styles["action"]),
        Paragraph(escape(why), styles["body"]),
        Paragraph(f"<b>Action required by:</b> {escape(action.required_by)}", styles["body"]),
        Spacer(1, 6),
        Paragraph(escape(letter.stamp), styles["small"]),
        Paragraph(escape(action.disclaimer), styles["small"]),
    ]
    box = Table([[inner]], colWidths=[width], hAlign="LEFT")
    box.setStyle(
        TableStyle(
            [
                ("LINEBEFORE", (0, 0), (0, 0), 4, colour),
                ("BOX", (0, 0), (-1, -1), 0.5, _RULE),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    return box


def _numbered(
    label: str, lines: list[Any], styles: Mapping[str, ParagraphStyle], width: float
) -> Any:
    row = Table(
        [[Paragraph(escape(label), styles["number"]), lines]],
        colWidths=[44, width - 44],
        hAlign="LEFT",
    )
    row.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, _RULE),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    return KeepTogether([row])


def _previous(letter: Letter, styles: Mapping[str, ParagraphStyle], width: float) -> list[Any]:
    if not letter.previous:
        return []
    story: list[Any] = [
        Paragraph(f"Previous comments ({len(letter.previous)})", styles["section"]),
        Paragraph(escape(f"{letter.previous_line}."), styles["label"]),
        Spacer(1, 4),
    ]
    for item in letter.previous:
        colour = _GREEN if item.words == ROUND_WORDS["fixed"] else _RED
        if item.words == ROUND_WORDS["not_compared"]:
            colour = _GREY
        lines = [
            Paragraph(f"<b>{escape(item.item)}</b>", styles["body"]),
            Paragraph(
                f'<font color="{_hex(colour)}"><b>{escape(item.words)}</b></font>'
                + (f": {escape(item.detail)}" if item.detail else ""),
                styles["body"],
            ),
        ]
        story.append(_numbered(item.label, lines, styles, width))
    return story


def _corrections(letter: Letter, styles: Mapping[str, ParagraphStyle], width: float) -> list[Any]:
    story: list[Any] = [Paragraph(f"Comments ({len(letter.corrections)})", styles["section"])]
    if not letter.corrections:
        story.append(
            Paragraph("None: no wrong value was proved on these drawings.", styles["body"])
        )
        return story
    story.append(
        Paragraph(
            "Each comment number is the numbered cloud on the enclosed GV cloud drawing. Please "
            "answer each comment in writing.",
            styles["label"],
        )
    )
    story.append(Spacer(1, 4))
    for item in letter.corrections:
        page = f"Page {item.page}" if item.page is not None else "Page not marked"
        status = f" · {item.round_word}" if item.round_word else ""
        head = Paragraph(
            f"<b>{escape(page)} · {escape(item.item)}</b>"
            f'<font color="{_hex(_GREY)}"> · {escape(item.action)}{escape(status)}</font>',
            styles["body"],
        )
        if item.sentence:
            # The proof card's own sentence (#1804): the letter, the register and the cloud
            # drawing say the same thing in the same words.
            lines = [head, Paragraph(_sentence(item.sentence), styles["body"])]
            if item.sum_line:
                lines.append(Paragraph(escape(item.sum_line), styles["body"]))
            lines.append(
                Paragraph(
                    f"Reference: {escape(item.source)}"
                    + (f" · {escape(item.checked)}" if item.checked else ""),
                    styles["label"],
                )
            )
        else:
            lines = [
                head,
                Paragraph(
                    f'<font color="{_hex(_RED)}">Vendor shows <b>{escape(item.vendor_shows)}</b>'
                    "</font>",
                    styles["body"],
                ),
                Paragraph(
                    f'<font color="{_hex(_GREEN)}"><b>{escape(item.requires)}</b></font>',
                    styles["body"],
                ),
                Paragraph(f"Source: {escape(item.source)}", styles["label"]),
            ]
        story.append(_numbered(item.label, lines, styles, width))
    return story


def _sentence(runs: tuple[tuple[str, str], ...]) -> str:
    """The sentence's pieces as markup: the vendor's value red, the should-be green, both bold."""
    out: list[str] = []
    for text, role in runs:
        words = escape(text)
        if role == "vendor":
            out.append(f'<font color="{_hex(_RED)}"><b>{words}</b></font>')
        elif role == "should":
            out.append(f'<font color="{_hex(_GREEN)}"><b>{words}</b></font>')
        else:
            out.append(words)
    return "".join(out)


def _story(letter: Letter, width: float) -> list[Any]:
    styles = _styles()
    story: list[Any] = [
        Paragraph("GV REVIEW · SUBMITTAL REVIEW COMMENTS", styles["brand"]),
        Spacer(1, 6),
        Paragraph("Submittal review comments", styles["title"]),
        _head(letter, styles, width),
        Spacer(1, 14),
        _action_box(letter, styles, width),
        Spacer(1, 12),
        Paragraph(
            "Thank you for your submittal. We have checked the drawings against the architect's "
            "drawings and GV's standards. Our comments are below.",
            styles["body"],
        ),
    ]
    story += _previous(letter, styles, width)
    story += _corrections(letter, styles, width)
    story.append(Paragraph(f"Not compared ({letter.not_compared})", styles["section"]))
    if letter.not_compared:
        story.append(
            Paragraph(
                escape(
                    f"{letter.not_compared} "
                    f"{'item' if letter.not_compared == 1 else 'items'} could not be compared "
                    "against the architect's drawings or GV's standards (for example, where the "
                    "architect gives no size). They are listed with the reason in "
                    f"{letter.not_compared_where}, with what is needed to compare each. They "
                    "were not reviewed."
                ),
                styles["body"],
            )
        )
    else:
        story.append(Paragraph("None: every item was compared.", styles["body"]))
    story += [
        Paragraph("Next step", styles["section"]),
        Paragraph(escape(letter.action.next_step), styles["body"]),
        Spacer(1, 10),
        Paragraph("Kind regards,", styles["body"]),
        Paragraph("GV", styles["value"]),
    ]
    if letter.enclosures:
        story.append(Paragraph("Enclosures", styles["section"]))
        story += [Paragraph(escape(f"· {name}"), styles["body"]) for name in letter.enclosures]
    return story


def comment_letter_pdf(letter: Letter, out: Path) -> None:
    """The comment letter, written to `out`."""
    width, height = letter_size

    def footer(c: canvas.Canvas, document: Any) -> None:
        c.saveState()
        c.setFillColor(_GREY)
        c.setFont(_FONT, 8)
        c.drawString(_MARGIN, _MARGIN - 24, f"{letter.drawing_set} · {LETTER_KIND}")
        c.drawRightString(width - _MARGIN, _MARGIN - 24, f"Page {document.page}")
        c.restoreState()

    out.parent.mkdir(parents=True, exist_ok=True)
    document = BaseDocTemplate(
        str(out),
        pagesize=letter_size,
        title=f"{LETTER_KIND}: {letter.drawing_set}",
        author="GV Review",
        invariant=1,
        leftMargin=_MARGIN,
        rightMargin=_MARGIN,
        topMargin=_MARGIN,
        bottomMargin=_MARGIN,
    )
    usable = width - 2 * _MARGIN
    frame = Frame(_MARGIN, _MARGIN, usable, height - 2 * _MARGIN, id="body")
    document.addPageTemplates([PageTemplate(id="page", frames=[frame], onPage=footer)])
    document.build(_story(letter, usable))
