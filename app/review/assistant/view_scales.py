"""Each drawing's true scale, measured from its own printed dimensions: code, $0, no AI.

A ruler on a drawing needs its scale. The scale note a drawing prints (`1/2" = 1'-0"`) is right
only where the drawing was pasted at its own size: GV resizes the vendor's and the architect's
drawings when it pastes them onto its combined sheets, so on a real set the note and the drawing
disagree by up to two times. The printed dimensions do not: each one is drawn between its own
extension lines, so its printed inches over its drawn length is the scale of the view it is in.

**How.** For each drawing on a page (the page map's halves: architect, vendor, brand) every label
of `dims.json` with a dimension line and at least 2 inches gives inches per drawn point. Labels
of one view agree with each other to a fraction of a percent; labels of another view at another
scale, or a dimension not drawn to its value, do not. So the ratios are grouped: a group is the
labels within `AGREE` of the group's median, and a group of at least `MIN_LABELS` is one scale,
with the box round its labels and lines (a view, or several views at the same scale). A drawing
whose labels form no such group has no scale here (`cannot_tell`): no number beats a wrong one.

**What each label then says.** A label inside a scale's box is measured against it: `drawn_in`
(its drawn length in inches) and `off_by` (drawn minus printed). A label off by more than
`OFF_TOLERANCE` of its value is not drawn to its value (a dimension typed over, or a line code
took for its own that is not): other code passes do not trust its geometry.

**The printed scale notes** are read too (`dims.json` keeps them per page) and set beside the
measured scale: `note_agrees` is false where GV resized the drawing.

A scaled length is never a Wrong by itself (it only helps a person decide); this module's job is
to witness other code (`measure_on_drawing` without hand calibration, strings, pairing).

Pure: no PDF, no model. Verification: `tests/review/assistant/test_view_scales.py`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any, Final

__all__ = [
    "AGREE",
    "MIN_LABELS",
    "SCALES_FILE",
    "ViewScale",
    "from_dict",
    "page_scales",
    "scale_at",
    "scales_on",
    "set_scales",
]

SCALES_FILE: Final = "scales.json"
#: Labels whose inches per point are within this share of their group's median are one scale.
AGREE: Final = 0.01
#: A scale needs this many labels that agree.
MIN_LABELS: Final = 3
#: The shortest label (inches) that gives a scale: a short one's drawn length is mostly line width.
MIN_INCHES: Final = Decimal(2)
#: A label inside a scale's box drawn more than this share off its value is not drawn to it.
OFF_TOLERANCE: Final = 0.05
#: A printed scale note agrees with the measured scale within this share.
NOTE_AGREE: Final = 0.03
#: A printed scale note belongs to the nearest scale at most this far (points) from its box.
NOTE_REACH_PT: Final = 120.0
#: A scale's box reaches this far (points) round its labels and lines.
BOX_MARGIN_PT: Final = 24.0

type Box = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class ViewScale:
    """One scale on one drawing of a page."""

    page: int
    side: str
    in_per_pt: float
    """Real inches per drawn point."""
    labels: tuple[str, ...]
    """The ids of the labels that agree on it."""
    spread_percent: float
    box: Box
    note: str | None = None
    note_in_per_pt: float | None = None

    @property
    def ratio(self) -> float:
        """Real inches per drawn inch (24 for 1/2" = 1'-0")."""
        return self.in_per_pt * 72

    @property
    def note_agrees(self) -> bool | None:
        if self.note_in_per_pt is None:
            return None
        return abs(self.note_in_per_pt / self.in_per_pt - 1) <= NOTE_AGREE

    def as_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "side": self.side,
            "in_per_pt": round(self.in_per_pt, 6),
            "about": f"1:{round(self.ratio)}",
            "from_labels": list(self.labels),
            "spread_percent": round(self.spread_percent, 2),
            "box": [round(v, 1) for v in self.box],
            "note": self.note,
            "note_in_per_pt": (
                None if self.note_in_per_pt is None else round(self.note_in_per_pt, 6)
            ),
            "note_agrees": self.note_agrees,
        }


def _inches(label: Mapping[str, Any]) -> Decimal | None:
    try:
        return Decimal(str(label.get("inches")))
    except (InvalidOperation, ValueError):
        return None


def _usable(label: Mapping[str, Any]) -> float | None:
    """A label's inches per drawn point, or `None` when it gives no scale."""
    inches = _inches(label)
    span = label.get("span_pt")
    if inches is None or inches < MIN_INCHES or not isinstance(span, (int, float)) or span <= 0:
        return None
    if label.get("orientation") not in ("h", "v") or not label.get("dim_line"):
        return None
    return float(inches) / float(span)


