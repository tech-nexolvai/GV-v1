"""Which architect dimension measures the same span as which vendor dimension: paired by where
they are drawn, never by value. Code, $0, no AI.

A difference between the architect's size and the vendor's is a code-proved Wrong only when code
knows the two numbers measure the same thing. `extraction/architect/pairing.py` already pairs that
way: each row's ticks go to real inches through its own view's scale, the architect's row slides
along the vendor's, and a span pairs only when both its ends land on vendor ticks; it never reads
the printed values. This module feeds it from the set pack and compares what it pairs:

1. **Rows** from `dims.json`: one drawing's labels on one dimension line (same side, same
   direction, the line within `LINE_PT`), contiguous end to end, at least two. A label on a
   parallel line whose span covers the row end to end is its overall.
2. **Only geometry code can trust**: every label of a row must be drawn to its value at its
   drawing's measured scale (`view_scales`: within 5%) and that scale must be known. A row with a
   dimension typed over is never paired: its ticks are not where its numbers say.
3. **Ends on drawn work**: an architect span counts as measuring drawn work (`on_outline`) only
   when code found an extension line at both its ends; otherwise `pairing.py` never pairs it.
4. **Compared exactly**: each pair's architect inches against the vendor piece's (or the sum of
   the pieces it spans): `same` within 1/32", else `different`, both values the drawing's own text
   (or drawn ink, which is weaker: #756 D2) and the pairing derived by code.

A `different` pair with both values printed as text is a code-proved Wrong: red = the vendor's
value, green = the architect's. Everything code cannot pair is said with `pairing.py`'s reason.

`pairs.json` holds `pairs` (the one-to-one pairs, in the shape the full check reads:
`full_check_advocate.code_pairs`, where a pair code made is proof by itself) and `pages` (every
pair and every reason, per page).

Pure: no PDF, no model. Verification: `tests/review/assistant/test_dim_pairs.py`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

from app.review.assistant import view_scales
from extraction.architect.pairing import (
    DrawnRow,
    DrawnSpan,
    PairingSettings,
    PairingStatus,
    pair_rows,
)

__all__ = ["PAIRS_FILE", "SETTINGS", "flat_pairs", "page_pairs", "pairs_on", "set_pairs"]

PAIRS_FILE: Final = "pairs.json"
#: Labels whose dimension lines are this close (points, across the line) are on one row.
LINE_PT: Final = 1.5
#: Two spans are contiguous when one ends this close (points) to where the next starts.
JOIN_PT: Final = 1.0
#: An overall's line is at most this far (points) from its row's.
OVERALL_PT: Final = 60.0
#: Two printed values are the same within this (inches).
SAME_IN: Final = Fraction(1, 32)
#: `pairing.py`'s thresholds as the old pipeline measured them (`workflow/architect_pairing.py`).
SETTINGS: Final = PairingSettings(
    tick_tolerance_in=Fraction(1, 2),
    tolerance_fraction_of_smallest_bay=Fraction(1, 3),
    minimum_coincident_ticks=3,
    minimum_support_margin=2,
)

type Label = Mapping[str, Any]


def _inches(label: Label) -> Fraction | None:
    try:
        return Fraction(Decimal(str(label.get("inches"))))
    except (InvalidOperation, ValueError):
        return None


def _along(label: Label) -> tuple[float, float, float] | None:
    """A label's dimension line as (line position across, start, end) along its direction."""
    line = label.get("dim_line")
    if not line or label.get("orientation") not in ("h", "v"):
        return None
    (ax, ay), (bx, by) = line
    if label["orientation"] == "h":
        return (ay + by) / 2, min(ax, bx), max(ax, bx)
    return (ax + bx) / 2, min(ay, by), max(ay, by)


def _span(label: Label, start: float, end: float, *, architect: bool) -> DrawnSpan:
    ends = label.get("extension") or []
    # `pairing.py` pairs an architect span only when its ends sit on drawn work: here, when code
    # found an extension line at both ends. Never known for the vendor's (it does not ask).
    on_drawn_work = True if architect and len(ends) == 2 and all(ends) else None
    return DrawnSpan(
        Decimal(str(round(start, 3))),
        Decimal(str(round(end, 3))),
        on_drawn_work,
        str(label.get("id")),
    )


