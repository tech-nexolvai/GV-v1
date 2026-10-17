"""The notes and tags each drawing prints, listed by code: $0, no AI.

The architect's drawing says more than its dimensions: "PULL-OUT DRAWERS FOR TOWEL STORAGE",
"(2) ADJ. SHELVES", a finish tag `WD-01`, a fixture tag `V-322-CUST`. A helper reading a picture
may miss a note, and the reviewer cannot tell a note it never read from one that is fine. Code
reads every printed phrase once per set (`dim_labels`, the set pack's `dims.json`: the drawing's
own text, black or grey, its pasted drawings included; GV's markup is never read) and this module
sorts them per drawing of the page map:

- **tags**: a phrase shaped like a tag (`WD-01`, `CT-001`, `V-322-CUST`: letters, a dash,
  digits, an optional suffix);
- **notes**: a phrase of at least two words (dimensions, scale notes and lone tags are not notes);
- per page, the **architect's tags the vendor's drawing does not print** and the other way round.

What it proves is observed: the architect prints `V-322-CUST` here and the vendor's drawing on the
same page prints no such tag. Whether a different tag name means the same finish stays a person's
call unless a printed legend says so (the resolver's rule). The notes are for the helpers'
checklist: each architect note is something to look for on the vendor's drawing.

Pure: no PDF, no model. Verification: `tests/review/assistant/test_drawing_notes.py`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

__all__ = ["NOTES_FILE", "notes_on", "page_notes", "set_notes"]

NOTES_FILE: Final = "notes.json"
#: A tag: one to four letters, a dash, one to four digits, an optional letter and suffix.
_TAG: Final = re.compile(r"\b[A-Z]{1,4}-\d{1,4}[A-Z]?(?:-[A-Z]{2,8})?\b")
_WORD: Final = re.compile(r"[A-Za-z]{2,}")
_SCALE: Final = re.compile(r"\bSCALE\b|\d\s*\"?\s*=\s*\d", re.IGNORECASE)
#: The most notes and tags listed per drawing.
MAX_PER_DRAWING: Final = 120

_ROLES: Final = {"architect": "architect", "vendor": "vendor", "brand_prototype": "brand"}


def _inside(box: Sequence[float], area: Sequence[float]) -> bool:
    cx, cy = (float(box[0]) + float(box[2])) / 2, (float(box[1]) + float(box[3])) / 2
    return float(area[0]) <= cx <= float(area[2]) and float(area[1]) <= cy <= float(area[3])


def _kind(text: str) -> str | None:
    """`tag`, `note` or `None` (a dimension, a scale note, a lone word or number)."""
    if _SCALE.search(text):
        return None
    if _TAG.fullmatch(text.strip(" .,;:()")):
        return "tag"
    return "note" if len(_WORD.findall(text)) >= 2 else None


def page_notes(
    page: int, phrases: Sequence[Mapping[str, Any]], halves: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """One page's drawings with their tags and notes, and the tags printed on one side only."""
    drawings: list[dict[str, Any]] = [
        {
            "role": _ROLES.get(str(half.get("role")), "general"),
            "round": half.get("round"),
            "heading": half.get("heading"),
            "box": list(half.get("box") or []),
            "tags": [],
            "notes": [],
        }
        for half in halves
        if isinstance(half.get("box"), Sequence) and len(half.get("box") or []) == 4
    ]
    outside: dict[str, Any] = {
        "role": "page",
        "round": None,
        "heading": None,
        "box": None,
        "tags": [],
        "notes": [],
    }
    for phrase in phrases:
        text, box = str(phrase.get("text") or ""), phrase.get("box")
        if not isinstance(box, Sequence) or len(box) != 4:
            continue
        kind = _kind(text)
        tags = sorted(set(_TAG.findall(text))) if kind == "note" else []
        if kind is None:
            continue
        where = next((d for d in drawings if _inside(box, d["box"])), outside)
        item = {"text": text, "box": [round(float(v), 1) for v in box]}
        if kind == "tag":
            where["tags"].append({**item, "text": text.strip(" .,;:()")})
        else:
            where["notes"].append(item)
            # A tag inside a note (`TOEKICK- WD-04`) is a tag the drawing prints too.
            where["tags"] += [{**item, "text": tag} for tag in tags]
    for drawing in (*drawings, outside):
        drawing["tags"] = drawing["tags"][:MAX_PER_DRAWING]
        drawing["notes"] = drawing["notes"][:MAX_PER_DRAWING]

    def printed(role: str) -> set[str]:
        return {tag["text"] for d in drawings if d["role"] == role for tag in d["tags"]}

    architect, vendor = printed("architect"), printed("vendor")
    both_sides = bool(architect) and any(d["role"] == "vendor" for d in drawings)
    return {
        "page": page,
        "drawings": drawings + ([outside] if outside["tags"] or outside["notes"] else []),
        "architect_only_tags": sorted(architect - vendor) if both_sides else [],
        "vendor_only_tags": sorted(vendor - architect) if both_sides else [],
    }


def set_notes(dims: Mapping[str, Any], page_map: Mapping[str, Any]) -> dict[str, Any]:
    """`notes.json` for a whole set, from its `dims.json` (the phrases) and `page_map.json`."""
    halves: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    for entry in page_map.get("pages") or []:
        if isinstance(entry, Mapping):
            key = (str(entry.get("file")), int(entry.get("page") or 0))
            halves[key] = [h for h in entry.get("halves") or [] if isinstance(h, Mapping)]
    pages: list[dict[str, Any]] = []
    for entry in dims.get("pages") or []:
        if not isinstance(entry, Mapping):
            continue
        key = (str(entry.get("file")), int(entry.get("page") or 0))
        found = page_notes(key[1], entry.get("phrases") or [], halves.get(key, []))
        pages.append({"file": key[0], **found})
    return {"version": 1, "pages": pages}


def notes_on(folder: Path | None, file: str, page: int) -> dict[str, Any] | None:
    """One page's entry of the set pack's `notes.json`, or `None`."""
    if folder is None:
        return None
    try:
        document = json.loads((folder / NOTES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for entry in document.get("pages") or [] if isinstance(document, Mapping) else []:
        if isinstance(entry, Mapping) and entry.get("file") == file and entry.get("page") == page:
            return dict(entry)
    return None
