"""The automatic round-2 check (#1603): Fixed / Still wrong / New, value by value, by code.

When the set holds the vendor's next round of a drawing (GV's sheets head each vendor drawing with
its round; `page_map.json` carries it per drawing), the full check's cloud drawing says, for every
round-1 item it clouded, what the vendor did with it in round 2, and marks what round 2 got wrong
that round 1 did not have. Code decides each status from the printed values, never the AI:

1. **Line the rounds up** (`fit`). The numbered dimension labels code reads off each drawing
   (`dims.json`, #1558) are the anchors: values printed exactly once in both drawings, in the same
   direction. A least-squares scale and shift per axis maps round 1 onto round 2. When fewer than
   `MIN_ANCHORS` anchors agree, or the median anchor misses by more than `MAX_FIT_PT`, code
   **refuses** (round 2 is a different view, or redrawn): those items go to the grey list, never a
   guess. Of the round-2 drawings of one area, the one that fits best is used.
2. **Find the item's label in round 1**: the label inside (or by) the finding's vendor box that
   prints the vendor's value; then **its counterpart in round 2**: the nearest round-2 label in the
   same direction within `PAIR_PT` of where the fit puts it.
3. **Decide**:
   - **fixed**: round 2 prints what it should be (the architect's value, code's sum of the parts,
     or a width within GV's limits);
   - **still_wrong**: round 2 prints the same wrong value, or another value that is still not what
     it should be;
   - **not_compared** (grey, with the reason): the fit was refused, the value was not found as a
     printed label, round 2 has nothing at that spot, code does not know the value it should be
     and round 2 changed it, or the drawings' ink (`revision_diff.diff_pages`, when given)
     changed at a value the labels say is unchanged (a new digit alone is below what the ink
     check sees, so unchanged ink never contradicts a changed value).
4. **New**: a round-2 item code proved wrong is **new** when round 1 printed a different value at
   the same place (or none); when round 1 printed the same value it was already wrong there
   (`also_in_round_1`).

Both values are printed text read by code at their spots and the fit is code's arithmetic, so a
Fixed or Still wrong status is code-proved (the same witness rule as the clouds, #1581). Pure code
on `dims.json` and `page_map.json` (numpy for the fit): no PDF opened here, no AI call.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

import numpy as np

from app.review.assistant.chat_trust import inch_words
from app.review.assistant.proof import proof_group, should_be_value, vendor_value

__all__ = [
    "MAX_FIT_PT",
    "MIN_ANCHORS",
    "PAIR_PT",
    "Fit",
    "Label",
    "RoundItem",
    "RoundReport",
    "fit",
    "labels_by_page",
    "round_drawings",
    "round_two",
]

type Box = tuple[float, float, float, float]
type Status = Literal["fixed", "still_wrong", "new", "also_in_round_1", "not_compared"]
#: Zones of changed ink for (round-1 page, its box, round-2 page, its box): each zone's round-1
#: box, or `None` when the ink could not be lined up (`revision_diff.diff_pages`).
type InkZones = Callable[[int, Box, int, Box], list[Box] | None]

#: At least this many anchors, missing by at most this much (median, points), or code refuses.
MIN_ANCHORS: Final = 3
MAX_FIT_PT: Final = 2.0
#: A round-2 label pairs with a round-1 label when it is this close to where the fit puts it.
PAIR_PT: Final = 12.0
#: How far round a finding's vendor box its value's label may be (points).
_BOX_SLACK_PT: Final = 8.0
#: Two values are the same to this many inches (a sixteenth, halved).
_SAME_IN: Final = Decimal(1) / Decimal(32)
#: Ink changed this close to a label counts as a change at that label.
_INK_NEAR_PT: Final = 6.0
_LIMITS: Final = re.compile(r'limits are ([\d/ ]+)" to ([\d/ ]+)"')


@dataclass(frozen=True, slots=True)
class Label:
    """One printed dimension on a drawing (`dims.json`)."""

    file: str
    page: int
    id: str
    text: str
    inches: Decimal | None
    box: Box
    orientation: str

    @property
    def centre(self) -> tuple[float, float]:
        return (self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2


@dataclass(frozen=True, slots=True)
class Fit:
    """Round 1 onto round 2: x2 = sx * x1 + bx, y2 = sy * y1 + by."""

    sx: float
    bx: float
    sy: float
    by: float
    anchors: int
    error_pt: float

    def place(self, point: tuple[float, float]) -> tuple[float, float]:
        return self.sx * point[0] + self.bx, self.sy * point[1] + self.by

    def back(self, point: tuple[float, float]) -> tuple[float, float]:
        return (point[0] - self.bx) / self.sx, (point[1] - self.by) / self.sy


@dataclass(frozen=True, slots=True)
class _Drawing:
    file: str
    page: int
    box: Box
    round: int
    area: str


@dataclass(frozen=True, slots=True)
class RoundItem:
    """What round 2 did with one item."""

    finding_id: str
    status: Status
    item: str
    was: str
    """The round-1 value (as code read it)."""
    now: str | None
    """The round-2 value at the same place, when code found one."""
    should_be: str
    page: int | None
    """Round 2's page (the round-1 item's), or the item's own page (a round-2 item)."""
    spot: Box | None
    """Where to mark it on that page (the round-2 label)."""
    file: str | None
    reason: str | None = None
    """Why it could not be compared (grey), in one plain line."""


