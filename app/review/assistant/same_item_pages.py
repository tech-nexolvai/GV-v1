"""The same item printed in two sizes on two of the vendor's pages (GV: "flag everything", #1906).

The reading ledger (`ledger.ReadingLedger`) groups what the AI read by item: when the AI names the
same item (the same tag or title) on two vendor pages with two different printed values for the
same span, the ledger makes a `cross_view` item. Until now it stayed Not compared ("code could not
prove they are the same span"), however plainly the two pages disagreed.

GV's rule (settled: exact match, flag everything): the vendor's own drawings must agree, so such an
item is a mistake on one of the two pages, and **code never guesses which**:

- **Code reads both labels** (`settle`): at each page's spot there is one of code's numbered labels
  (`dims.json`) read from the drawing's own text (never drawn ink), on the vendor's side, printing
  the value the AI recorded. Then the item is a Wrong (`status: different`) with both labels as its
  witnesses and an **undecided should-be**: `30" (page 12) or 32" (page 15): correct one so both
  pages agree` (`either_words`); every output (the screen, the drawing, the letter, the register)
  says so, never "should be 30"".
- **Code cannot read both** (no label at a spot, two different values there, a value read from
  drawn ink, a label of another side): a **Likely problem** ("Please check and confirm", dashed,
  never clouded: `likely_problems`).
- **Two rounds of one drawing** (the page map gives the two spots different vendor rounds): a
  revision, not a mistake; the item is left as the ledger made it.

Only sizes are judged here (the values read as inches); tags and models keep the ledger's item. The
same page twice (typical units, two views on one sheet) is left to the ledger and the vendor
consistency check, which proves a same-drawing span by its geometry.

Pure code over JSON (the pack's labels and page map); no AI call. Verification:
`tests/review/assistant/test_same_item_pages.py` (made-up labels).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from app.review.assistant import likely_problems
from app.review.assistant.finding_checks import to_inches

__all__ = [
    "FIELD",
    "either_words",
    "governs_words",
    "marked",
    "pages_of",
    "settle",
]

#: The finding's field this check writes: both pages' values, the reference page's first.
FIELD: Final = "same_item_pages"
#: Two printed values closer than this are the same (a sixteenth: the drawings' own step).
TOLERANCE: Final = Decimal(1) / Decimal(16)
#: A label stands at the AI's spot when its centre is inside the spot grown by this (points).
SLACK_PT: Final = 2.0
#: Who governs, as every output says it.
GOVERNS: Final = "the vendor's own drawings of the same item disagree"


def marked(finding: Mapping[str, Any]) -> bool:
    """A Wrong this check made: both pages' values read by code, the should-be undecided."""
    found = finding.get(FIELD)
    return isinstance(found, Mapping) and isinstance(found.get("pages"), list)


def pages_of(finding: Mapping[str, Any]) -> list[tuple[int, str]]:
    """(page, value as printed) of both spots, the reference page first."""
    found = finding.get(FIELD)
    if not isinstance(found, Mapping):
        return []
    out: list[tuple[int, str]] = []
    for one in found.get("pages") or []:
        if isinstance(one, Mapping) and isinstance(one.get("page"), int):
            out.append((int(one["page"]), " ".join(str(one.get("value") or "").split())))
    return out


def either_words(finding: Mapping[str, Any]) -> str | None:
    """`30" (page 12) or 32" (page 15): correct one so both pages agree`, else `None`."""
    pages = pages_of(finding)
    if len(pages) != 2 or not marked(finding):
        return None
    (first_page, first), (second_page, second) = pages
    return (
        f"{first} (page {first_page}) or {second} (page {second_page}): correct one so both "
        "pages agree"
    )


def governs_words(finding: Mapping[str, Any]) -> str | None:
    """ "the vendor's own drawings of the same item disagree (pages 12 and 15)", else `None`."""
    pages = pages_of(finding)
    if len(pages) != 2 or not marked(finding):
        return None
    return f"{GOVERNS} (pages {pages[0][0]} and {pages[1][0]})"


# ---- settling the ledger's items -------------------------------------------------------------


def _inches(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _spot(box: object, main_file: str | None) -> tuple[str, int, tuple[float, ...]] | None:
    if not isinstance(box, Mapping):
        return None
    try:
        page = int(box["page"])
        coords = tuple(float(box[k]) for k in ("x0", "y0", "x1", "y1"))
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in coords):
        return None
    name = str(box.get("file") or main_file or "")
    return (name, page, coords)


def _label_at(
    spot: tuple[str, int, tuple[float, ...]],
    value: object,
    pages: Mapping[tuple[str, int], Mapping[str, Any]],
) -> tuple[Mapping[str, Any] | None, str]:
    """The one vendor label code read from the drawing's own text at `spot`, printing `value`;
    else `None` and why (in plain words)."""
    wanted = to_inches(str(value or ""))
    if wanted is None:
        return None, "the value is not a size"
    name, page, (x0, y0, x1, y1) = spot
    entry = pages.get((name, page))
    if entry is None:
        return None, f"code has no printed numbers of page {page}"
    found: list[tuple[float, Mapping[str, Any]]] = []
    others = False
    for label in entry.get("labels") or []:
        if not isinstance(label, Mapping):
            continue
        box = label.get("box")
        if not isinstance(box, Sequence) or len(box) != 4:
            continue
        cx = (float(box[0]) + float(box[2])) / 2
        cy = (float(box[1]) + float(box[3])) / 2
        if not (x0 - SLACK_PT <= cx <= x1 + SLACK_PT and y0 - SLACK_PT <= cy <= y1 + SLACK_PT):
            continue
        inches = _inches(label.get("inches"))
        if inches is None or abs(inches - wanted) >= TOLERANCE:
            others = True
            continue
        if str(label.get("route") or "text") != "text":
            return None, f"page {page}'s value is read from drawn ink, not the drawing's text"
        if label.get("side") not in ("vendor", None):
            return None, f"page {page}'s label is not the vendor's"
        middle = ((x0 + x1) / 2, (y0 + y1) / 2)
        found.append((math.dist((cx, cy), middle), label))
    if not found:
        why = "another value is printed there" if others else "no printed number there"
        return None, f"code could not read {value} at page {page}'s spot ({why})"
    found.sort(key=lambda pair: pair[0])
    return found[0][1], ""