def _box_of(labels: Iterable[Mapping[str, Any]]) -> Box:
    xs: list[float] = []
    ys: list[float] = []
    for label in labels:
        box = label["box"]
        xs += [float(box[0]), float(box[2])]
        ys += [float(box[1]), float(box[3])]
        for point in label.get("dim_line") or []:
            xs.append(float(point[0]))
            ys.append(float(point[1]))
    return (
        min(xs) - BOX_MARGIN_PT,
        min(ys) - BOX_MARGIN_PT,
        max(xs) + BOX_MARGIN_PT,
        max(ys) + BOX_MARGIN_PT,
    )


def _groups(rated: list[tuple[float, Mapping[str, Any]]]) -> list[list[tuple[float, Any]]]:
    """Ratios grouped: the largest set of labels within `AGREE` of its own median first, then
    the largest among the rest, until no set of `MIN_LABELS` is left."""
    left = sorted(rated, key=lambda item: item[0])
    out: list[list[tuple[float, Any]]] = []
    while len(left) >= MIN_LABELS:
        best: list[tuple[float, Any]] = []
        for start in range(len(left)):
            centre = left[start][0]
            members = [item for item in left if abs(item[0] / centre - 1) <= AGREE]
            middle = median(item[0] for item in members)
            members = [item for item in left if abs(item[0] / middle - 1) <= AGREE]
            if len(members) > len(best):
                best = members
        if len(best) < MIN_LABELS:
            break
        out.append(best)
        chosen = {id(item) for item in best}
        left = [item for item in left if id(item) not in chosen]
    return out


def _note_scale(text: str) -> float | None:
    """Inches per point of a printed scale note (`1/2" = 1'-0"`: 1/2 drawn inch is 12 inches)."""
    from app.review.assistant.finding_checks import to_inches

    left, _, right = text.partition("=")
    drawn, real = to_inches(left.strip()), to_inches(right.strip())
    if drawn is None or real is None or drawn <= 0 or real <= 0:
        return None
    return float(real) / (float(drawn) * 72)


def _inside(box: Sequence[float], area: Box) -> bool:
    cx, cy = (float(box[0]) + float(box[2])) / 2, (float(box[1]) + float(box[3])) / 2
    return area[0] <= cx <= area[2] and area[1] <= cy <= area[3]


