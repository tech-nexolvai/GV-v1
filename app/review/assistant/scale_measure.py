"""Measure by scale where the reference prints no size: code, $0, no AI (client meeting, 2026-10-23).

GV's client asked in the meeting: "if it's not in the architect drawing but shown in the shop
drawing, use the scale". A reviewer does that with a ruler: the architect's drawing shows the piece
but prints no size for it, so they scale it. Code does the same, carefully:

- **Which items**: an item still open (a difference code did not prove, or Not compared) whose
  vendor side is a printed label code read (a width or a height), whose reference side prints no
  number at all, and whose reference spot is a tight box round the piece (not a whole drawing).
- **The scale** is the view's own, worked out from the view's printed dimensions
  (`view_scales`, at least three agreeing), never the title block's scale note: views on one
  sheet are drawn at different scales (a 1:25 plan beside a 1 1/2" = 1'-0" section).
- **The span**: the spot's two edges along the label's direction, each snapped onto the
  drawing's own line work (`measure.measure_on_drawing`); an edge that lands on no line is no
  measurement.
- **What it says**: "measured by scale: about X" on the item. A measured value that differs from
  the vendor's by more than `TOLERANCE` (1/4", proposed to the owner and applied) is a Likely
  problem: "please check: measured about X on the reference drawing". A measured value is **never
  red** (only printed values are): the item keeps its status and is marked scaled.

Verification: `tests/review/assistant/test_scale_measure.py` (a synthetic PDF).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from fractions import Fraction
from importlib import import_module
from pathlib import Path
from typing import Any, Final

from app.review.assistant import likely_problems, view_scales
from app.review.assistant.finding_checks import claim_value, to_inches

__all__ = [
    "FIELD",
    "MAX_ITEMS",
    "TOLERANCE",
    "add_to_answer",
    "measure_item",
]

_log = logging.getLogger(__name__)

#: The finding's field with what code measured.
FIELD: Final = "measured_by_scale"
#: A measured value further than this from the vendor's is a Likely problem (owner, 2026-10-23).
TOLERANCE: Final = Fraction(1, 4)
#: The most items one check measures (each reads one page's line work).
MAX_ITEMS: Final = 40
#: A spot covering at least this share of its drawing (or page) is no tight box round a piece.
_WHOLE_SHARE: Final = 0.25
#: A spot narrower than this (points) along the direction is too short to measure.
_SHORTEST_PT: Final = 8.0
_OPEN: Final = frozenset({"check", "different"})


def _inches_text(value: float) -> str:
    sixteenths = round(value * 16)
    whole, rest = divmod(sixteenths, 16)
    if not rest:
        return f'{whole}"'
    part = Fraction(rest, 16)
    return f'{whole} {part.numerator}/{part.denominator}"' if whole else f'{part}"'


def _number(text: object) -> Fraction | None:
    claim = claim_value(" ".join(str(text or "").split()))
    value = None if claim is None else to_inches(claim)
    return None if value is None else Fraction(value)


def _vendor(finding: Mapping[str, Any]) -> tuple[Fraction, str] | None:
    """The vendor's printed value and its direction, from the label code read."""
    dims = finding.get("dims")
    label = dims.get("vendor") if isinstance(dims, Mapping) else None
    if not isinstance(label, Mapping) or label.get("orientation") not in ("h", "v"):
        return None
    if str(label.get("route") or "text") != "text":
        return None
    try:
        value = Fraction(str(label.get("inches")))
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return (value, str(label["orientation"])) if value > 0 else None


def _prints_nothing(finding: Mapping[str, Any]) -> bool:
    dims = finding.get("dims")
    if isinstance(dims, Mapping) and isinstance(dims.get("architect"), Mapping):
        return False
    return _number(finding.get("architect")) is None


def _box(raw: object) -> tuple[float, float, float, float] | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        x0, y0, x1, y1 = (float(raw[k]) for k in ("x0", "y0", "x1", "y1"))
    except (KeyError, TypeError, ValueError):
        return None
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _tight(box: tuple[float, float, float, float], around: Sequence[Sequence[float]]) -> bool:
    """A box round one piece, not a whole drawing: under `_WHOLE_SHARE` of every drawing (or the
    page) holding its centre."""
    area = (box[2] - box[0]) * (box[3] - box[1])
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    holders = [b for b in around if len(b) == 4 and b[0] <= cx <= b[2] and b[1] <= cy <= b[3]] or [
        (0.0, 0.0, 842.0, 595.0)
    ]
    return all(area < _WHOLE_SHARE * (b[2] - b[0]) * (b[3] - b[1]) for b in holders)


type Measure = Callable[..., Any]


