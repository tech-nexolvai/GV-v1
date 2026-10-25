"""Code's proof that a vendor and an architect dimension run between the same two points: the
three geometry proofs in one place, $0, no AI (final plan #3 and #4, 2026-10-25).

A red that compares a vendor size with a reference size needs code to confirm both dimensions end
at the same places (option C, `assistant_pairing_proof = code_ends`). In order:

1. **The anchor fit** (`full_check_advocate.same_extent`): the drawings lined up on values printed
   once on each. Different ends there block at once.
2. **Each value against its drawn length** (`scale_fit`, final plan #3): a value that does not
   agree with its line at its view's own scale ("the vendor's 23" is drawn 24" long") may be
   typed over, misread or not to scale. It never feeds a red, whatever else agrees: `cannot_tell`,
   with those words.
3. **The drawn run** (`same_ends`): ties on both drawings (the floor, the walls, an outlet, a
   neighbour printed the same) and nothing else drawn near a moved end.
4. **What both ends touch** (`dim_ends.same_kinds`, final plan #4): both ends of both dimensions on
   the same kinds of points (a wall's face, a panel's face, a sink cut-out's edge...), each drawn
   to its value, and no other dimension on either drawing between such points. A top's edge
   against a cabinet's side is "one measures the countertop, the other the cabinets": different.

The first proof that is not `cannot_tell` stands (but a drawn-length disagreement overrides
`same_extent`). Pure: the caller passes the labels, the line work and the pages read for their
ends. Verification: `tests/review/assistant/test_ends_proof.py`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from app.review.assistant import dim_ends, same_ends
from app.review.assistant.full_check_advocate import Extent, same_extent

__all__ = ["code_extent", "drawn_disagreement"]

_log = logging.getLogger(__name__)

type PageOf = Callable[[str, int], dim_ends.Page | None]


def _page(pages: PageOf | None, label: Mapping[str, Any]) -> dim_ends.Page | None:
    if pages is None:
        return None
    try:
        return pages(str(label.get("file")), int(label.get("page") or 0))
    except Exception:  # noqa: BLE001 - no page read: that proof says nothing
        _log.exception("ends proof: page not read for %s", label.get("id"))
        return None


def drawn_disagreement(
    vendor: Mapping[str, Any], architect: Mapping[str, Any], pages: PageOf | None
) -> str | None:
    """The words when either value does not agree with its drawn length at its view's fit
    (`scale_fit`): such a value never feeds a red. `None` when both agree or code cannot tell."""
    for label, whose in ((vendor, "the vendor's"), (architect, "the reference's")):
        said = dim_ends.drawn_words(_page(pages, label), label.get("id"), whose)
        if said is not None:
            return f"{said}: its number does not agree with its drawing"
    return None


def code_extent(
    vendor: Mapping[str, Any],
    architect: Mapping[str, Any],
    *,
    labels_of: same_ends.LabelsOf,
    segments_of: same_ends.SegmentsOf,
    page_map: Mapping[str, Any] | None,
    pages: PageOf | None,
    cache: dict[Any, same_ends.Side | None] | None = None,
) -> tuple[Extent, str]:
    """Code's geometry for a vendor and an architect label (see the module)."""

    def side(label: Mapping[str, Any], wanted: str) -> list[dict[str, Any]]:
        found = labels_of(str(label.get("file")), int(label.get("page") or 0)) or []
        return [dict(item) for item in found if item.get("side") == wanted]

    state, why = same_extent(
        vendor, architect, side(vendor, "vendor"), side(architect, "architect")
    )
    if state == "different_extent":
        return state, why
    drawn = drawn_disagreement(vendor, architect, pages)
    if drawn is not None:
        return "cannot_tell", drawn
    if state == "same_extent":
        return state, why
    try:
        proved, said = same_ends.prove_labels(
            vendor,
            architect,
            labels_of=labels_of,
            segments_of=segments_of,
            page_map=page_map,
            cache=cache,
        )
    except Exception:  # noqa: BLE001 - the drawn run is extra proof; the anchor fit stands
        _log.exception("ends proof: same-ends proof failed")
        proved, said = "cannot_tell", ""
    if proved != "cannot_tell":
        return proved, f"the drawn run: {said}"
    v_page, a_page = _page(pages, vendor), _page(pages, architect)
    v_ends = v_page.ends(vendor.get("id")) if v_page is not None else None
    a_ends = a_page.ends(architect.get("id")) if a_page is not None else None
    if v_page is None or a_page is None or v_ends is None or a_ends is None:
        return state, why
    try:
        kinds, words = dim_ends.same_kinds(v_page, v_ends, a_page, a_ends)
    except Exception:  # noqa: BLE001 - the ends' kinds are extra proof
        _log.exception("ends proof: kinds proof failed")
        return state, why
    if kinds != "cannot_tell":
        return kinds, f"the ends: {words}"
    return state, f"{why}; the ends: {words}"


def labels_reader(
    labels: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
) -> same_ends.LabelsOf:
    """`labels_of` over a set pack's labels by (file, page) (`escalation.EscalationContext.labels`),
    each label carrying its file and page."""

    def read(file: str, page: int) -> Sequence[Mapping[str, Any]]:
        return [
            {"file": file, "page": page, **dict(label)} for label in labels.get((file, page), ())
        ]

    return read
