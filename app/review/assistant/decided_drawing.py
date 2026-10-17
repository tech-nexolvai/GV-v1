"""The cloud drawing and the check report remade from the reviewer's decisions (#1521), by code.

Step 5 of the reviewer's path: the reviewer agrees (✓) or dismisses (✗ Not a problem) each finding
the AI called wrong or left for their decision. What goes to the vendor carries only the agreed
items:

- **Clouds** only at agreed findings, drawn as a reviewer would (#1537, `chat_drawing.draw_markup`):
  a tight red cloud at the value, a red numbered circle and a note in clear space ("× 12 Item:
  vendor X, make it Y (architect Z)"), on a copy without earlier review markup when the file has
  some. Undecided, not-a-problem and right items are never marked.
- **Stamp** on page 1 (and the notes pages' heading): "Reviewed by <name>" once every item needing
  a decision was decided, else the usual "AI markup: draft, not GV's signed review".
- **Notes pages** and **report**: the agreed items (by page, each with a small picture of its
  spot), then "Not yet decided", "Not a problem" and "Right", each listed separately.

Each finding carries its latest decision under `decision` (`chat_decisions.with_decisions`).

**Loaded by name, never imported** (`chat_decisions.py` uses `import_module`): `app/api/` must not
reach a PDF renderer by import (`tests/api/test_no_heavy_work.py`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from app.review.assistant.chat_drawing import (
    MARKUP_TITLE,
    REPORT_TITLE,
    Entry,
    Mark,
    Section,
    draw_markup,
    entry_lines,
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
    "Red cloud, red numbered circle and note: the reviewer agreed the vendor's value is wrong; the "
    "note says what it needs to be. Items not yet decided, items marked not a problem and right "
    "items are not marked; they are listed below."
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


def _agreed(finding: Mapping[str, Any]) -> dict[str, Any]:
    """An agreed finding as the drawing shows it: wrong, whatever the AI called it."""
    return {**finding, "status": "different", "trust_level": "confirmed"}


def decided_markup_pdf(
    source: Path,
    findings: Sequence[tuple[int, Mapping[str, Any], int | None, Box | None, str]],
    out: Path,
) -> None:
    """A copy of `source` with a tight red cloud, a numbered marker and a note at each agreed
    finding that can be placed (on a clean copy when the file carries earlier review markup), the
    stamp on page 1, and notes pages: agreed (by page, with a picture of each spot), not yet
    decided, not a problem, right. `findings`: (number, finding, page, box, where), as
    `chat_findings.place_findings` makes them."""
    plain = [finding for _n, finding, _p, _b, _w in findings]
    title = _title(plain, MARKUP_TITLE)
    grouped = groups(plain)
    marks: list[Mark] = []
    entries: dict[str, list[Entry]] = {key: [] for key in grouped}
    for key, positions in grouped.items():
        for n in positions:
            number, finding, page_number, box, where = findings[n]
            if key == "agreed":
                shown = _agreed(finding)
                lines = [*entry_lines(shown), where, _decision_words(finding)]
                if page_number is not None and box is not None:
                    value = (
                        str(finding.get("vendor") or "")
                        if isinstance(finding.get("vendor_box"), Mapping)
                        else str(finding.get("architect") or "")
                    )
                    marks.append(Mark(number, shown, page_number, box, "wrong", True, value))
                entries[key].append(Entry(number, shown, "wrong", lines, picture=True))
                continue
            lines = [f"Architect: {finding.get('architect')}   Vendor: {finding.get('vendor')}"]
            if key == "undecided":
                lines.append(f"AI says: {_AI_SAYS[str(cloud_kind(finding))]}")
                if finding.get("why"):
                    lines.append(f"Why: {finding['why']}")
            elif key == "dismissed":
                lines.append(_decision_words(finding))
            kind = cloud_kind(finding) if key == "undecided" else None
            entries[key].append(Entry(number, finding, kind, lines))
    for listed in entries.values():
        listed.sort(key=lambda e: (int(e.finding.get("page") or 0), e.number))
    counts = (
        f"{len(plain)} items: {len(grouped['agreed'])} agreed (red clouds), "
        f"{len(grouped['undecided'])} not yet decided, {len(grouped['dismissed'])} not a problem, "
        f"{len(grouped['right'])} right (none of these is marked)."
    )
    front = [counts, DECIDED_LEGEND]
    sections = (
        Section("Agreed: needs correction", entries["agreed"]),
        Section("Not yet decided", entries["undecided"]),
        Section("Not a problem (not clouded)", entries["dismissed"]),
        Section("Right (not clouded)", entries["right"]),
    )
    draw_markup(source, marks, out, title=title, front=front, sections=sections)


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
