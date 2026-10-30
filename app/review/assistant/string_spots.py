"""Where each printed number of a vendor string is (2026-10-31, the drawer's "vendor's own numbers").

A finding that checks the vendor's own numbers (a row of parts that does not add up to its printed
overall: `source` `vendor_arithmetic`, from code's consistency check, the reading ledger or the page
reviewer) carries its parts and overall as printed (`parts`, `printed_total`), but only one box
for the whole row. The result screen's drawer shows each part's own close-up and boxes each part
and the overall apart on the vendor's page, so it needs each number's own spot.

This step, worked out on read (nothing stored), finds those spots among the set pack's numbered
dimension labels (`dims.json`, the same labels the checks read) and adds them to the finding:

- `total_box`: the printed overall's label;
- `part_boxes`: one entry per part, in the finding's order; a part code could not place has no
  `box` (the screen then shows its value only).

How a label is found, by code facts only:

1. **The labels the finding names.** Code's consistency check keeps its labels (`ledger_ids`
   `p<page>.<label id>`: the overall first, then the parts); the overall of any other finding is
   the label its vendor side was built from (`dims.vendor`). A named label is used only when its
   value is the printed one.
2. **The row on the drawing.** The parts are the labels whose dimension lines run along the
   overall's own dimension line, inside its two ends, touching end to end on one line, with the
   same values in the same order (either way along the line). Exactly one such row, or none is
   used.
3. **Else, inside the finding's box.** A value is placed only when the box holds exactly as many
   labels of that value as the row has parts of it.

Nothing is guessed: a part with no unambiguous label stays without a box. Read only, plain code,
no AI call; never changes a value, a group or a decision.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Any, Final

from app.review.assistant.finding_checks import to_inches

__all__ = [
    "PART_BOXES",
    "TOTAL_BOX",
    "LabelsOf",
    "blocks_with_spots",
    "spots_of",
]

#: The finding's fields this step adds.
PART_BOXES: Final = "part_boxes"
TOTAL_BOX: Final = "total_box"

type Box = tuple[float, float, float, float]
type LabelsOf = Callable[[str | None, int], Sequence[Mapping[str, Any]] | None]
"""A page's labels as the set pack's `dims.json` holds them, by (file, page); `None` file: the
vendor's main file."""

#: How far a part's dimension line may sit from the overall's, across the line (points): a row of
#: parts is drawn just inside its overall (on GV's sets 10-20 pt).
ACROSS_REACH_PT: Final = 48.0
#: Two dimension lines on one row: their positions across the line differ by at most this (points).
SAME_ROW_PT: Final = 2.0
#: How far past the overall's ends a part's line may run (points: arrowheads and rounding).
END_SLACK_PT: Final = 3.0
#: Two parts touch end to end: the gap (or overlap) between their lines is at most this (points).
TOUCH_PT: Final = 3.0
#: The padding round the finding's box when the parts are looked for inside it (points).
BOX_PAD_PT: Final = 6.0

_CODE_LABEL: Final = re.compile(r"^p(\d+)\.(\S+)$")


def _inches(value: object) -> Decimal | None:
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    if not isinstance(value, str) or not value.strip():
        return None
    return to_inches(value)


def _label_inches(label: Mapping[str, Any]) -> Decimal | None:
    raw = label.get("inches")
    if raw is not None:
        try:
            return Decimal(str(raw))
        except InvalidOperation:
            return None
    return _inches(label.get("text"))


def _box(label: Mapping[str, Any]) -> Box | None:
    raw = label.get("box")
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def _line(label: Mapping[str, Any]) -> tuple[str, float, float, float] | None:
    """A label's dimension line as (axis, start, end, across): `v` up and down, `h` across."""
    raw = label.get("dim_line")
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    try:
        (ax, ay), (bx, by) = ((float(p[0]), float(p[1])) for p in raw)
    except (TypeError, ValueError, IndexError):
        return None
    if abs(ax - bx) <= 0.5 and abs(ay - by) > 0.5:
        return "v", min(ay, by), max(ay, by), (ax + bx) / 2
    if abs(ay - by) <= 0.5 and abs(ax - bx) > 0.5:
        return "h", min(ax, bx), max(ax, bx), (ay + by) / 2
    return None


def _spot(
    text: str, label: Mapping[str, Any] | None, page: int, file: str | None
) -> dict[str, Any]:
    """One number as the screen reads it: as printed, its inches, its box (when placed)."""
    inches = _label_inches(label) if label is not None else _inches(text)
    box = _box(label) if label is not None else None
    out: dict[str, Any] = {
        "text": text,
        "inches": None if inches is None else format(inches.normalize(), "f"),
        "box": None,
        "turned": False,
    }
    if box is not None:
        x0, y0, x1, y1 = box
        out["box"] = {"page": page, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "file": file}
        out["turned"] = label is not None and label.get("orientation") == "v"
    return out


