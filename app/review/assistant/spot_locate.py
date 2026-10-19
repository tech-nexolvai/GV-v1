"""A value with no spot, found again at its printed label (#1738). Pure code: no PDF, no AI call.

GV Review's older stored page-review records (`source: gv_review`) carry a value but no spot on
the drawing: no box, no label id. Code then had nothing to check, and the classification called
the value "could not be read" (Illegible) although the set pack had already read it, as typed
text or as pen strokes (the drawn route). This module looks the value up among the page's
numbered labels (the set pack's `dims.json`, `dim_index.DimLabels`) and says where it is printed:

- **one spot**: exactly one label of that side on the page prints the value (the same inches; a
  value written in millimetres and inches must match both), or the same value on each round's copy of
  the area (the newest round's label then: the newest round governs);
- **several**: different labels print it and nothing says which: code never guesses (the AI
  names one by its id, `read_number`);
- **none**: no label of that side prints it (or the page's labels are not known).

`relocate` gives a seeded finding the one spot it found (its box and `dims` entry, so code's
checks read it there) and records what it searched in `spot_search`, which the classification
uses to say why a value still has no spot ("the value has no spot on the drawing").
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

from app.review.assistant.finding_checks import claim_value, to_inches
from units.normalise import plain_marks

__all__ = [
    "SPOT_SEARCH",
    "Located",
    "code_spotted",
    "locate",
    "relocate",
    "spot_words",
]

type Found = Literal["one", "several", "none"]

#: The finding field that records what code searched for each side's spot.
SPOT_SEARCH: Final = "spot_search"
#: Millimetres with the inches beside them in brackets: `940 [37]`.
_DUAL: Final = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:mm\.?)?\s*[\[(]\s*([^\])]+?)\s*[\])]\s*$")
#: Each side's box field on a finding.
_BOX: Final[Mapping[str, str]] = {"vendor": "vendor_box", "architect": "arch_box"}


@dataclass(frozen=True, slots=True)
class Located:
    """Where a value is printed on its page."""

    found: Found
    label: Mapping[str, Any] | None = None
    """The one label (`found == "one"`)."""
    candidates: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    """Every label that prints the value (several: the AI picks one by its id)."""
    why: str = ""
    """Plain words for how it was found, or why not."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "label": None if self.label is None else self.label.get("id"),
            "candidates": [str(c.get("id")) for c in self.candidates],
            "why": self.why,
        }


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _mm(text: object) -> Decimal | None:
    """The millimetres of a value written in both units (`794 [31 1/4]` -> 794), else None."""
    found = _DUAL.match(" ".join(plain_marks(str(text or "")).split()))
    return None if found is None else _decimal(found.group(1))


def _prints(label: Mapping[str, Any], want: Decimal, mm: Decimal | None) -> bool:
    """Whether `label` prints the claimed value: the same inches, and the same millimetres when
    the claim gives both."""
    if label.get("derived"):
        return False  # an equal part code worked out is not printed
    inches = _decimal(label.get("inches"))
    if inches is None or inches != want:
        return False
    if mm is None:
        return True
    printed = _decimal(label.get("mm")) if label.get("mm") is not None else _mm(label.get("text"))
    return printed is not None and printed == mm


def _round_of(label: Mapping[str, Any], halves: Sequence[Mapping[str, Any]]) -> int | None:
    """The round of the drawing half the label's centre is in (the page map's halves)."""
    try:
        x0, y0, x1, y1 = (float(v) for v in label.get("box") or ())
    except (TypeError, ValueError):
        return None
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    for half in halves:
        try:
            hx0, hy0, hx1, hy1 = (float(v) for v in half.get("box") or ())
        except (TypeError, ValueError):
            continue
        if hx0 <= cx <= hx1 and hy0 <= cy <= hy1:
            got = half.get("round")
            return got if isinstance(got, int) and not isinstance(got, bool) else None
    return None


