"""Where a dimension's two ends land, as an unordered pair (2026-10-27).

A blind pick answers each dimension's ends as `{"from": ..., "to": ...}`, and its schema gives the
two no order ("where one end lands", "where the other end lands"). So `{wall_face, centre_line}`
and `{centre_line, wall_face}` are the same two ends. Comparing the two objects as they are made
the same ends written in the other order read as "the ends land in different places", which threw
away a pairing the picks had agreed on (a real upload: a width that should have been a
please-check went Not compared). Every place that compares picks' ends uses these helpers instead.

Under the live setting (`code_ends`, option C) this adds no red: a red still needs code's own
geometry to prove the same ends (`full_check_advocate.decide`); it only stops a word-order slip
from blocking an item, which then shows as a please-check ("ends not confirmed").
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

__all__ = ["end_pair", "ends_apart", "same_ends"]

_ENDS = ("from", "to")


def end_pair(spans: object) -> tuple[str, str] | None:
    """The two ends of one `{from, to}` answer, sorted so the order they were written in does
    not count; `None` for anything that is not such an answer."""
    if not isinstance(spans, Mapping):
        return None
    first, second = (str(spans.get(end)) for end in _ENDS)
    return (first, second) if first <= second else (second, first)


def same_ends(one: object, other: object) -> bool:
    """`True` when the two answers name the same two ends, in either order. Anything that is not
    a `{from, to}` answer compares as before (by plain equality)."""
    first, second = end_pair(one), end_pair(other)
    if first is None or second is None:
        return one == other
    return first == second


def ends_apart(vendor: object, architect: object) -> tuple[str, str] | None:
    """The end each side names that the other does not (vendor's, architect's), checking the
    `to` end first, as the wording always did; `None` when they name the same two ends or either
    is not a `{from, to}` answer."""
    if not isinstance(vendor, Mapping) or not isinstance(architect, Mapping):
        return None
    if same_ends(vendor, architect):
        return None
    ours = [str(vendor.get(end)) for end in ("to", "from")]
    theirs = [str(architect.get(end)) for end in ("to", "from")]
    only_ours = Counter(ours) - Counter(theirs)
    only_theirs = Counter(theirs) - Counter(ours)
    v = next(end for end in ours if only_ours[end])
    a = next(end for end in theirs if only_theirs[end])
    return v, a