def _named(
    finding: Mapping[str, Any], labels: Sequence[Mapping[str, Any]], page: int
) -> tuple[Mapping[str, Any] | None, list[Mapping[str, Any] | None] | None]:
    """The overall and the parts the finding names by label (code's consistency check), else
    the overall its vendor side was built from; `None` parts when it names none."""
    by_id = {str(label.get("id")): label for label in labels if isinstance(label, Mapping)}
    ids = [str(each) for each in finding.get("ledger_ids") or []]
    own = [_CODE_LABEL.match(each) for each in ids]
    if ids and all(found is not None and int(found.group(1)) == page for found in own):
        named = [by_id.get(found.group(2)) for found in own if found is not None]
        parts = finding.get("parts") or []
        if len(named) == len(parts) + 1:
            return named[0], named[1:]
    dims = finding.get("dims")
    vendor = dims.get("vendor") if isinstance(dims, Mapping) else None
    if isinstance(vendor, Mapping) and int(vendor.get("page") or 0) == page:
        return by_id.get(str(vendor.get("id"))), None
    return None, None


def _same(label: Mapping[str, Any] | None, value: Decimal | None) -> bool:
    return label is not None and value is not None and _label_inches(label) == value


def _overall(
    finding: Mapping[str, Any],
    labels: Sequence[Mapping[str, Any]],
    named: Mapping[str, Any] | None,
    total: Decimal | None,
    row: Box | None,
) -> Mapping[str, Any] | None:
    if _same(named, total):
        return named
    if row is None or total is None:
        return None
    inside = [
        label for label in labels if _same(label, total) and _inside(_box(label), row, BOX_PAD_PT)
    ]
    return inside[0] if len(inside) == 1 else None


def _inside(box: Box | None, row: Box, pad: float) -> bool:
    if box is None:
        return False
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return row[0] - pad <= cx <= row[2] + pad and row[1] - pad <= cy <= row[3] + pad


def _row_parts(
    overall: Mapping[str, Any], labels: Sequence[Mapping[str, Any]], values: Sequence[Decimal]
) -> list[Mapping[str, Any]] | None:
    """The labels of the one row of parts along the overall's dimension line (see the module),
    in the finding's order; `None` when there is no such row, or more than one."""
    line = _line(overall)
    if line is None:
        return None
    axis, start, end, across = line
    rows: list[list[tuple[float, float, float, Mapping[str, Any]]]] = []
    for label in labels:
        if label is overall:
            continue
        own = _line(label)
        if own is None or own[0] != axis:
            continue
        _axis, s, e, a = own
        if abs(a - across) > ACROSS_REACH_PT or s < start - END_SLACK_PT or e > end + END_SLACK_PT:
            continue
        if e - s >= end - start - END_SLACK_PT:
            continue  # as long as the overall: another overall, not a part
        for row in rows:
            if abs(row[0][2] - a) <= SAME_ROW_PT:
                row.append((s, e, a, label))
                break
        else:
            rows.append([(s, e, a, label)])
    found: list[list[Mapping[str, Any]]] = []
    for row in rows:
        row.sort(key=lambda one: one[0])
        if any(b[0] - a[1] > TOUCH_PT or a[1] - b[0] > TOUCH_PT for a, b in pairwise(row)):
            continue
        chain = [one[3] for one in row]
        read = [_label_inches(label) for label in chain]
        if read == list(values):
            found.append(chain)
        elif read == list(reversed(values)):
            found.append(list(reversed(chain)))
    return found[0] if len(found) == 1 else None


def _row_axis(overall: Mapping[str, Any] | None, row: Box) -> str | None:
    """Which way the row runs: its overall's dimension line, else the box's clear shape."""
    line = _line(overall) if overall is not None else None
    if line is not None:
        return line[0]
    width, height = row[2] - row[0], row[3] - row[1]
    if width >= 2 * height:
        return "h"
    if height >= 2 * width:
        return "v"
    return None


def _along(label: Mapping[str, Any], axis: str, overall: Mapping[str, Any] | None) -> bool:
    """A label that can be a part of a row running `axis`: its dimension line runs that way (and
    inside the overall's, when the overall's line is known); with no line, its text does."""
    own = _line(label)
    if own is None:
        return label.get("orientation") == axis
    if own[0] != axis:
        return False
    line = _line(overall) if overall is not None else None
    if line is None:
        return True
    _axis, start, end, across = line
    return (
        abs(own[3] - across) <= ACROSS_REACH_PT
        and own[1] >= start - END_SLACK_PT
        and own[2] <= end + END_SLACK_PT
        and own[2] - own[1] < end - start - END_SLACK_PT
    )