def measure_item(
    finding: Mapping[str, Any],
    *,
    pdf: Path,
    scales: Sequence[view_scales.ViewScale],
    drawings: Sequence[Sequence[float]] = (),
    measure: Measure | None = None,
) -> dict[str, Any] | None:
    """What code measured on the reference for one item (see the module), or `None` when the item
    is not one to measure or code cannot measure it surely. `drawings`: the boxes of the reference
    page's drawings (the page map's halves); `measure`: `measure.measure_on_drawing` (tests pass a
    stand-in)."""
    if finding.get("status") not in _OPEN or finding.get(FIELD):
        return None
    vendor = _vendor(finding)
    if vendor is None or not _prints_nothing(finding):
        return None
    vendor_in, axis = vendor
    spot = finding.get("arch_box")
    box = _box(spot)
    page = spot.get("page") if isinstance(spot, Mapping) else None
    if box is None or not isinstance(page, int) or not _tight(box, drawings):
        return None
    if (box[2] - box[0] if axis == "h" else box[3] - box[1]) < _SHORTEST_PT:
        return None
    scale = view_scales.scale_at(scales, box, side="architect") or view_scales.scale_at(scales, box)
    if scale is None or len(scale.labels) < view_scales.MIN_LABELS:
        return None
    if measure is None:
        # Loaded by name, as `chat_tools` does: it reads PDFs (never imported by the API).
        measure = import_module("app.review.assistant.measure").measure_on_drawing
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    start, end = (
        ({"x": box[0], "y": cy}, {"x": box[2], "y": cy})
        if axis == "h"
        else ({"x": cx, "y": box[1]}, {"x": cx, "y": box[3]})
    )
    try:
        measured = measure(
            pdf,
            page,
            start,
            end,
            None,
            axis,
            scale={
                "in_per_pt": scale.in_per_pt,
                "from_labels": list(scale.labels),
                "spread_percent": scale.spread_percent,
            },
        )
    except Exception as refused:  # noqa: BLE001 - MeasureRefused or an unreadable page: no value
        _log.info("assistant measured by scale: not measured (%s)", refused)
        return None
    ends = tuple(str(e) for e in getattr(measured, "snapped", ()) or ())
    if len(ends) != 2 or any(e.startswith("as given") for e in ends):
        return None  # an edge on no line of the drawing: no measurement
    value = float(measured.value_in)
    off = abs(Fraction(round(value * 16), 16) - vendor_in)
    return {
        "value_in": value,
        "text": f"about {_inches_text(value)}",
        "plus_minus_in": float(measured.plus_minus_in),
        "axis": axis,
        "scale": f"about 1:{round(float(measured.scale_ratio))}",
        "from_labels": list(scale.labels),
        "page": page,
        "vendor_in": float(vendor_in),
        "off_in": float(off),
        "differs": off > TOLERANCE,
        "ends": list(ends),
    }


def _with_measure(finding: Mapping[str, Any], found: Mapping[str, Any]) -> dict[str, Any]:
    """The item with what code measured: always "measured by scale: about X"; a Likely problem
    when it differs from the vendor's by more than `TOLERANCE`; never red (marked scaled)."""
    item = " ".join(str(finding.get("item") or "this item").split())
    vendor = " ".join(str(finding.get("vendor") or "").split()) or _inches_text(found["vendor_in"])
    said = (
        f"Measured by scale: {found['text']} on the reference drawing (page {found['page']}, "
        f"{found['scale']}, from {len(found['from_labels'])} of its printed dimensions; scaled, "
        "not printed: never a Wrong by itself)."
    )
    out = {**finding, FIELD: dict(found)}
    out["why"] = " ".join(f"{said} {finding.get('why') or ''}".split())[:600]
    if found["differs"] and likely_problems.FIELD not in finding:
        out["status"] = "check"
        out[likely_problems.FIELD] = {
            "by": FIELD,
            "kind": "size",
            "proof": "likely_only",
            "sure": "likely",
            "why": (
                f"The reference drawing prints no size here; code measured {found['text']} by "
                f"its own scale, and the vendor prints {vendor}."
            ),
            "words": (
                f"{likely_problems.LETTER_LEAD}: {item}: measured {found['text']} on the "
                f"reference drawing (it prints no size here); the vendor's drawing shows {vendor}."
            ),
        }
    return out


def add_to_answer(engine: Any) -> dict[str, int] | None:
    """The engine's hook (a full check, at its finish, $0): every open item whose reference prints
    no size, measured by the reference's own scale where code can (see the module)."""
    if getattr(engine.settings, "mode", None) != "full_check":
        return None
    workspace = engine.tools.workspace
    folder, uploads = workspace.set_pack, getattr(workspace, "uploads", None)
    if folder is None or uploads is None:
        return None
    book = engine.book
    counts = {"measured": 0, "likely": 0}
    tried = 0
    for finding in list(book.live()):
        if tried >= MAX_ITEMS:
            break
        spot = finding.get("arch_box")
        if not isinstance(spot, Mapping) or _vendor(finding) is None:
            continue
        if finding.get("status") not in _OPEN or not _prints_nothing(finding):
            continue
        file = str(spot.get("file") or book.main_file or "")
        pdf = Path(uploads) / file
        page = spot.get("page")
        if not file or not pdf.is_file() or not isinstance(page, int):
            continue
        tried += 1
        drawings = [
            list(half.get("box") or ())
            for half in book.dim_labels.halves(file, page)
            if isinstance(half, Mapping)
        ]
        found = measure_item(
            finding,
            pdf=pdf,
            scales=view_scales.scales_on(folder, file, page),
            drawings=drawings,
        )
        if found is None:
            continue
        updated = _with_measure(finding, found)
        with engine.finding_lock:
            if book.replace(updated):
                engine._show_finding(updated)
                counts["measured"] += 1
                counts["likely"] += 1 if found["differs"] else 0
    if counts["measured"]:
        _log.warning("assistant measured by scale: %s", counts)
    return counts
