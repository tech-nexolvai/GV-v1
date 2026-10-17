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

__all__ = ["Fold", "item_key", "lens_folds"]

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