def _rows(
    labels: Sequence[Label],
    side: str,
    orientation: str,
    scales: Sequence[view_scales.ViewScale],
    trusted: set[str],
) -> tuple[list[tuple[DrawnRow, list[Label], Label | None]], list[str]]:
    """The side's rows in one direction, each with its labels (left to right) and its overall,
    and why the others were left out."""
    placed = [
        (where, label)
        for label in labels
        if label.get("side") == side
        and label.get("orientation") == orientation
        and (where := _along(label)) is not None
    ]
    placed.sort(key=lambda item: (item[0][0], item[0][1]))
    lines: list[list[tuple[tuple[float, float, float], Label]]] = []
    for item in placed:
        if lines and abs(lines[-1][0][0][0] - item[0][0]) <= LINE_PT:
            lines[-1].append(item)
        else:
            lines.append([item])
    chains: list[list[tuple[tuple[float, float, float], Label]]] = []
    for line in lines:
        line.sort(key=lambda item: item[0][1])
        chain = [line[0]]
        for item in line[1:]:
            if abs(item[0][1] - chain[-1][0][2]) <= JOIN_PT:
                chain.append(item)
            else:
                chains.append(chain)
                chain = [item]
        chains.append(chain)
    out: list[tuple[DrawnRow, list[Label], Label | None]] = []
    notes: list[str] = []
    for number, chain in enumerate(c for c in chains if len(c) >= 2):
        start, end, across = chain[0][0][1], chain[-1][0][2], chain[0][0][0]
        overall = next(
            (
                label
                for where, label in placed
                if 0 < abs(where[0] - across) <= OVERALL_PT
                and abs(where[1] - start) <= JOIN_PT
                and abs(where[2] - end) <= JOIN_PT
            ),
            None,
        )
        members = [label for _, label in chain] + ([overall] if overall is not None else [])
        ids = ", ".join(str(label.get("id")) for label in members)
        untrusted = [str(label.get("id")) for label in members if label.get("id") not in trusted]
        if untrusted:
            notes.append(
                f"{side} row {ids}: {', '.join(untrusted)} not drawn to its value at a measured "
                "scale, so its ticks cannot be trusted"
            )
            continue
        scale = view_scales.scale_at(scales, chain[0][1]["box"], side=side)
        if scale is None:
            notes.append(f"{side} row {ids}: no measured scale there")
            continue
        architect = side == "architect"
        # Each span between its own neighbours' ticks: a chain shares its ticks exactly.
        ticks = (
            [start]
            + [(chain[i][0][2] + chain[i + 1][0][1]) / 2 for i in range(len(chain) - 1)]
            + [end]
        )
        spans = tuple(
            _span(label, ticks[i], ticks[i + 1], architect=architect)
            for i, (_, label) in enumerate(chain)
        )
        drawn_overall = None if overall is None else _span(overall, start, end, architect=architect)
        row = DrawnRow(
            key=f"{side[0]}{orientation}{number}",
            spans=spans,
            overall=drawn_overall,
            pt_per_inch=1 / Fraction(scale.in_per_pt).limit_denominator(10**6),
        )
        out.append((row, [label for _, label in chain], overall))
    return out, notes


def _value(labels: Sequence[Label]) -> Fraction | None:
    total = Fraction(0)
    for label in labels:
        inches = _inches(label)
        if inches is None:
            return None
        total += inches
    return total


def _text(value: Fraction) -> str:
    whole, rest = divmod(value, 1)
    if not rest:
        return f'{whole}"'
    return f'{whole} {rest.numerator}/{rest.denominator}"' if whole else f'{rest}"'


