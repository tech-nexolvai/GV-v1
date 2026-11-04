"""The vendor's item number that does not fit, as GV's please-check: $0, no AI (2026-11-04).

A vendor sheet may print the wrong item number in its title block: another counter's number, which
the architect's plan prints in another room. Pairing used to follow that number and end unsure
between the wrong counter's page and a page the AI matcher liked, so the whole check stayed
incomplete. The work list now checks the number against three printed things
(`worklist._misleading_tag`): the room the sheet's title names, the vendor's own other sheet of the
same item, and where the architect's plan prints each number. When they show the number does not
fit, pairing goes by the other number, and this module adds the wrong number itself as a finding:

- one per vendor sheet, on its page, boxed at the number as the sheet prints it, and at the other
  number where the architect's plan prints it in the room (when code found it there);
- always a please-check (the whole-drawing look's mark, `likely_only`): which number the architect
  means for the piece is code's reading of its plan's rooms, not a value both sides print;
- not added when the answer already says it (a finding on the same page naming both numbers).

Pure code over the set pack (`worklist.build_worklist`); the engine's hook adds the findings
through the finding book like the other code checks.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from importlib import import_module
from pathlib import Path
from typing import Any, Final

from app.review.assistant.simple_check import views as views_module
from app.review.assistant.simple_check.schema import AcrossPair, Box, WrongTag

__all__ = ["CREATED_BY", "add_to_answer", "check_pack", "findings_from"]

_log = logging.getLogger(__name__)

#: Who made the findings (the chat finding's `created_by`).
CREATED_BY: Final = "pairing_tag"
_NOT_CODE: Final = re.compile(r"[^A-Z0-9]")


def _code(text: object) -> str:
    """An item number for comparing: letters and digits only (`XX-512` is `XX512`)."""
    return _NOT_CODE.sub("", str(text or "").upper())


def _box_dict(box: Box, page: int, file: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "page": page,
        "x0": round(box[0], 1),
        "y0": round(box[1], 1),
        "x1": round(box[2], 1),
        "y1": round(box[3], 1),
    }
    if file:
        out["file"] = file
    return out


def _printed_at(phrases: Sequence[views_module.Phrase], printed: str) -> Box | None:
    """Where a page prints an item number (the first phrase holding it whole)."""
    wanted = _code(printed)
    for text, box in phrases:
        words = [_code(w) for w in re.split(r"[\s,;:()]+", str(text))]
        if wanted in words or _code(text) == wanted:
            return box
    return None


def findings_from(
    pairs: Sequence[AcrossPair], phrases: Mapping[tuple[str, int], Sequence[views_module.Phrase]]
) -> list[dict[str, Any]]:
    """GV's please-check for each vendor drawing whose item number does not fit
    (`AcrossPair.wrong_tag`). `phrases`: each page's printed phrases by file and page (its own),
    to box the number where the vendor's sheet prints it."""
    from app.review.assistant import likely_problems

    out: list[dict[str, Any]] = []
    for pair in pairs:
        wrong: WrongTag | None = pair.wrong_tag
        if wrong is None:
            continue
        vendor = pair.vendor
        page = vendor.own_page
        pages = ", ".join(str(p) for p in wrong.other_pages)
        where = (
            f"the architect's plan (page {wrong.tag_page}) prints {wrong.printed} near the "
            f"{' and the '.join(wrong.tag_rooms)}"
            if wrong.tag_rooms and wrong.tag_page is not None
            else f"the vendor prints {wrong.printed} on its page "
            f"{', '.join(str(p) for p in wrong.elsewhere)} of another item"
        )
        if wrong.confirmed and wrong.other:
            words = (
                f"the item number {wrong.printed} on this sheet looks wrong. This sheet is for the "
                f"{wrong.room}; {where}, and prints {wrong.other} in the {wrong.room} (page "
                f"{wrong.arch_page}). The vendor's own sheet of the same item (page {pages}) "
                f"prints {wrong.other}. Correct the item number to {wrong.other}, or say why it "
                "differs."
            )
            architect = wrong.other
        else:
            words = (
                f"the vendor's sheets of one item print two item numbers: {wrong.printed} here and "
                f"{wrong.other} on page {pages}. This sheet is for the {wrong.room}; {where}, and "
                f"code does not find {wrong.other} in the {wrong.room}. Confirm which number is "
                "the item's."
            )
            architect = "n/a"
        why = words[0].upper() + words[1:]
        spot = _printed_at(phrases.get((vendor.file, page), ()), wrong.printed)
        finding: dict[str, Any] = {
            "page": page,
            "item": f"Item number {wrong.printed}",
            "category": "other",
            "vendor": wrong.printed,
            "architect": architect,
            "status": "check",
            "why": why[:600],
            "verified": False,
            "source": "architect",
            "paired_by": "code",
            "created_by": CREATED_BY,
            likely_problems.FIELD: {
                "by": "whole_drawing_look",
                "proof": "likely_only",
                "kind": "labels",
                "claim": "please_check",
                "sure": "likely",
                "why": words[:400],
                "words": f"{likely_problems.LETTER_LEAD}: {words}"[:600],
            },
        }
        if spot is not None:
            finding["vendor_box"] = _box_dict(spot, page, vendor.file)
        if wrong.arch_box is not None and wrong.arch_page is not None:
            finding["vs_page"] = wrong.arch_page
            finding["arch_box"] = _box_dict(wrong.arch_box, wrong.arch_page, wrong.arch_file)
        out.append(finding)
    return out


