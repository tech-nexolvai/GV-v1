"""One reference page per finding: the architect (file, page) its pair gives (stream V, 2026-10-23).

A full check of two separate uploads pairs each vendor page with its architect pages (code and the
AI page matcher agree, `full_check_plan.CrossPairs`). Before this, each picture, caption, "Whole
page" view and code check picked its architect page by itself: the drawer took page N of the FIRST
architect file, a caption fell back to the vendor's page number, and the spot guard looked for an
architect value on the vendor's page number in the vendor's file. On a set with two architect files
(or any unpaired page) that shows, or checks, a page the comparison never used.

`stamp` sets `finding["reference_page"]` once, at the check's finish, from the pair alone:

- `{"file", "page", "why": None}`: the architect page the item is compared on. Its architect spot's
  page when the spot is on the pair; else the page it names (`vs_page`) when that is on the pair;
  else the pair's governing (first) page.
- `{"file": None, "page": None, "why": ...}`: no architect page, with the true reason (the vendor
  page is not paired; the vendor's own drawings; GV's rule). Nothing guesses a page then.
- `"outside": True` with it: the item's architect spot or `vs_page` is on a page the pair does not
  hold (`spot_guard` keeps such an item from the vendor: it was not compared on its pair).
- `"pair"`: every (file, page) the item may be compared on (its pair, then its brand prototype
  drawings): `spot_guard` looks for an architect value on each of them.

An item checked within the vendor's own drawings is never "outside": its boxes are on the vendor's
own drawings by design. An unpaired page's own spot (code's run-by-run pairing) is its page.

A page the cross-file pairing did not decide (a one-file set, GV's combined sheets, a page with no
drawing) gets no stamp: its architect page is the sheet's own, as before.

Pure code: no PDF, no AI. Verification: `tests/review/assistant/test_reference_page.py`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

__all__ = ["KEY", "Pairs", "allowed", "outside_words", "stamp", "vendor_only", "vendor_page"]

#: The finding field.
KEY: Final = "reference_page"

#: Vendor page -> the architect (file, page)s paired with it.
type Pairs = Mapping[int, Sequence[tuple[str, int]]]

#: Items compared within the vendor's own drawings: no architect page.
_VENDOR_ONLY_SOURCES: Final = frozenset({"vendor_arithmetic", "cross_view"})
_VENDOR_ONLY_MAKERS: Final = frozenset({"consistency"})
_BLOCK: Final = 1000


def _own(page: int) -> int:
    """The work list's architect page number (`p1012` = 1012) as its file's own (12)."""
    return (page - 1) % _BLOCK + 1 if page > _BLOCK else page


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def vendor_page(finding: Mapping[str, Any]) -> int | None:
    """The vendor page the item is on (its vendor spot's page, else its page)."""
    box = finding.get("vendor_box")
    page = _int(box.get("page")) if isinstance(box, Mapping) else None
    return page or _int(finding.get("page"))


def allowed(
    page: int | None, pairs: Pairs, references: Pairs | None = None
) -> tuple[tuple[str, int], ...]:
    """The architect (file, page)s an item on vendor `page` may be compared on: its pair, then its
    brand prototype drawings."""
    if page is None:
        return ()
    out = [(str(f), int(p)) for f, p in pairs.get(page, ())]
    for where in (references or {}).get(page, ()):
        if (str(where[0]), int(where[1])) not in out:
            out.append((str(where[0]), int(where[1])))
    return tuple(out)


def _names(pair: Sequence[tuple[str, int]]) -> str:
    return ", ".join(f"page {p} of {f}" for f, p in pair)


def outside_words(page: int, spot: tuple[str | None, int], pair: Sequence[tuple[str, int]]) -> str:
    """Why a spot is refused: vendor `page` is paired with `pair`, the spot is not on it."""
    where = f"page {spot[1]}" + (f" of {spot[0]}" if spot[0] else "")
    return (
        f"vendor page {page} is paired with the architect's {_names(pair)}; {where} is not one "
        "of them. Put the architect's spot on its paired page, or leave the architect spot out."
    )


def vendor_only(finding: Mapping[str, Any]) -> bool:
    """An item checked within the vendor's own drawings (a string that does not add up, a span
    printed twice, two views of one side): its boxes are on the vendor's (or one side's) own
    drawings, and no architect page is compared."""
    return str(finding.get("source") or "") in _VENDOR_ONLY_SOURCES or (
        str(finding.get("created_by") or "") in _VENDOR_ONLY_MAKERS
    )


def stamp(
    finding: Mapping[str, Any],
    pairs: Pairs,
    *,
    references: Pairs | None = None,
    unpaired: Mapping[int, str] | None = None,
) -> dict[str, Any]:
    """`finding` with its `reference_page` (see the module), set from `pairs` alone."""
    out = dict(finding)
    record: dict[str, Any] = {"file": None, "page": None, "why": None, "outside": False}
    if vendor_only(finding):
        record["why"] = "Checked within the vendor's own drawings: no architect page is compared."
        out[KEY] = record
        return out
    page = vendor_page(finding)
    if page is None or (page not in pairs and page not in (unpaired or {})):
        # Not a page the cross-file pairing decided (a combined sheet, no drawing): as before.
        return out
    pair = tuple((str(f), int(p)) for f, p in pairs.get(page, ()))
    may = allowed(page, pairs, references)
    record["pair"] = [{"file": f, "page": p} for f, p in may]
    box = finding.get("arch_box") if isinstance(finding.get("arch_box"), Mapping) else None
    box_page = _int(box.get("page")) if box else None
    box_file = str(box["file"]) if box and box.get("file") else None
    vs_page = _int(finding.get("vs_page"))
    vs_page = _own(vs_page) if vs_page else None
    if box is not None and box_page is not None:
        spot = (box_file, _own(box_page))
        if any(spot == where or (box_file is None and spot[1] == where[1]) for where in may):
            file = box_file or next(f for f, p in may if p == spot[1])
            record.update(file=file, page=spot[1])
        elif not pair:
            # An unpaired page whose item has its own spot (code's run-by-run pairing, #1883):
            # the spot is the item's page; there is no pair to hold it against.
            record.update(file=box_file, page=spot[1])
        else:
            record.update(file=pair[0][0], page=pair[0][1], outside=True)
            record["why"] = (
                "The architect's spot is not on the page paired with this vendor page: "
                + outside_words(page, spot, pair)
            )
        out[KEY] = record
        return out
    if str(finding.get("source") or "") == "rules" or finding.get("rule_check_id"):
        record["why"] = "GV's rule: no architect page is compared."
        out[KEY] = record
        return out
    if vs_page is not None and pair:
        named = [where for where in may if where[1] == vs_page]
        if named:
            record.update(file=named[0][0], page=named[0][1])
        else:
            record.update(file=pair[0][0], page=pair[0][1], outside=True)
            record["why"] = (
                f"The item names architect page {vs_page}, which is not the page paired with "
                f"vendor page {page} ({_names(pair)})."
            )
        out[KEY] = record
        return out
    if pair:
        record.update(file=pair[0][0], page=pair[0][1])
        out[KEY] = record
        return out
    # The pairing's own reason is not repeated: it can carry the page matcher's private names
    # for pages ("A5", "p1009"), which are no page a person can find.
    record["why"] = f"No architect page is paired with vendor page {page}."
    out[KEY] = record
    return out