def page_pairs(
    page: int,
    labels: Sequence[Label],
    scales: Sequence[view_scales.ViewScale],
    checks: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """One page's architect-vendor pairs (`pairs`), and why rows were not paired (`notes`)."""
    trusted = {str(check["id"]) for check in checks if check.get("drawn_to_value")}
    pairs: list[dict[str, Any]] = []
    notes: list[str] = []
    for orientation in ("h", "v"):
        architect, arch_notes = _rows(labels, "architect", orientation, scales, trusted)
        vendor, vendor_notes = _rows(labels, "vendor", orientation, scales, trusted)
        notes += arch_notes + vendor_notes
        if not architect or not vendor:
            continue
        by_key = {row.key: (row, members, overall) for row, members, overall in architect}
        for vendor_row, vendor_labels, vendor_overall in vendor:
            result = pair_rows(vendor_row, [row for row, _, _ in architect], SETTINGS)
            if result.status is not PairingStatus.PAIRED or result.architect_row_key is None:
                notes.append(
                    f"vendor row {vendor_row.spans[0].printed}..{vendor_row.spans[-1].printed}: "
                    f"{result.status.value}: {' '.join(result.reasons)[:300]}"
                )
                continue
            _, arch_labels, arch_overall = by_key[result.architect_row_key]
            for pair in result.pairs:
                arch = (
                    arch_overall
                    if pair.architect_span_index == "overall"
                    else arch_labels[int(pair.architect_span_index)]
                )
                if arch is None:
                    continue
                if pair.kind == "overall" and vendor_overall is not None:
                    pieces: list[Label] = [vendor_overall]
                else:
                    pieces = [vendor_labels[i] for i in pair.vendor_span_indices]
                if not pieces:
                    continue
                architect_in, vendor_in = _inches(arch), _value(pieces)
                if architect_in is None or vendor_in is None:
                    continue
                same = abs(architect_in - vendor_in) <= SAME_IN
                routes = {str(label.get("route")) for label in (arch, *pieces)}
                pairs.append(
                    {
                        "page": page,
                        "kind": pair.kind,
                        "architect": {"id": arch.get("id"), "text": arch.get("text")},
                        "vendor": [{"id": p.get("id"), "text": p.get("text")} for p in pieces],
                        "architect_in": _text(architect_in),
                        "vendor_in": _text(vendor_in),
                        "status": "same" if same else "different",
                        "ends_off_in": [
                            float(round(pair.start_error_in, 3)),
                            float(round(pair.end_error_in, 3)),
                        ],
                        "printed_both": routes == {"text"},
                        "how": (
                            f"paired by drawn position: both ends within "
                            f"{_text(SETTINGS.tick_tolerance_in)} at each drawing's measured "
                            f"scale ({result.support} ticks agree, best rival {result.runner_up_support})"
                        ),
                    }
                )
    return {"page": page, "pairs": pairs, "notes": notes[:40]}


def set_pairs(dims: Mapping[str, Any], scales: Mapping[str, Any]) -> dict[str, Any]:
    """`pairs.json` for a whole set, from its `dims.json` and `scales.json`."""
    by_page = {
        (str(entry.get("file")), int(entry.get("page") or 0)): entry
        for entry in scales.get("pages") or []
        if isinstance(entry, Mapping)
    }
    pages: list[dict[str, Any]] = []
    for entry in dims.get("pages") or []:
        if not isinstance(entry, Mapping):
            continue
        key = (str(entry.get("file")), int(entry.get("page") or 0))
        found = by_page.get(key) or {}
        measured = [
            scale
            for raw in found.get("scales") or []
            if isinstance(raw, Mapping) and (scale := view_scales.from_dict(raw)) is not None
        ]
        labels = [label for label in entry.get("labels") or [] if isinstance(label, Mapping)]
        sides = {label.get("side") for label in labels}
        if not {"architect", "vendor"} <= sides:
            continue
        result = page_pairs(key[1], labels, measured, found.get("labels") or [])
        pages.append({"file": key[0], **result})
    return {"version": 1, "pairs": flat_pairs(pages), "pages": pages}


def flat_pairs(pages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The one-to-one pairs as the full check reads them (`full_check_advocate.code_pairs`):
    `{"vendor": "p23.D30 254 [10]", "architect": "p23.D10 8\"", "status": "same" | "different"}`.
    A pair of one architect dimension with several vendor pieces stays in its page's list only:
    no one vendor label stands for it."""
    out: list[dict[str, Any]] = []
    for entry in pages:
        for pair in entry.get("pairs") or []:
            vendor = pair.get("vendor") or []
            if len(vendor) != 1:
                continue
            page, arch = pair.get("page"), pair.get("architect") or {}
            out.append(
                {
                    "vendor": f"p{page}.{vendor[0].get('id')} {vendor[0].get('text')}",
                    "architect": f"p{page}.{arch.get('id')} {arch.get('text')}",
                    "status": pair.get("status"),
                    "file": entry.get("file"),
                    "page": page,
                    "architect_in": pair.get("architect_in"),
                    "vendor_in": pair.get("vendor_in"),
                    "printed_both": pair.get("printed_both"),
                    "how": pair.get("how"),
                }
            )
    return out


def pairs_on(folder: Path | None, file: str, page: int) -> dict[str, Any] | None:
    """One page's entry of the set pack's `pairs.json`, or `None`."""
    if folder is None:
        return None
    try:
        document = json.loads((folder / PAIRS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for entry in document.get("pages") or [] if isinstance(document, Mapping) else []:
        if isinstance(entry, Mapping) and entry.get("file") == file and entry.get("page") == page:
            return dict(entry)
    return None