@dataclass(slots=True)
class RoundReport:
    items: list[RoundItem] = field(default_factory=list)
    pairs: list[dict[str, Any]] = field(default_factory=list)
    """Each round pair tried: area, pages, anchors, fit error, refused reason."""

    def counts(self) -> dict[str, int]:
        found = Counter(item.status for item in self.items)
        return {key: found.get(key, 0) for key in ("fixed", "still_wrong", "new", "not_compared")}

    def line(self) -> str | None:
        """The cover's summary, or `None` when the set has no round 2 to compare."""
        if not self.items:
            return None
        counts = self.counts()
        line = (
            f"Round 2: {counts['fixed']} fixed, {counts['still_wrong']} still wrong, "
            f"{counts['new']} new"
        )
        if counts["not_compared"]:
            line += f"; {counts['not_compared']} could not be compared by code"
        return line + "."

    def by_finding(self) -> dict[str, RoundItem]:
        return {item.finding_id: item for item in self.items}


# ---- reading dims.json and page_map.json --------------------------------------------------------


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _box(raw: object) -> Box | None:
    if isinstance(raw, Mapping):
        raw = [raw.get(key) for key in ("x0", "y0", "x1", "y1")]
    if not isinstance(raw, Sequence) or len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def labels_by_page(dims: Mapping[str, Any] | None) -> dict[tuple[str, int], list[Label]]:
    """Every printed dimension label of the set, by (file, page)."""
    out: dict[tuple[str, int], list[Label]] = {}
    for page in (dims or {}).get("pages") or []:
        if not isinstance(page, Mapping) or not isinstance(page.get("page"), int):
            continue
        key = (str(page.get("file")), int(page["page"]))
        for raw in page.get("labels") or []:
            box = _box(raw.get("box")) if isinstance(raw, Mapping) else None
            if box is None:
                continue
            out.setdefault(key, []).append(
                Label(
                    key[0],
                    key[1],
                    str(raw.get("id")),
                    str(raw.get("text") or ""),
                    _decimal(raw.get("inches")),
                    box,
                    str(raw.get("orientation") or ""),
                )
            )
    return out


def _area(entry: Mapping[str, Any]) -> str | None:
    area = entry.get("area")
    if not isinstance(area, str) or not area.strip():
        return None
    return f"{entry.get('section') or ''}|{' '.join(area.split()).lower()}"


def round_drawings(page_map: Mapping[str, Any] | None) -> dict[str, list[_Drawing]]:
    """The vendor's drawings that carry a round, by area (GV's typed headings)."""
    out: dict[str, list[_Drawing]] = {}
    for entry in (page_map or {}).get("pages") or []:
        if (
            not isinstance(entry, Mapping)
            or entry.get("skip")
            or not isinstance(entry.get("page"), int)
        ):
            continue
        area = _area(entry)
        if area is None:
            continue
        for half in entry.get("halves") or []:
            if not isinstance(half, Mapping) or half.get("role") != "vendor":
                continue
            box = _box(half.get("box"))
            if box is None or not isinstance(half.get("round"), int):
                continue
            out.setdefault(area, []).append(
                _Drawing(str(entry.get("file")), int(entry["page"]), box, int(half["round"]), area)
            )
    return out