def _round_at(
    spot: tuple[str, int, tuple[float, ...]], rounds: Mapping[tuple[str, int], Sequence[Any]]
) -> int | None:
    """The vendor round of the drawing holding the spot (the page map), else `None`."""
    name, page, (x0, y0, x1, y1) = spot
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    for half in rounds.get((name, page)) or []:
        box = half.get("box") if isinstance(half, Mapping) else None
        if not isinstance(box, Sequence) or len(box) != 4:
            continue
        if float(box[0]) <= cx <= float(box[2]) and float(box[1]) <= cy <= float(box[3]):
            found = half.get("round")
            return int(found) if isinstance(found, int) and not isinstance(found, bool) else None
    return None


def _provenance(
    label: Mapping[str, Any], spot: tuple[str, int, tuple[float, ...]]
) -> dict[str, Any]:
    return {
        "id": str(label.get("id")),
        "page": spot[1],
        "file": spot[0],
        "text": " ".join(str(label.get("text") or "").split()),
        "inches": label.get("inches"),
        "route": label.get("route") or "text",
        "side": label.get("side") or "vendor",
        "box": [float(v) for v in label["box"]],
    }


def _likely(finding: Mapping[str, Any], why: str) -> dict[str, Any]:
    first, second = finding.get("architect"), finding.get("vendor")
    pages = (finding.get("vs_page"), finding.get("page"))
    words = (
        f"{likely_problems.LETTER_LEAD}: the same item is printed {first} on page {pages[0]} and "
        f"{second} on page {pages[1]} of the vendor's drawings."
    )
    return {
        **finding,
        likely_problems.FIELD: {
            "by": "reading_ledger",
            "proof": "likely_only",
            "kind": "size",
            "sure": "likely",
            "words": words,
            "why": why,
        },
    }


def settle(
    finding: Mapping[str, Any],
    dims: Mapping[str, Any] | None,
    page_map: Mapping[str, Any] | None = None,
    main_file: str | None = None,
) -> dict[str, Any]:
    """The ledger's item as this check leaves it (see the module): a Wrong with both labels and an
    undecided should-be, a Likely problem, or the item unchanged (not the vendor's two pages, not a
    size, two rounds of one drawing)."""
    if (
        finding.get("source") != "cross_view"
        or finding.get("reference_side") != "vendor"
        or finding.get("created_by") != "ledger"
        or finding.get("status") != "check"
    ):
        return dict(finding)
    first_spot = _spot(finding.get("arch_box"), main_file)
    second_spot = _spot(finding.get("vendor_box"), main_file)
    if first_spot is None or second_spot is None:
        return dict(finding)
    if first_spot[:2] == second_spot[:2]:
        return dict(finding)  # one page: the ledger and the consistency check speak for it
    first, second = finding.get("architect"), finding.get("vendor")
    if to_inches(str(first or "")) is None or to_inches(str(second or "")) is None:
        return dict(finding)  # a tag or a model: compared as words by the ledger
    rounds: dict[tuple[str, int], list[Any]] = {}
    for entry in (page_map or {}).get("pages") or []:
        if isinstance(entry, Mapping) and isinstance(entry.get("page"), int):
            rounds[(str(entry.get("file")), int(entry["page"]))] = [
                h
                for h in entry.get("halves") or []
                if isinstance(h, Mapping) and h.get("role") == "vendor"
            ]
    a_round, b_round = _round_at(first_spot, rounds), _round_at(second_spot, rounds)
    if a_round is not None and b_round is not None and a_round != b_round:
        return dict(finding)  # two rounds of one drawing: a revision, not a mistake
    pages: dict[tuple[str, int], Mapping[str, Any]] = {}
    for entry in (dims or {}).get("pages") or []:
        if isinstance(entry, Mapping) and isinstance(entry.get("page"), int):
            pages[(str(entry.get("file")), int(entry["page"]))] = entry
    first_label, first_why = _label_at(first_spot, first, pages)
    second_label, second_why = _label_at(second_spot, second, pages)
    if first_label is None or second_label is None:
        return _likely(finding, first_why or second_why)
    first_text = " ".join(str(first_label.get("text") or first).split())
    second_text = " ".join(str(second_label.get("text") or second).split())
    item = " ".join(str(finding.get("item") or "this item").split())
    why = (
        f"The vendor prints {item} as {first_text} on page {first_spot[1]} and {second_text} on "
        f"page {second_spot[1]}. Code read both printed values at their spots. One of them is "
        "wrong; which one is not known: correct one so both pages agree."
    )
    return {
        **finding,
        "status": "different",
        "architect": first_text,
        "vendor": second_text,
        "why": why,
        "dims": {
            "architect": _provenance(first_label, first_spot),
            "vendor": _provenance(second_label, second_spot),
        },
        "ledger_ids": list(finding.get("ledger_ids") or []),
        FIELD: {
            "pages": [
                {"page": first_spot[1], "value": first_text, "file": first_spot[0]},
                {"page": second_spot[1], "value": second_text, "file": second_spot[0]},
            ]
        },
    }
