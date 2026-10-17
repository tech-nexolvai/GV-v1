"""The cloud drawing and the check report remade from the reviewer's decisions (#1521), by code.

Step 5 of the reviewer's path: the reviewer agrees (✓) or dismisses (✗ Not a problem) each finding
the AI called wrong or left for their decision. What goes to the vendor carries only the agreed
items:

- **Clouds** only at agreed findings: a red cloud, a drawn cross and "n Item: Vendor: X — Needs: Y
  (architect Z)". Undecided, not-a-problem and right items are never clouded.
- **Stamp** on page 1 (and the notes pages' heading): "Reviewed by <name>" once every item needing
  a decision was decided, else the usual "AI markup: draft, not GV's signed review".
- **Notes page** and **report**: the agreed items, then "Not yet decided", "Not a problem" and
  "Right", each listed separately.

Each finding carries its latest decision under `decision` (`chat_decisions.with_decisions`).

**Loaded by name, never imported** (`chat_decisions.py` uses `import_module`): `app/api/` must not
reach a PDF renderer by import (`tests/api/test_no_heavy_work.py`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import white  # type: ignore[import-untyped]
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from app.review.assistant.chat_drawing import (
    _CLOUD_PAD,
    _NOTE_CHARS,
    _NOTE_SIZE,
    _RED,
    MARKUP_TITLE,
    REPORT_TITLE,
    _cloud,
    _notes_pages,
    _overlay,
    _shown_to_user,
    _stamp,
)
from app.review.assistant.chat_trust import cloud_kind, needs_text

__all__ = [
    "DECIDED_LEGEND",
    "decided_markup_pdf",
    "decided_report_pdf",
    "groups",
    "reviewed_by",
]

type Box = tuple[float, float, float, float]

DECIDED_LEGEND: Final = (
    "Red cloud with a cross: the reviewer agreed the vendor's value is wrong; the note says what "
    "it needs to be. Items not yet decided, items marked not a problem and right items are not "
    "clouded; they are listed below."
)
#: What the AI called an item, for the "Not yet decided" lists.
_AI_SAYS: Final = {"wrong": "Wrong", "check": "Your decision"}


def _decided(finding: Mapping[str, Any]) -> str | None:
    decision = finding.get("decision")
    return str(decision.get("decision")) if isinstance(decision, Mapping) else None


def groups(findings: Sequence[Mapping[str, Any]]) -> dict[str, list[int]]:
    """The findings' positions (0-based) by group: `agreed`, `undecided`, `dismissed`, `right`."""
    out: dict[str, list[int]] = {"agreed": [], "undecided": [], "dismissed": [], "right": []}
    for n, finding in enumerate(findings):
        if cloud_kind(finding) is None:
            out["right"].append(n)
            continue
        decided = _decided(finding)
        if decided == "agree":
            out["agreed"].append(n)
        elif decided == "not_a_problem":
            out["dismissed"].append(n)
        else:
            out["undecided"].append(n)
    return out


def reviewed_by(findings: Sequence[Mapping[str, Any]]) -> str | None:
    """The people who decided, when every item needing a decision was decided (else `None`)."""
    grouped = groups(findings)
    if grouped["undecided"] or not (grouped["agreed"] or grouped["dismissed"]):
        return None
    names: list[str] = []
    for n in sorted(grouped["agreed"] + grouped["dismissed"]):
        name = str(findings[n]["decision"].get("by") or "")
        if name and name not in names:
            names.append(name)
    return ", ".join(names) or None


def _title(findings: Sequence[Mapping[str, Any]], draft: str) -> str:
    names = reviewed_by(findings)
    return draft if names is None else f"Reviewed by {names}"


def _note(number: int, finding: Mapping[str, Any]) -> str:
    needs, architect = needs_text(finding), str(finding.get("architect"))
    text = f"{number} {finding.get('item')}: Vendor: {finding.get('vendor')} — Needs: {needs}"
    if needs != architect:
        text += f" (architect {architect})"
    return text if len(text) <= _NOTE_CHARS else text[: _NOTE_CHARS - 1] + "…"