def _inside(point: tuple[float, float], box: Box, slack: float = 0.0) -> bool:
    return (
        box[0] - slack <= point[0] <= box[2] + slack
        and box[1] - slack <= point[1] <= box[3] + slack
    )


def _on(drawing: _Drawing, labels: Mapping[tuple[str, int], list[Label]]) -> list[Label]:
    return [
        label
        for label in labels.get((drawing.file, drawing.page), [])
        if label.inches is not None and _inside(label.centre, drawing.box)
    ]


# ---- the fit -------------------------------------------------------------------------------------


def fit(first: Sequence[Label], second: Sequence[Label]) -> tuple[Fit | None, str | None]:
    """Round 1's labels onto round 2's: the fit, or `None` and why code refuses."""
    once_a = Counter((label.inches, label.orientation) for label in first)
    once_b = Counter((label.inches, label.orientation) for label in second)
    anchors = [
        (a, b)
        for a in first
        for b in second
        if a.inches == b.inches
        and a.orientation == b.orientation
        and once_a[(a.inches, a.orientation)] == 1
        and once_b[(b.inches, b.orientation)] == 1
    ]
    if len(anchors) < MIN_ANCHORS:
        return None, (
            f"too few values printed in both rounds to line them up ({len(anchors)}); "
            "code would not guess"
        )
    xs = np.array([[a.centre[0], 1.0] for a, _b in anchors])
    ys = np.array([[a.centre[1], 1.0] for a, _b in anchors])
    sx, bx = np.linalg.lstsq(xs, np.array([b.centre[0] for _a, b in anchors]), rcond=None)[0]
    sy, by = np.linalg.lstsq(ys, np.array([b.centre[1] for _a, b in anchors]), rcond=None)[0]
    if abs(sx) < 1e-6 or abs(sy) < 1e-6:
        return None, "round 2 does not line up with round 1; code would not guess"
    lined = Fit(float(sx), float(bx), float(sy), float(by), len(anchors), 0.0)
    misses = [float(np.hypot(*np.subtract(lined.place(a.centre), b.centre))) for a, b in anchors]
    error = float(np.median(misses))
    if error > MAX_FIT_PT:
        return None, (
            f"round 2 is a different view or redrawn (its values miss by {error:.0f} pt when "
            "lined up); code would not guess"
        )
    return Fit(lined.sx, lined.bx, lined.sy, lined.by, len(anchors), error), None


def _nearest(point: tuple[float, float], labels: Sequence[Label], orientation: str) -> Label | None:
    best: tuple[float, Label] | None = None
    for label in labels:
        if label.orientation != orientation:
            continue
        distance = float(np.hypot(point[0] - label.centre[0], point[1] - label.centre[1]))
        if distance <= PAIR_PT and (best is None or distance < best[0]):
            best = (distance, label)
    return None if best is None else best[1]


# ---- the items -----------------------------------------------------------------------------------