def check_pack(folder: Path | None, main_file: str | None) -> list[dict[str, Any]]:
    """The findings over a set pack's work list (`worklist.build_worklist`): none when it has no
    page map or no vendor drawing paired across the uploads."""
    if folder is None:
        return []
    # Loaded by name: the work list reaches the drawing readers, which the API never may.
    worklist = import_module("app.review.assistant.simple_check.worklist")
    try:
        work = worklist.build_worklist(Path(folder), main_file)
    except FileNotFoundError:
        return []
    pairs = [pair for pair in work.across if pair.wrong_tag is not None]
    if not pairs:
        return []
    dims = worklist._read(Path(folder) / "dims.json")
    pages = {
        key: views_module.phrases_of(entry)
        for key, entry in worklist._by_page(dims).items()
        if any((p.vendor.file, p.vendor.own_page) == key for p in pairs)
    }
    return findings_from(pairs, pages)


def _already(finding: Mapping[str, Any], live: Sequence[Mapping[str, Any]]) -> bool:
    """Whether the answer already says it: a finding on the same page naming both numbers (a page
    reviewer's item-number card), or this check's own."""
    numbers = {_code(finding.get("vendor")), _code(finding.get("architect"))} - {"", "NA"}
    for other in live:
        if other.get("page") != finding.get("page"):
            continue
        if other.get("created_by") == CREATED_BY and other.get("item") == finding.get("item"):
            return True
        said = _code(
            " ".join(str(other.get(k) or "") for k in ("item", "why", "vendor", "architect"))
        )
        if len(numbers) == 2 and all(number in said for number in numbers):
            return True
    return False


def add_to_answer(engine: Any) -> int:
    """The engine's hook (a full check, at its finish, $0): each wrong item number added through
    the finding book (its checks and trust rule), unless the answer already says it; the
    please-check's mark laid on after. Returns how many were added."""
    if getattr(engine.settings, "mode", None) != "full_check":
        return 0
    from app.review.assistant import likely_problems

    book = engine.book
    made = check_pack(
        engine.tools.workspace.set_pack, str(book.main_file) if book.main_file else None
    )
    added = 0
    with engine.finding_lock:
        live = book.live()
        for finding in made:
            if _already(finding, live):
                continue
            mark = finding.pop(likely_problems.FIELD, None)
            stored = book.add_code_finding(finding, created_by=CREATED_BY)
            if stored is None:
                continue
            laid = {**stored, **({likely_problems.FIELD: mark} if mark else {})}
            if book.replace(laid):
                engine._show_finding(laid)
                live.append(laid)
                added += 1
    if added:
        _log.info("wrong item number: %d please-checks added", added)
    return added
