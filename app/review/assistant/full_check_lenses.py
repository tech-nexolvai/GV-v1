"""The two helpers of a page group, merged by code (#1569).

A full check can give each page group two helpers (`full_check_plan.with_lenses`): one for the
NUMBERS (sizes and strings) and one for the DRAWN THINGS (notes, doors and drawers, hardware,
extent, the brand prototype, finish tags). Their briefs do not overlap, but both may still name the
same item on the same page ("Kitchenette: bin drawer fronts"). Code keeps one finding per item and
page:

- both say the same status: the numbers helper's finding stays, the other is folded into it;
- they disagree (one Wrong, the other Right): the stronger status stays (Wrong over Your decision
  over Right) so nothing is lost, and the lead is asked to verify it first.

Two findings of different rounds are never the same item. Pure code: no I/O, no AI call.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

__all__ = ["Fold", "SameWrong", "item_key", "lens_folds", "same_wrongs"]

_WORD: Final = re.compile(r"[a-z0-9]+(?:/[0-9]+)?")
_FILLER: Final = frozenset({"the", "a", "an", "of", "on", "at", "in", "for", "to"})
#: The stronger status wins a disagreement.
_STRENGTH: Final = {"different": 0, "check": 1, "same": 2}


@dataclass(frozen=True, slots=True)
class Fold:
    """One duplicate: `drop` is folded into `keep`."""

    keep: str
    drop: str
    page: int
    item: str
    disagree: bool
    statuses: tuple[str, str]
    """(the numbers helper's status, the drawn-things helper's)."""

    @property
    def reason(self) -> str:
        if not self.disagree:
            return (
                f"Folded into {self.keep}: both helpers of this page reported it (the numbers and "
                "the drawn things)."
            )
        return (
            f"Folded into {self.keep}: the two helpers of this page disagree ({self.statuses[0]} "
            f"vs {self.statuses[1]}); the stronger one is kept for the lead to verify."
        )


def item_key(item: object) -> tuple[str, ...]:
    """An item's words for matching: lower case, letters and numbers only, filler words left out
    ("Kitchenette: the bin drawer fronts" and "kitchenette - bin drawer fronts" match)."""
    words = _WORD.findall(str(item or "").lower())
    return tuple(word for word in words if word not in _FILLER)


def _same_round(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return a.get("round") is None or b.get("round") is None or a.get("round") == b.get("round")


def lens_folds(
    numbers: Sequence[Mapping[str, Any]], drawn: Sequence[Mapping[str, Any]]
) -> list[Fold]:
    """The duplicates between one page group's two helpers, matched by page and item (each
    finding at most once), in the drawn-things helper's order."""
    folds: list[Fold] = []
    used: set[str] = set()
    for other in drawn:
        key = item_key(other.get("item"))
        if not key:
            continue
        match = next(
            (
                found
                for found in numbers
                if str(found.get("id")) not in used
                and found.get("page") == other.get("page")
                and item_key(found.get("item")) == key
                and _same_round(found, other)
            ),
            None,
        )
        if match is None:
            continue
        used.add(str(match.get("id")))
        mine, theirs = str(match.get("status")), str(other.get("status"))
        keep, drop = match, other
        if _STRENGTH.get(theirs, 1) < _STRENGTH.get(mine, 1):
            keep, drop = other, match
        folds.append(
            Fold(
                keep=str(keep.get("id")),
                drop=str(drop.get("id")),
                page=int(match.get("page") or 0),
                item=str(match.get("item") or ""),
                disagree=mine != theirs,
                statuses=(mine, theirs),
            )
        )
    return folds


# ---- the same Wrong item reported again (#1622) -------------------------------------------------


@dataclass(frozen=True, slots=True)
class SameWrong:
    """A Wrong finding that repeats another: `drop` is folded into `keep` (one comment)."""

    keep: str
    drop: str

    @property
    def reason(self) -> str:
        return (
            f"Folded into {self.keep}: the same item reported again (same page, the same vendor "
            "value and the same value it should be, at the same spot)."
        )