def _draw_agreed(
    c: canvas.Canvas, page: Any, marks: Sequence[tuple[int, Mapping[str, Any], Box]]
) -> None:
    rotation = int(page.get("/Rotate", 0) or 0) % 360
    for number, finding, (x0, y0, x1, y1) in marks:
        ax, ay = _shown_to_user(page, min(x0, x1), min(y0, y1))
        bx, by = _shown_to_user(page, max(x0, x1), max(y0, y1))
        c.setStrokeColor(_RED)
        c.setLineWidth(1.2)
        _cloud(
            c,
            min(ax, bx) - _CLOUD_PAD,
            min(ay, by) - _CLOUD_PAD,
            max(ax, bx) + _CLOUD_PAD,
            max(ay, by) + _CLOUD_PAD,
        )
        note = _note(number, finding)
        nx, ny = _shown_to_user(page, max(x0, x1) + _CLOUD_PAD + 8, min(y0, y1) - _CLOUD_PAD)
        c.saveState()
        c.translate(nx, ny)
        c.rotate(rotation)
        c.setFont("Helvetica-Bold", _NOTE_SIZE)
        lead = _NOTE_SIZE + 2
        width = c.stringWidth(note, "Helvetica-Bold", _NOTE_SIZE) + lead
        c.setFillColor(white)
        c.rect(-2, -_NOTE_SIZE - 2, width + 4, _NOTE_SIZE + 4, stroke=0, fill=1)
        c.setFillColor(_RED)
        # The cross is drawn: the standard fonts have no such sign.
        c.setLineWidth(1.4)
        c.line(0, -_NOTE_SIZE + 1, _NOTE_SIZE - 2, -1)
        c.line(0, -1, _NOTE_SIZE - 2, -_NOTE_SIZE + 1)
        c.drawString(lead, -_NOTE_SIZE + 1, note)
        c.restoreState()


def _decision_words(finding: Mapping[str, Any]) -> str:
    decision = finding.get("decision")
    if not isinstance(decision, Mapping):
        return ""
    words = f"Decided by {decision.get('by')}"
    at = decision.get("at")
    if at is not None:
        words += f" on {str(at)[:16].replace('T', ' ')}"
    if decision.get("note"):
        words += f": {decision['note']}"
    return words


def decided_markup_pdf(
    source: Path,
    findings: Sequence[tuple[int, Mapping[str, Any], int | None, Box | None, str]],
    out: Path,
) -> None:
    """A copy of `source` with a red cloud and note at each agreed finding that can be placed, the
    stamp on page 1, and notes pages: agreed, not yet decided, not a problem, right.
    `findings`: (number, finding, page, box, where), as `chat_findings.place_findings` makes them.
    """
    writer = PdfWriter(clone_from=PdfReader(str(source)))
    plain = [finding for _n, finding, _p, _b, _w in findings]
    title = _title(plain, MARKUP_TITLE)
    grouped = groups(plain)
    agreed = set(grouped["agreed"])
    by_page: dict[int, list[tuple[int, Mapping[str, Any], Box]]] = {}
    where_of: dict[int, str] = {}
    for n, (number, finding, page_number, box, where) in enumerate(findings):
        where_of[n] = where
        if n not in agreed:
            continue
        if page_number is not None and box is not None:
            if 1 <= page_number <= len(writer.pages):
                by_page.setdefault(page_number, []).append((number, finding, box))
            else:
                where_of[n] = f"Not marked: the file has no page {page_number}."
    for page_number, page in enumerate(writer.pages, start=1):
        marks = by_page.get(page_number, [])
        if not marks and page_number != 1:
            continue
        media = page.mediabox

        def draw(
            c: canvas.Canvas, page: Any = page, marks: Any = marks, n: int = page_number
        ) -> None:
            if marks:
                _draw_agreed(c, page, marks)
            if n == 1:
                _stamp(c, page, title)

        page.merge_page(_overlay(float(media.right), float(media.top), draw))
    first = writer.pages[0]
    width, height = float(first.cropbox.width), float(first.cropbox.height)
    if int(first.get("/Rotate", 0) or 0) % 180 == 90:
        width, height = height, width
    lines: list[tuple[str, bool]] = [(DECIDED_LEGEND, False)]
    sections = (
        ("agreed", "Agreed: needs correction"),
        ("undecided", "Not yet decided"),
        ("dismissed", "Not a problem (not clouded)"),
        ("right", "Right (not clouded)"),
    )
    for key, heading in sections:
        lines.append((f"{heading} ({len(grouped[key])})", True))
        for n in grouped[key]:
            number, finding = findings[n][0], findings[n][1]
            vs = f" (architect page {finding['vs_page']})" if finding.get("vs_page") else ""
            head = f"{number}  page {finding.get('page')}{vs}  {finding.get('item')}"
            if key == "undecided":
                head += f"  [AI says: {_AI_SAYS[str(cloud_kind(finding))]}]"
            lines.append((head, True))
            lines.append(
                (f"Architect: {finding.get('architect')}   Vendor: {finding.get('vendor')}", False)
            )
            if key == "agreed":
                lines.append((f"Needs: {needs_text(finding)}", False))
                lines.append((where_of[n], False))
            if key in ("agreed", "dismissed"):
                lines.append((_decision_words(finding), False))
            elif key == "undecided" and finding.get("why"):
                lines.append((f"Why: {finding['why']}", False))
        if not grouped[key]:
            lines.append(("None.", False))
    for page in _notes_pages(width, height, lines, title):
        writer.add_page(page)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as handle:
        writer.write(handle)


