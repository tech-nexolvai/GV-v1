"""Whose drawing is whose on a sheet that prints no GV heading (#1825): decided by code from the
content of both drawings, with the rule code already uses for such sheets (#1052).

A combined sheet pastes the architect's drawing and the vendor's on one page. GV usually types a
heading over each (`ID SET ELEVATION`, `VENDOR'S SHOP DRAWING`) and the set pack reads the roles
from those (`set_pack._page_entries`). Some sheets print no heading at all: then every drawing on
the page was `general`, no label had a side, code paired nothing, and the full check compared no
page of such a set (the paid runs of 2026-10-21: 0 of 5 pages, every item Not compared).

**The rule is not new.** `extraction/architect/views.decide_without_headings` (#1052, decision D2
of 2026-10-09) decides both roles on a page with no heading only when it is clear on both sides:
exactly one drawing is clearly the architect's (feet-and-inches labels outnumbering the
inch-or-millimetre ones, and an architectural scale printed in it or the one its labels are drawn
to), at least one other is clearly the vendor's (a ratio scale, or inch-or-millimetre labels and
no feet and inches), and every other drawing is neither. Anything less: no role, as before. This
module runs that reader (`read_architect_page`) on such pages only and copies the roles it decides
onto the page map's drawings, matched by their boxes; a drawing it does not decide stays `general`.

**Which pages.** A page with two or more pasted drawings, none of them with a role (no GV
heading, not a separate file's whole-page drawing) and no heading printed on the page. GV's headed
sheets and the separate files are never touched.

**Never a verdict.** A role only says which side a label is on. A red still needs everything code
proves today (both values printed at their spots, the same ends, the pair); a label lying inside
both an architect's and a vendor's drawing gets no side (`dim_labels._side`).

Read in a process of its own (`python -m app.review.assistant.content_sides <pdf> <page>...`,
one JSON object out), as the labels are: the reader is pure Python over the PDF, seconds a page.
Loaded by name, never imported by `app/api/` (`tests/api/test_no_heavy_work.py`).

Verification: `tests/review/assistant/test_content_sides.py` (made-up sheets).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from importlib import import_module
from pathlib import Path
from typing import Any, Final

__all__ = ["ROLE_BY", "apply_sides", "decide_sides", "unheaded_pages"]

#: What a drawing's `role_by` says when its role came from this module.
ROLE_BY: Final = "content"
#: A decided drawing matches a page-map drawing when every edge is this close (points).
_MATCH_PT: Final = 3.0
#: The reader's roles, as the page map writes them.
_ROLE_WORDS: Final = {"arch": "architect", "shop": "vendor"}
#: The dpi the reader opens its pictures at: roles never depend on it.
_DPI: Final = 72


def unheaded_pages(entries: Sequence[Mapping[str, Any]]) -> list[int]:
    """The pages (numbers) whose drawings may be decided here: two or more pasted drawings, every
    one `general` and none a separate file's (`from_upload`), on a page that prints no heading."""
    pages: list[int] = []
    for entry in entries:
        halves = [h for h in entry.get("halves") or [] if isinstance(h, Mapping)]
        if len(halves) < 2 or entry.get("headings"):
            continue
        if any(h.get("role") != "general" or h.get("from_upload") for h in halves):
            continue
        pages.append(int(entry["page"]))
    return pages


def decide_sides(pdf: Path, pages: Sequence[int]) -> dict[int, list[dict[str, Any]]]:
    """`{page: [{box, role, reason}]}`: the drawings the reader decided by the content of both
    drawings (`by_content_alone`), in page points as shown, origin top-left. A page it cannot
    read, or decides nothing on, is left out."""
    reader = import_module("extraction.architect.reader")
    data = pdf.read_bytes()
    out: dict[int, list[dict[str, Any]]] = {}
    for page in pages:
        try:
            reading = reader.read_architect_page(
                data, page - 1, settings=reader.MEASURED_ARCHITECT_SETTINGS, dpi=_DPI
            )
        except Exception as failed:  # noqa: BLE001 - an unreadable page keeps no role, as before
            print(f"page {page} not read: {type(failed).__name__}", file=sys.stderr)
            continue
        decided = []
        for view in reading.views:
            judgment = view.judgment
            role = _ROLE_WORDS.get(str(getattr(judgment.agreed, "value", "")))
            if role is None or not judgment.by_content_alone:
                continue
            box = view.box
            decided.append(
                {
                    "box": [round(float(v), 1) for v in (box.x0, box.top, box.x1, box.bottom)],
                    "role": role,
                    "reason": judgment.reason,
                }
            )
        if decided:
            out[page] = decided
    return out


def _same_box(a: Sequence[Any], b: Sequence[Any]) -> bool:
    return (
        len(a) == 4
        and len(b) == 4
        and all(abs(float(x) - float(y)) <= _MATCH_PT for x, y in zip(a, b, strict=True))
    )


def apply_sides(
    entries: Sequence[dict[str, Any]], decided: Mapping[int, Sequence[Mapping[str, Any]]]
) -> list[int]:
    """Copy the decided roles onto the page map's drawings in place (each matched by its box; a
    drawing matched by none, or by two, stays as it was) and set each page's role again. A page
    keeps its drawings unless both an architect's and a vendor's drawing are matched. Returns
    the pages that changed."""
    changed: list[int] = []
    for entry in entries:
        found = decided.get(int(entry.get("page") or 0))
        if not found:
            continue
        halves = [h for h in entry.get("halves") or [] if isinstance(h, dict)]
        roles: dict[int, Mapping[str, Any]] = {}
        for index, half in enumerate(halves):
            matches = [d for d in found if _same_box(half.get("box") or (), d.get("box") or ())]
            if len(matches) == 1:
                roles[index] = matches[0]
        if {str(d["role"]) for d in roles.values()} != {"architect", "vendor"}:
            continue
        for index, decision in roles.items():
            halves[index]["role"] = str(decision["role"])
            halves[index]["role_by"] = ROLE_BY
            halves[index]["role_reason"] = str(decision.get("reason") or "")
        entry["role"] = "+".join(sorted({h["role"] for h in halves} - {"general"}))
        changed.append(int(entry["page"]))
    return changed


def main(argv: Sequence[str]) -> int:
    pdf, *pages = argv
    print(json.dumps(decide_sides(Path(pdf), [int(page) for page in pages])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