_TRUST_ORDER: Final = {"confirmed": 0, "likely": 1, "to_check": 2}


def _value(finding: Mapping[str, Any], side: str) -> str:
    inches = finding.get("inches")
    given = inches.get(side) if isinstance(inches, Mapping) else None
    if given not in (None, ""):
        return f"in:{given}"
    return " ".join(str(finding.get(side) or "").lower().split())


def _vendor_box(finding: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    box = finding.get("vendor_box")
    if not isinstance(box, Mapping):
        return None
    try:
        return (float(box["x0"]), float(box["y0"]), float(box["x1"]), float(box["y1"]))
    except (KeyError, TypeError, ValueError):
        return None


def _label(finding: Mapping[str, Any]) -> str | None:
    dims = finding.get("dims")
    vendor = dims.get("vendor") if isinstance(dims, Mapping) else None
    return str(vendor["id"]) if isinstance(vendor, Mapping) and vendor.get("id") else None


def _same_spot(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """The same vendor dimension label (code's own id for the printed dimension), else overlapping
    vendor boxes on the same file for the same item words (two items read at one spot by an AI box
    stay two)."""
    label_a, label_b = _label(a), _label(b)
    if label_a and label_b:
        return label_a == label_b
    if item_key(a.get("item")) != item_key(b.get("item")):
        return False
    box_a, box_b = _vendor_box(a), _vendor_box(b)
    if box_a is None or box_b is None:
        return False
    file_a = (a.get("vendor_box") or {}).get("file")
    file_b = (b.get("vendor_box") or {}).get("file")
    if file_a and file_b and file_a != file_b:
        return False
    return not (
        box_a[2] < box_b[0] or box_b[2] < box_a[0] or box_a[3] < box_b[1] or box_b[3] < box_a[1]
    )


def _number(finding_id: str) -> tuple[int, str]:
    digits = "".join(ch for ch in finding_id if ch.isdigit())
    return (int(digits) if digits else 0, finding_id)


def same_wrongs(findings: Sequence[Mapping[str, Any]]) -> list[SameWrong]:
    """Wrong findings that are the same item: the same page and round, the same vendor value, the
    same value it should be, and the same vendor dimension label (or overlapping vendor boxes and the
    same item words). Each
    group is folded into one finding: a recorded difference first (it is never folded away), then
    one code paired by itself (`pair_findings`: its pairing proof is kept), then the best trust,
    then the one reported first. So the letter, the register, the clouds and the
    counts never repeat an item (the resolver's same-item fold, #1554, settles only "Your decision"
    items and never a Wrong one)."""
    wrong = [f for f in findings if f.get("status") == "different" and f.get("id")]
    folds: list[SameWrong] = []
    gone: set[str] = set()
    for at, finding in enumerate(wrong):
        if str(finding["id"]) in gone:
            continue
        group = [finding] + [
            other
            for other in wrong[at + 1 :]
            if str(other["id"]) not in gone
            and other.get("page") == finding.get("page")
            and _same_round(other, finding)
            and _value(other, "vendor") == _value(finding, "vendor")
            and _value(other, "architect") == _value(finding, "architect")
            and _same_spot(other, finding)
        ]
        if len(group) < 2:
            continue
        keep = min(
            group,
            key=lambda f: (
                0 if f.get("record_id") else 1,
                0 if f.get("paired_by") == "code" else 1,  # code's own pair keeps its proof
                _TRUST_ORDER.get(str(f.get("trust_level") or ""), 3),
                _number(str(f["id"])),
            ),
        )
        for other in group:
            if other is keep or other.get("record_id"):
                continue
            gone.add(str(other["id"]))
            folds.append(SameWrong(keep=str(keep["id"]), drop=str(other["id"])))
    return folds