def _same(a: Decimal | None, b: Decimal | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= _SAME_IN


def _fraction(text: str) -> Decimal | None:
    total = Decimal(0)
    for part in text.split():
        if "/" in part:
            top, _slash, bottom = part.partition("/")
            try:
                total += Decimal(int(top)) / Decimal(int(bottom))
            except (ValueError, ArithmeticError):
                return None
        else:
            value = _decimal(part)
            if value is None:
                return None
            total += value
    return total


def _governs(finding: Mapping[str, Any]) -> tuple[Decimal, Decimal] | None:
    """The range a value must be in to be right: one size (low = high), GV's limits, or `None`
    when code does not know it as numbers."""
    settled = finding.get("settled")
    if isinstance(settled, Mapping) and settled.get("method") == "gv_limits":
        found = _LIMITS.search(str(settled.get("how") or ""))
        if found:
            low, high = _fraction(found.group(1)), _fraction(found.group(2))
            if low is not None and high is not None:
                return low, high
    if finding.get("source") == "vendor_arithmetic":
        summed = _decimal(finding.get("parts_sum"))
        return None if summed is None else (summed, summed)
    value = _decimal((finding.get("inches") or {}).get("architect"))
    return None if value is None else (value, value)


def _right(value: Decimal | None, governs: tuple[Decimal, Decimal] | None) -> bool | None:
    if value is None or governs is None:
        return None
    low, high = governs
    return low - _SAME_IN <= value <= high + _SAME_IN


def _vendor_box(finding: Mapping[str, Any]) -> tuple[str | None, int, Box] | None:
    raw = finding.get("vendor_box")
    box = _box(raw)
    if box is None or not isinstance(raw, Mapping):
        return None
    try:
        page = int(raw["page"])
    except (KeyError, TypeError, ValueError):
        return None
    name = raw.get("file")
    return (None if name is None else str(name)), page, box


def _label_at(finding: Mapping[str, Any], labels: Sequence[Label], box: Box) -> Label | None:
    """The label in (or by) the finding's box that prints the vendor's value (else, when only one
    label is there, that one)."""
    near = [label for label in labels if _inside(label.centre, box, _BOX_SLACK_PT)]
    wanted = _decimal((finding.get("inches") or {}).get("vendor"))
    if wanted is None:
        wanted = _fraction(vendor_value(finding).replace('"', "").strip()) if near else None
    matching = [label for label in near if _same(label.inches, wanted)]
    if len(matching) == 1:
        return matching[0]
    if not matching and len(near) == 1:
        return near[0]
    if matching:
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        return min(matching, key=lambda label: np.hypot(label.centre[0] - cx, label.centre[1] - cy))
    return None


def _drawing_of(file: str, page: int, box: Box, drawings: Sequence[_Drawing]) -> _Drawing | None:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    for drawing in drawings:
        if drawing.file == file and drawing.page == page and _inside((cx, cy), drawing.box, 4.0):
            return drawing
    return None


def _words(value: Decimal | None, text: str) -> str:
    return inch_words(value) if value is not None else text


def _ink_disagrees(
    zones: InkZones | None,
    first: _Drawing,
    second: _Drawing,
    label: Label,
    changed: bool,
) -> bool:
    """The drawings' ink changed at a value the labels say is unchanged (the item may have moved
    or been redrawn there). The ink check groups changes into zones and drops specks, so a new
    digit alone is below what it sees: ink that did not change never contradicts a changed value.
    No ink check (or ink that could not be lined up) says nothing."""
    if zones is None or changed:
        return False
    try:
        found = zones(first.page, first.box, second.page, second.box)
    except Exception:  # noqa: BLE001 - the ink check is a second witness, never a failure
        return False
    if found is None:
        return False
    x0, y0, x1, y1 = label.box
    grown = (x0 - _INK_NEAR_PT, y0 - _INK_NEAR_PT, x1 + _INK_NEAR_PT, y1 + _INK_NEAR_PT)
    return any(
        not (z[2] < grown[0] or z[0] > grown[2] or z[3] < grown[1] or z[1] > grown[3])
        for z in found
    )


def round_two(
    findings: Sequence[Mapping[str, Any]],
    dims: Mapping[str, Any] | None,
    page_map: Mapping[str, Any] | None,
    *,
    zones: InkZones | None = None,
) -> RoundReport:
    """What round 2 did with each round-1 item code clouded, and which round-2 items are new
    (see the module's words). `zones`: the ink check (`revision_diff.diff_pages`), optional."""
    report = RoundReport()
    labels = labels_by_page(dims)
    by_area = round_drawings(page_map)
    fits: dict[tuple[_Drawing, _Drawing], tuple[Fit | None, str | None]] = {}

    def fitted(first: _Drawing, second: _Drawing) -> tuple[Fit | None, str | None]:
        if (first, second) not in fits:
            found, why = fit(_on(first, labels), _on(second, labels))
            fits[(first, second)] = (found, why)
            report.pairs.append(
                {
                    "area": first.area,
                    "round1": {"file": first.file, "page": first.page},
                    "round2": {"file": second.file, "page": second.page},
                    "anchors": None if found is None else found.anchors,
                    "error_pt": None if found is None else round(found.error_pt, 2),
                    "refused": why,
                }
            )
        return fits[(first, second)]

    def best_next(first: _Drawing) -> tuple[_Drawing | None, Fit | None, str | None]:
        later = [d for d in by_area.get(first.area, []) if d.round == first.round + 1]
        if not later:
            return None, None, None
        tried = [(d, *fitted(first, d)) for d in later]
        good = [(d, f) for d, f, _w in tried if f is not None]
        if good:
            drawing, found = min(good, key=lambda pair: pair[1].error_pt)
            return drawing, found, None
        return later[0], None, tried[0][2]

    live = [f for f in findings if proof_group(f) == "clouded" and f.get("status") == "different"]
    main_file = next((str(d.file) for drawings in by_area.values() for d in drawings), None)

    for finding in live:
        where = _vendor_box(finding)
        if where is None:
            continue
        file = where[0] or main_file or ""
        page, box = where[1], where[2]
        drawings = [d for ds in by_area.values() for d in ds]
        drawing = _drawing_of(file, page, box, drawings)
        if drawing is None:
            continue
        item = str(finding.get("item") or "")
        should = should_be_value(finding)
        if drawing.round == 1:
            following = best_next(drawing)
            if following[0] is not None:  # a drawing with no later round says nothing
                report.items.append(
                    _round_one_item(finding, drawing, following, labels, zones, item, should)
                )
            continue
        first_round = [d for d in by_area.get(drawing.area, []) if d.round == drawing.round - 1]
        if first_round:
            report.items.append(
                _round_two_item(finding, drawing, first_round, box, labels, fitted, item, should)
            )
    return report


def _round_two_item(
    finding: Mapping[str, Any],
    drawing: _Drawing,
    first_round: Sequence[_Drawing],
    box: Box,
    labels: Mapping[tuple[str, int], list[Label]],
    fitted: Callable[[_Drawing, _Drawing], tuple[Fit | None, str | None]],
    item: str,
    should: str,
) -> RoundItem:
    """A round-2 item code proved wrong: new when round 1 printed another value (or none) at the
    same place; already there when round 1 printed the same; else not compared (it stays clouded:
    only its round is unknown)."""
    finding_id, value = str(finding.get("id")), vendor_value(finding)
    label = _label_at(finding, _on(drawing, labels), box)
    why = "code did not find the vendor's value as printed text in round 2"
    for first in first_round:
        found, refused = fitted(first, drawing)
        if found is None:
            why = refused or why
            continue
        if label is None:
            break
        back = _nearest(found.back(label.centre), _on(first, labels), label.orientation)
        if back is not None and _same(back.inches, label.inches):
            reason = f"round 1 printed the same {_words(back.inches, back.text)} here"
            status: Status = "also_in_round_1"
        else:
            before = "nothing" if back is None else _words(back.inches, back.text)
            reason, status = f"round 1 printed {before} here", "new"
        return RoundItem(
            finding_id,
            status,
            item,
            value,
            value,
            should,
            drawing.page,
            label.box,
            drawing.file,
            reason,
        )
    return RoundItem(
        finding_id, "not_compared", item, value, value, should, drawing.page, None, None, why
    )


def _round_one_item(
    finding: Mapping[str, Any],
    drawing: _Drawing,
    found: tuple[_Drawing | None, Fit | None, str | None],
    labels: Mapping[tuple[str, int], list[Label]],
    zones: InkZones | None,
    item: str,
    should: str,
) -> RoundItem:
    finding_id = str(finding.get("id"))
    was = vendor_value(finding)
    second, lined, refused = found
    vendor = _vendor_box(finding)
    box = vendor[2] if vendor is not None else drawing.box

    def grey(why: str, page: int | None = None) -> RoundItem:
        return RoundItem(finding_id, "not_compared", item, was, None, should, page, None, None, why)

    if second is None:
        return grey("the set has no round 2 of this drawing")
    if lined is None:
        return grey(refused or "round 2 does not line up with round 1", second.page)
    label = _label_at(finding, _on(drawing, labels), box)
    if label is None:
        return grey("code did not find the vendor's value as printed text in round 1", second.page)
    after = _nearest(lined.place(label.centre), _on(second, labels), label.orientation)
    if after is None:
        return grey("round 2 prints nothing at the same place", second.page)
    now = _words(after.inches, after.text)
    changed = not _same(after.inches, label.inches)
    if _ink_disagrees(zones, drawing, second, label, changed):
        return grey("the drawing's ink and its printed value disagree at this spot", second.page)
    right = _right(after.inches, _governs(finding))
    if right is True:
        status: Status = "fixed"
    elif not changed or right is False:
        status = "still_wrong"
    else:
        return grey(f"round 2 changed it to {now}; code does not know the value it should be")
    return RoundItem(
        finding_id, status, item, was, now, should, second.page, after.box, second.file
    )