def page_scales(
    page: int,
    labels: Sequence[Mapping[str, Any]],
    notes: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[ViewScale], list[dict[str, Any]]]:
    """The scales of one page from its labels (as `dims.json` holds them), and each label's
    check against the scale whose box holds it: `{id, drawn_in, off_by, drawn_to_value}`.
    `notes`: the page's printed scale notes (`{text, box}`), each set beside the scale nearest
    it (within `NOTE_REACH_PT`)."""
    by_side: dict[str, list[tuple[float, Mapping[str, Any]]]] = {}
    for label in labels:
        ratio = _usable(label)
        if ratio is not None:
            by_side.setdefault(str(label.get("side") or "unknown"), []).append((ratio, label))
    scales: list[ViewScale] = []
    for side, rated in sorted(by_side.items()):
        for group in _groups(rated):
            ratios = [ratio for ratio, _ in group]
            middle = median(ratios)
            members = sorted(group, key=lambda item: str(item[1].get("id")))
            scales.append(
                ViewScale(
                    page=page,
                    side=side,
                    in_per_pt=middle,
                    labels=tuple(str(label.get("id")) for _, label in members),
                    spread_percent=(max(ratios) - min(ratios)) / middle * 100,
                    box=_box_of(label for _, label in members),
                )
            )
    for note in notes:
        value = _note_scale(str(note.get("text") or ""))
        box = note.get("box")
        if value is None or not isinstance(box, Sequence) or len(box) != 4:
            continue
        cx, cy = (float(box[0]) + float(box[2])) / 2, (float(box[1]) + float(box[3])) / 2
        nearest = min(
            (
                s
                for s in scales
                if s.note is None
                and max(s.box[0] - cx, 0, cx - s.box[2]) + max(s.box[1] - cy, 0, cy - s.box[3])
                <= NOTE_REACH_PT
            ),
            key=lambda s: max(s.box[0] - cx, 0, cx - s.box[2])
            + max(s.box[1] - cy, 0, cy - s.box[3]),
            default=None,
        )
        if nearest is not None:
            index = scales.index(nearest)
            scales[index] = ViewScale(
                nearest.page,
                nearest.side,
                nearest.in_per_pt,
                nearest.labels,
                nearest.spread_percent,
                nearest.box,
                str(note.get("text")),
                value,
            )
    checks: list[dict[str, Any]] = []
    for label in labels:
        inches, span = _inches(label), label.get("span_pt")
        if inches is None or inches <= 0 or not isinstance(span, (int, float)) or span <= 0:
            continue
        found = scale_at(scales, label["box"], side=str(label.get("side") or "unknown"))
        if found is None:
            continue
        drawn = float(span) * found.in_per_pt
        checks.append(
            {
                "id": label.get("id"),
                "drawn_in": round(drawn, 2),
                "off_by": round(drawn - float(inches), 2),
                "drawn_to_value": abs(drawn - float(inches))
                <= max(0.25, OFF_TOLERANCE * float(inches)),
            }
        )
    return scales, checks


def scale_at(
    scales: Sequence[ViewScale], box: Sequence[float], *, side: str | None = None
) -> ViewScale | None:
    """The one scale whose box holds `box`'s centre (on `side`, when given), or `None` when none
    or several different ones do."""
    found = [
        scale
        for scale in scales
        if (side is None or scale.side == side) and _inside(box, scale.box)
    ]
    if not found:
        return None
    first = found[0]
    if any(abs(other.in_per_pt / first.in_per_pt - 1) > AGREE for other in found[1:]):
        return None
    return first


def set_scales(dims: Mapping[str, Any]) -> dict[str, Any]:
    """`scales.json` for a whole set, from its `dims.json`."""
    pages: list[dict[str, Any]] = []
    for entry in dims.get("pages") or []:
        if not isinstance(entry, Mapping):
            continue
        labels = [label for label in entry.get("labels") or [] if isinstance(label, Mapping)]
        scales, checks = page_scales(
            int(entry.get("page") or 0), labels, entry.get("scale_notes") or ()
        )
        pages.append(
            {
                "file": entry.get("file"),
                "page": entry.get("page"),
                "scales": [scale.as_dict() for scale in scales],
                "labels": checks,
            }
        )
    return {"version": 1, "pages": pages}


def from_dict(raw: Mapping[str, Any]) -> ViewScale | None:
    try:
        box = tuple(float(v) for v in raw["box"])
        return ViewScale(
            page=int(raw["page"]),
            side=str(raw["side"]),
            in_per_pt=float(raw["in_per_pt"]),
            labels=tuple(str(label) for label in raw.get("from_labels") or ()),
            spread_percent=float(raw.get("spread_percent") or 0.0),
            box=(box[0], box[1], box[2], box[3]),
            note=raw.get("note"),
            note_in_per_pt=raw.get("note_in_per_pt"),
        )
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def scales_on(folder: Path | None, file: str, page: int) -> list[ViewScale]:
    """The scales the set pack measured on one page (`scales.json`), or none."""
    if folder is None:
        return []
    try:
        document = json.loads((folder / SCALES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    for entry in document.get("pages") or [] if isinstance(document, Mapping) else []:
        if isinstance(entry, Mapping) and entry.get("file") == file and entry.get("page") == page:
            found = [
                from_dict(raw) for raw in entry.get("scales") or [] if isinstance(raw, Mapping)
            ]
            return [scale for scale in found if scale is not None]
    return []