def _box_parts(
    labels: Sequence[Mapping[str, Any]],
    values: Sequence[Decimal | None],
    row: Box,
    overall: Mapping[str, Any] | None,
) -> list[Mapping[str, Any] | None]:
    """Each part's label inside the finding's box, only where its value is unambiguous there
    among the labels running the row's way."""
    out: list[Mapping[str, Any] | None] = [None] * len(values)
    axis = _row_axis(overall, row)
    if axis is None:
        return out
    for value in {v for v in values if v is not None}:
        wanted = [at for at, v in enumerate(values) if v == value]
        inside = [
            label
            for label in labels
            if label is not overall
            and _same(label, value)
            and _inside(_box(label), row, BOX_PAD_PT)
            and _along(label, axis, overall)
        ]
        if len(inside) != len(wanted):
            continue
        # Equal values: any order shows the same numbers; along the row as drawn.
        at_x = 0 if axis == "h" else 1
        inside.sort(key=lambda label: (_box(label) or (0.0, 0.0, 0.0, 0.0))[at_x])
        for at, label in zip(wanted, inside):
            out[at] = label
    return out


def _row_box(finding: Mapping[str, Any]) -> Box | None:
    raw = finding.get("vendor_box")
    if not isinstance(raw, Mapping):
        return None
    try:
        x0, y0, x1, y1 = (float(raw[k]) for k in ("x0", "y0", "x1", "y1"))
    except (KeyError, TypeError, ValueError):
        return None
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def spots_of(finding: Mapping[str, Any], labels_of: LabelsOf) -> dict[str, Any] | None:
    """`{"part_boxes": [...], "total_box": {...} | None}` for a finding with printed parts and an
    overall, when code places at least one of its numbers (see the module); else `None`."""
    parts = [str(p) for p in finding.get("parts") or [] if isinstance(p, str) and p.strip()]
    printed = finding.get("printed_total")
    if not parts or not isinstance(printed, str) or not printed.strip():
        return None
    vendor_box = finding.get("vendor_box")
    page = int(
        (vendor_box.get("page") if isinstance(vendor_box, Mapping) else None)
        or finding.get("page")
        or 0
    )
    if page < 1:
        return None
    dims = finding.get("dims")
    vendor_dim = dims.get("vendor") if isinstance(dims, Mapping) else None
    file = (vendor_box.get("file") if isinstance(vendor_box, Mapping) else None) or (
        vendor_dim.get("file") if isinstance(vendor_dim, Mapping) else None
    )
    labels = [label for label in labels_of(file, page) or () if isinstance(label, Mapping)]
    if not labels:
        return None
    values = [_inches(p) for p in parts]
    total = _inches(printed)
    row = _row_box(finding)
    named_total, named_parts = _named(finding, labels, page)
    overall = _overall(finding, labels, named_total, total, row)
    placed: list[Mapping[str, Any] | None]
    if named_parts is not None and all(_same(l, v) for l, v in zip(named_parts, values)):
        placed = list(named_parts)
    else:
        chain = (
            _row_parts(overall, labels, [v for v in values if v is not None])
            if overall is not None and None not in values
            else None
        )
        if chain is not None:
            placed = list(chain)
        elif row is not None:
            placed = _box_parts(labels, values, row, overall)
        else:
            placed = [None] * len(parts)
    if overall is None and not any(placed):
        return None
    return {
        PART_BOXES: [_spot(text, label, page, file) for text, label in zip(parts, placed)],
        TOTAL_BOX: _spot(printed, overall, page, file) if overall is not None else None,
    }


def blocks_with_spots(
    blocks: Sequence[Mapping[str, Any]], labels_of: LabelsOf | None
) -> list[dict[str, Any]]:
    """An answer's blocks with each vendor string's number spots (worked out on read; nothing
    stored). A loader that fails leaves the finding as it was."""
    out: list[dict[str, Any]] = []
    for block in blocks:
        finding = block.get("finding")
        if (
            labels_of is not None
            and block.get("type") == "finding"
            and isinstance(finding, Mapping)
            and finding.get("parts")
        ):
            try:
                found = spots_of(finding, labels_of)
            except (OSError, ValueError, TypeError, KeyError):
                found = None
            if found is not None:
                block = {**block, "finding": {**finding, **found}}
        out.append(dict(block))
    return out