def locate(
    labels: Sequence[Mapping[str, Any]] | None,
    side: str,
    claim: object,
    halves: Sequence[Mapping[str, Any]] = (),
) -> Located:
    """Where `claim` (a side's value as recorded) is printed among a page's `labels` of `side`.
    `halves`: the page map's drawing halves with their rounds (several spots, one per round,
    resolve to the newest round's)."""
    text = claim_value(claim)
    want = to_inches(text) if text is not None else None
    if want is None:
        return Located("none", why="the value is not one size code can look up")
    if labels is None:
        return Located("none", why="the page's dimension labels are not known")
    mm = _mm(text)
    found = tuple(
        label for label in labels if label.get("side") == side and _prints(label, want, mm)
    )
    if not found:
        return Located("none", why=f"no {side} label on the page prints {text}")
    if len(found) == 1:
        return Located("one", found[0], found, f"label {found[0].get('id')} prints {text}")
    rounds = [_round_of(label, halves) for label in found]
    if None not in rounds and len(set(rounds)) == len(rounds):
        newest = max(range(len(found)), key=lambda at: rounds[at] or 0)
        label = found[newest]
        return Located(
            "one",
            label,
            found,
            f"label {label.get('id')} prints {text} on the newest round ({rounds[newest]}); the "
            "same value is printed on each round's copy",
        )
    ids = ", ".join(str(label.get("id")) for label in found)
    return Located("several", None, found, f"{len(found)} {side} labels print {text} ({ids})")


def _box(label: Mapping[str, Any], file: str) -> dict[str, Any]:
    x0, y0, x1, y1 = (float(v) for v in label["box"])
    return {"page": int(label["page"]), "x0": x0, "y0": y0, "x1": x1, "y1": y1, "file": file}


def _provenance(label: Mapping[str, Any], file: str) -> dict[str, Any]:
    return {
        "id": label.get("id"),
        "page": label.get("page"),
        "file": file,
        "text": label.get("text"),
        "inches": label.get("inches"),
        "route": label.get("route"),
        "side": label.get("side"),
        "box": [float(v) for v in label.get("box") or ()],
    }


def relocate(
    finding: Mapping[str, Any],
    labels: Sequence[Mapping[str, Any]] | None,
    *,
    file: str,
    halves: Sequence[Mapping[str, Any]] = (),
    sides: Sequence[str] = ("vendor", "architect"),
) -> dict[str, Any]:
    """`finding` with each of `sides` that has no spot given the one label that prints its value
    (`locate`): the side's box (the label's) and its `dims` entry, so code's checks read the value
    there (`dim_findings.label_checks`). What was searched is in `spot_search`, by side. `labels`: the finding's page labels (`DimLabels.on_page`), each with
    its `page`."""
    out = dict(finding)
    search: dict[str, Any] = dict(out.get(SPOT_SEARCH) or {})
    dims: dict[str, Any] = dict(out.get("dims") or {})
    for side in sides:
        if out.get(_BOX[side]) or dims.get(side):
            continue
        found = locate(labels, side, out.get(side), halves)
        search[side] = found.as_dict()
        if found.label is None:
            continue
        label = {**found.label, "page": found.label.get("page") or out.get("page")}
        out[_BOX[side]] = _box(label, file)
        dims[side] = _provenance(label, file)
        if side == "architect" and label["page"] != out.get("page"):
            out["vs_page"] = label["page"]
    if dims:
        out["dims"] = dims
    if search:
        out[SPOT_SEARCH] = search
    return out


def code_spotted(finding: Mapping[str, Any]) -> bool:
    """Whether every spot `finding` has was found by `relocate` (code), not given by a reading:
    a recorded row still waiting for a reading of its own (merges onto it as before)."""
    search = finding.get(SPOT_SEARCH)
    search = search if isinstance(search, Mapping) else {}
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    for side, box in _BOX.items():
        if not finding.get(box):
            continue
        given = (dims or {}).get(side)
        searched = search.get(side)
        if not (
            isinstance(searched, Mapping)
            and searched.get("found") == "one"
            and isinstance(given, Mapping)
            and given.get("id") == searched.get("label")
        ):
            return False
    return True


def spot_words(finding: Mapping[str, Any], side: str = "vendor") -> str:
    """Why a side has no spot, in plain words, from what code searched (`spot_search`)."""
    search = finding.get(SPOT_SEARCH)
    searched = search.get(side) if isinstance(search, Mapping) else None
    if isinstance(searched, Mapping) and searched.get("found") == "several":
        ids = ", ".join(str(c) for c in searched.get("candidates") or [])
        return f"the value is printed at several spots ({ids}) and none was named"
    return "the value has no spot on the drawing"