def decided_report_pdf(
    out: Path,
    summary: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]],
    not_checked: Sequence[str],
) -> None:
    """The check report from the decisions: a cover, then What's wrong (agreed), Not yet decided,
    Not a problem, What's right and Not checked."""
    from xml.sax.saxutils import escape

    from reportlab.lib import colors  # type: ignore[import-untyped]
    from reportlab.lib.pagesizes import landscape, letter  # type: ignore[import-untyped]
    from reportlab.lib.styles import getSampleStyleSheet  # type: ignore[import-untyped]
    from reportlab.platypus import (  # type: ignore[import-untyped]
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    styles = getSampleStyleSheet()
    body, small = styles["BodyText"], styles["BodyText"].clone("small", fontSize=8, leading=10)

    def p(text: object, style: Any = small) -> Any:
        return Paragraph(escape("" if text is None else str(text)), style)

    def table(
        header: Sequence[str], rows: Sequence[Sequence[object]], widths: Sequence[float]
    ) -> Any:
        data = [[p(h, small) for h in header], *[[p(c) for c in row] for row in rows]]
        grid = Table(data, colWidths=list(widths), repeatRows=1)
        grid.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#999999")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        return grid

    title = _title(findings, REPORT_TITLE)
    grouped = groups(findings)

    def by_whom(f: Mapping[str, Any]) -> object:
        return (f.get("decision") or {}).get("by")

    def note(f: Mapping[str, Any]) -> object:
        return (f.get("decision") or {}).get("note")

    def rows(key: str, cells: Any) -> list[list[object]]:
        return [[n + 1, *cells(findings[n])] for n in grouped[key]]

    sections = (
        (
            f"What's wrong: agreed ({len(grouped['agreed'])})",
            ["#", "Page", "Item", "Vendor", "Needs", "Architect", "Decided by", "Note"],
            rows(
                "agreed",
                lambda f: [
                    f.get("page"),
                    f.get("item"),
                    f.get("vendor"),
                    needs_text(f),
                    f.get("architect"),
                    by_whom(f),
                    note(f),
                ],
            ),
            [26, 34, 170, 80, 70, 80, 80, 110],
        ),
        (
            f"Not yet decided ({len(grouped['undecided'])})",
            ["#", "Page", "Item", "Vendor", "Architect", "AI says", "Why"],
            rows(
                "undecided",
                lambda f: [
                    f.get("page"),
                    f.get("item"),
                    f.get("vendor"),
                    f.get("architect"),
                    _AI_SAYS[str(cloud_kind(f))],
                    f.get("why"),
                ],
            ),
            [26, 34, 170, 90, 90, 70, 170],
        ),
        (
            f"Not a problem ({len(grouped['dismissed'])})",
            ["#", "Page", "Item", "Vendor", "Architect", "Decided by", "Note"],
            rows(
                "dismissed",
                lambda f: [
                    f.get("page"),
                    f.get("item"),
                    f.get("vendor"),
                    f.get("architect"),
                    by_whom(f),
                    note(f),
                ],
            ),
            [26, 34, 170, 90, 90, 90, 150],
        ),
        (
            f"What's right ({len(grouped['right'])})",
            ["#", "Page", "Item", "Value", "Matches"],
            rows(
                "right",
                lambda f: [
                    f.get("page"),
                    f.get("item"),
                    f.get("vendor"),
                    f"{f.get('source')}: {f.get('architect')}",
                ],
            ),
            [26, 34, 230, 120, 210],
        ),
    )
    story: list[Any] = [Paragraph(escape(title), styles["Title"])]
    for label, value in summary.items():
        story.append(Paragraph(f"<b>{escape(str(label))}:</b> {escape(str(value))}", body))
    story.append(PageBreak())
    for heading, header, cells, widths in sections:
        story += [Spacer(1, 10), Paragraph(escape(heading), styles["Heading2"])]
        story.append(table(header, cells, widths) if cells else p("None.", body))
    story += [Spacer(1, 10), Paragraph("Not checked", styles["Heading2"])]
    story += [p(line, body) for line in not_checked] or [p("Nothing left out.", body)]
    out.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(
        str(out),
        pagesize=landscape(letter),
        title=title,
        invariant=1,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36,
    ).build(story)
