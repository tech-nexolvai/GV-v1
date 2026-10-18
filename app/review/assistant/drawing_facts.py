"""The `drawing_facts` tool's words: code's facts about one page, read from the set pack.

The set pack computes facts once per set, $0, before any AI call: each drawing's scale measured
from its printed dimensions (`view_scales`, `scales.json`), architect-vendor dimension pairs by
drawn position (`dim_pairs`, `pairs.json`) and the notes and tags each drawing prints
(`drawing_notes`, `notes.json`). The lead and the helpers read them through one small tool instead
of finding them again by eye. This module only reads those files and says what they hold, in
plain lines; it computes nothing new.

Verification: `tests/review/assistant/test_drawing_facts.py`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from app.review.assistant import dim_pairs, drawing_notes, view_scales

__all__ = ["FACTS", "MAX_CHARS", "page_facts"]

#: What the tool can say about a page.
FACTS: Final = ("scale", "pairs", "notes")
#: The most characters one answer gives (each list is cut with a count of what is left).
MAX_CHARS: Final = 8000
_LISTED: Final = 25

_SIDES: Final = {"architect": "architect's", "vendor": "vendor's", "brand": "brand prototype's"}


def _box(box: Sequence[float] | None) -> str:
    if not box:
        return ""
    return " at [" + ", ".join(str(round(float(v))) for v in box) + "]"


def _cut(items: Sequence[str], limit: int = _LISTED) -> str:
    shown = "; ".join(items[:limit])
    return shown + (f"; ... {len(items) - limit} more" if len(items) > limit else "")


def _scales(folder: Path | None, file: str, page: int) -> list[str]:
    found = view_scales.scales_on(folder, file, page)
    if not found:
        return [
            (
                "Scale: code measured none on this page (fewer than three printed dimensions of "
                "one drawing agree within 1%)."
            )
        ]
    lines: list[str] = []
    for scale in found:
        labels = ", ".join(scale.labels[:8]) + (", ..." if len(scale.labels) > 8 else "")
        note = ""
        if scale.note is not None:
            agrees = scale.note_agrees
            note = f'; its printed note {scale.note} {"agrees" if agrees else "does NOT agree"}' + (
                "" if agrees else " (GV resized the drawing: trust the measured scale)"
            )
        lines.append(
            f"Scale: {_SIDES.get(scale.side, scale.side)} drawing{_box(scale.box)} about "
            f"1:{round(scale.ratio)} ({scale.in_per_pt:.4f} in per point) from "
            f"{len(scale.labels)} printed dimensions ({labels}), spread "
            f"{scale.spread_percent:.1f}%{note}."
        )
    return lines


def _pairs(folder: Path | None, file: str, page: int) -> list[str]:
    entry = dim_pairs.pairs_on(folder, file, page)
    if entry is None:
        return ["Pairs: none (this page has no architect and vendor drawing, or not computed)."]
    pairs = [p for p in entry.get("pairs") or [] if isinstance(p, Mapping)]
    lines: list[str] = []
    for pair in pairs:
        arch = pair.get("architect") or {}
        vendor = " + ".join(f"{v.get('id')} {v.get('text')}" for v in pair.get("vendor") or [])
        proof = "both printed as text" if pair.get("printed_both") else "a value from drawn ink"
        lines.append(
            f"{str(pair.get('status')).upper()}: architect {arch.get('id')} {arch.get('text')} "
            f"vs vendor {vendor} ({pair.get('architect_in')} vs {pair.get('vendor_in')}; "
            f"{proof}; {pair.get('how')})"
        )
    head = (
        f"Pairs by drawn position ({len(pairs)}): " + _cut(lines)
        if lines
        else "Pairs by drawn position: none on this page."
    )
    notes = [str(note) for note in entry.get("notes") or []]
    why = [f"Not paired: {_cut(notes, 8)}"] if notes else []
    return [head, *why]


def _notes(folder: Path | None, file: str, page: int) -> list[str]:
    entry = drawing_notes.notes_on(folder, file, page)
    if entry is None:
        return ["Notes and tags: not computed for this page yet."]
    lines: list[str] = []
    for drawing in entry.get("drawings") or []:
        if not isinstance(drawing, Mapping):
            continue
        who = (
            "outside the drawings"
            if drawing.get("role") == "page"
            else f"{_SIDES.get(str(drawing.get('role')), str(drawing.get('role')))} drawing"
            + (f" ({drawing['heading']})" if drawing.get("heading") else "")
            + (f", round {drawing['round']}" if drawing.get("round") else "")
        )
        tags = sorted({str(t.get("text")) for t in drawing.get("tags") or []})
        notes = [f'"{n.get("text")}"{_box(n.get("box"))}' for n in drawing.get("notes") or []]
        if tags or notes:
            lines.append(f"{who}: tags {', '.join(tags) or 'none'}; notes: {_cut(notes) or 'none'}")
    only = entry.get("architect_only_tags") or []
    if only:
        lines.append(
            "Architect's tags the vendor's drawing on this page does not print: " + ", ".join(only)
        )
    vendor_only = entry.get("vendor_only_tags") or []
    if vendor_only:
        lines.append(
            "Vendor's tags the architect's drawing does not print: " + ", ".join(vendor_only)
        )
    return lines or ["Notes and tags: none printed on this page."]


def page_facts(folder: Path | None, file: str, page: int, facts: Sequence[str] = FACTS) -> str:
    """Code's facts about `page` of `file`, in plain lines (at most `MAX_CHARS`)."""
    if folder is None:
        return "This chat has no set pack: code's facts are not available."
    lines = [f"Page {page}: code's facts from the set pack ($0, computed before you started)."]
    if "scale" in facts:
        lines += _scales(folder, file, page)
    if "pairs" in facts:
        lines += _pairs(folder, file, page)
    if "notes" in facts:
        lines += _notes(folder, file, page)
    text = "\n".join(lines)
    if len(text) > MAX_CHARS:
        text = text[: MAX_CHARS - 40].rstrip() + "\n... (cut: ask for one fact at a time)"
    return text
