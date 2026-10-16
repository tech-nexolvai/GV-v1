"""Pair every architect dimension with the vendor's dimension line for the same span, by geometry only.

The direct compare puts each number the architect printed beside the vendor's number for the same
span. This module decides **which vendor dimension line is that span**, from where and how long
things are drawn, never from a printed number (pairing by value could only pair numbers that
already agree, so a real difference could never show). **A wrong pair is worse than none**: every
test below must pass, or the dimension is "?" with the reason.

**The rule** (`pair_page`), for each architect dimension on the sheet:

1. **Same kind of view.** The architect's view and the vendor's view must both be named, by their
   printed headings, as the same kind (an elevation with an elevation, a plan with a plan). A view
   whose kind is not printed pairs with nothing.
2. **A printed scale.** The vendor view must print a scale code can read (through the factor it was
   pasted at). A view without one is never stretched to fit.
3. **Real dimension lines only.** A vendor span counts only when its ticks are the drawing's own
   slashes at both ends (not witness lines, which an edge of a cabinet or a stone also gives) and a
   label of its own sits on it: inside its span along the line, within `label_reach_pt` across, and
   nearer to it than to any other dimension line that runs past the label.
4. **Same place along the wall**, measured from a reference both views share, at each view's own
   printed scale (never stretched): for heights the **floor** (the lowest tick that at least
   `floor_lines` height lines, at different places across, end on); for widths the **left end** of
   the dimensioned run, and only when both views' widths run about as far (so their ends line up).
   A vendor line qualifies when both of its ends are where the architect span's ends are, and its
   length is the architect's, each within `length_fraction` (at least `length_floor_in`).
5. **Exactly one** qualifying line is the pair. None or several: "?" with the reason in plain
   words. One vendor line that is the only match for two architect dimensions pairs neither.

Every architect dimension appears **exactly once** in the result, held ones too (a held value or a
label on no line is a "?" with its reason). Nothing is dropped. Pure: plain values in and out.

Source: the direct-compare issues #1417, #1418 · Verification: `tests/extraction/compare/test_pair.py`
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from io import BytesIO
from pathlib import Path
from typing import Final

from extraction.compare.dimensions import ArchitectDimension, Axis, DrawnDimension
from extraction.compare.page import (
    MEASURED_COMPARE_SETTINGS,
    ArchitectViewDimensions,
    ComparePage,
    VendorViewDimensions,
    read_compare_page,
)
from extraction.geometry.rows import Box, TickSource

__all__ = [
    "MEASURED_PAIR_SETTINGS",
    "PairSettings",
    "PairStatus",
    "Pairing",
    "compare_pdf",
    "own_label",
    "pair_page",
]


class PairStatus(StrEnum):
    PAIRED = "paired"
    """Exactly one vendor dimension line draws the same span."""
    UNPAIRED = "unpaired"
    """None or several: shown as "?" with the reason."""


@dataclass(frozen=True, slots=True)
class PairSettings:
    """Every threshold, stated by the caller (no defaults)."""

    length_fraction: Fraction
    """The two real lengths may differ by this fraction of the architect's..."""
    length_floor_in: Fraction
    """...or by this many inches, whichever is larger."""
    label_reach_pt: Decimal
    """A vendor label is on its dimension line when at most this far across from it."""
    tick_pair_pt: Decimal
    """Two ticks this close are one place (a doubled slash)."""
    floor_lines: int
    """A view's floor is the lowest tick at least this many height lines end on."""


MEASURED_PAIR_SETTINGS: Final = PairSettings(
    # Measured $0 on the client's second set: a vendor view's printed scale through the paste
    # factor draws lengths up to about 4% short of what the vendor prints, so 5% (at least half an
    # inch) is the smallest band that still finds the vendor's line for a span both drew alike.
    length_fraction=Fraction(5, 100),
    length_floor_in=Fraction(1, 2),
    # The architect reader's own measured reach for a label off its line.
    label_reach_pt=Decimal(12),
    tick_pair_pt=Decimal(2),
    floor_lines=2,
)


@dataclass(frozen=True, slots=True)
class Pairing:
    """One architect dimension and the vendor line drawn for the same span, or why there is none."""

    page_index: int
    architect_view: int
    """The architect drawing's annotation index on the sheet."""
    architect: ArchitectDimension
    status: PairStatus
    vendor_view: int | None
    """The vendor view's page-local number, when paired."""
    vendor: DrawnDimension | None
    vendor_inches_drawn: Fraction | None
    """The paired line's drawn length at the registered scale (never its printed number)."""
    how: str | None
    """How the vendor view was laid over the architect's, when paired."""
    reason: str
    """In plain words: why this is the pair, or why there is none."""
    vendor_label: Box | None = None
    """The paired line's own label: where the vendor's number is printed."""


def _across(axis: Axis, box: Box) -> tuple[Decimal, Decimal, Decimal]:
    if axis is Axis.HORIZONTAL:
        return box.centre_x, box.top, box.bottom
    return box.centre_y, box.x0, box.x1


def _gap(low: Decimal, high: Decimal, offset: Decimal) -> Decimal:
    if low <= offset <= high:
        return Decimal(0)
    return min(abs(low - offset), abs(high - offset))


def own_label(line: DrawnDimension, lines: Sequence[DrawnDimension], reach: Decimal) -> Box | None:
    """The label that belongs to `line`: inside its span along it, within `reach` across, and
    nearer to it than to any other line of the view that runs past it. `None` when there is none."""
    for box in line.label_boxes:
        along, low, high = _across(line.axis, box)
        if not line.start_pt <= along <= line.end_pt:
            continue
        distance = _gap(low, high, line.offset_pt)
        if distance > reach:
            continue
        if any(
            other is not line
            and other.axis is line.axis
            and other.offset_pt != line.offset_pt
            and other.start_pt <= along <= other.end_pt
            and _gap(low, high, other.offset_pt) <= distance
            for other in lines
        ):
            continue
        return box
    return None


def _real_lines(
    view: VendorViewDimensions, settings: PairSettings
) -> list[tuple[DrawnDimension, Box]]:
    found: list[tuple[DrawnDimension, Box]] = []
    for line in view.lines:
        if line.tick_source is not TickSource.SLASH:
            continue
        label = own_label(line, view.lines, settings.label_reach_pt)
        if label is not None:
            found.append((line, label))
    return found


_Found = tuple[VendorViewDimensions, DrawnDimension, Box, Fraction]


def _reference(
    lines: Sequence[DrawnDimension], axis: Axis, settings: PairSettings
) -> tuple[Fraction, Fraction] | None:
    """The view's shared reference on `axis`, as (where it is, how far the dimensions reach from
    it), in page points: for heights the floor (the lowest tick that at least `floor_lines` height
    lines, at different places across, end on), for widths the left end of the dimensioned run.
    `None` when there is none."""
    on = [line for line in lines if line.axis is axis]
    if not on:
        return None
    if axis is Axis.VERTICAL:
        within = Fraction(settings.tick_pair_pt)
        for floor in sorted({Fraction(line.end_pt) for line in on}, reverse=True):
            resting = {
                Fraction(line.offset_pt)
                for line in on
                if abs(floor - Fraction(line.end_pt)) <= within
            }
            if len(resting) >= settings.floor_lines:
                return floor, floor - min(Fraction(line.start_pt) for line in on)
        return None
    left = min(Fraction(line.start_pt) for line in on)
    return left, max(Fraction(line.end_pt) for line in on) - left


def _from_reference(
    span: DrawnDimension, reference: Fraction, axis: Axis, ppi: Fraction
) -> tuple[Fraction, Fraction]:
    """The span's two ends as real inches from the reference (up from the floor, right of the
    left end)."""
    ends = (Fraction(span.start_pt), Fraction(span.end_pt))
    if axis is Axis.VERTICAL:
        return (reference - ends[1]) / ppi, (reference - ends[0]) / ppi
    return (ends[0] - reference) / ppi, (ends[1] - reference) / ppi


def _near(a: Fraction, b: Fraction, settings: PairSettings) -> bool:
    return abs(a - b) <= max(settings.length_floor_in, settings.length_fraction * abs(a))


def _candidates(
    dim: ArchitectDimension,
    arch_view: ArchitectViewDimensions,
    vendor: Sequence[VendorViewDimensions],
    settings: PairSettings,
) -> tuple[list[_Found], list[str]]:
    assert dim.span is not None and dim.drawn_inches is not None
    assert arch_view.points_per_inch is not None
    axis = dim.span.axis
    arch_ppi = Fraction(arch_view.points_per_inch)
    found: list[_Found] = []
    notes: list[str] = []
    arch_ref = _reference(arch_view.lines, axis, settings)
    where = (
        "floor that two of its heights rest on" if axis is Axis.VERTICAL else "widths to line up"
    )
    if arch_ref is None:
        return [], [f"the architect's view shows no {where}"]
    a0, a1 = _from_reference(dim.span, arch_ref[0], axis, arch_ppi)
    for view in vendor:
        if view.kind != arch_view.kind:
            kind = "of no printed kind" if view.kind is None else f"a {view.kind}"
            notes.append(f"vendor view {view.number} is {kind}")
            continue
        if view.points_per_inch is None:
            notes.append(f"vendor view {view.number} prints no scale")
            continue
        vendor_ppi = Fraction(view.points_per_inch)
        real = _real_lines(view, settings)
        lines = [line for line, _label in real]
        ref = _reference(lines, axis, settings)
        if ref is None:
            notes.append(f"vendor view {view.number} shows no {where}")
            continue
        if axis is Axis.HORIZONTAL and not _near(
            arch_ref[1] / arch_ppi, ref[1] / vendor_ppi, settings
        ):
            notes.append(
                f"vendor view {view.number}'s widths do not run as far as the architect's, so "
                "their ends cannot be lined up"
            )
            continue
        for line, label in real:
            if line.axis is not axis:
                continue
            v0, v1 = _from_reference(line, ref[0], axis, vendor_ppi)
            if (
                _near(a0, v0, settings)
                and _near(a1, v1, settings)
                and _near(dim.drawn_inches, v1 - v0, settings)
            ):
                found.append((view, line, label, v1 - v0))
    return found, notes


def _one(
    page_index: int,
    arch_view: ArchitectViewDimensions,
    dim: ArchitectDimension,
    vendor: Sequence[VendorViewDimensions],
    settings: PairSettings,
) -> Pairing:
    def unpaired(reason: str) -> Pairing:
        return Pairing(
            page_index,
            arch_view.annotation_index,
            dim,
            PairStatus.UNPAIRED,
            None,
            None,
            None,
            None,
            reason,
        )

    if dim.span is None:
        return unpaired(dim.placement_reason)
    if dim.inches is None:
        return unpaired(dim.held_reason or "the architect's number is held")
    if dim.drawn_inches is None or arch_view.points_per_inch is None:
        return unpaired("the architect view's scale is not known, so its drawn length is not")
    if not vendor:
        return unpaired("no drawing on this sheet is named the vendor's")
    if arch_view.kind is None:
        return unpaired(
            "the sheet does not print what kind of view the architect's drawing is (an elevation "
            "or a plan), so no vendor view can be matched to it"
        )
    found, notes = _candidates(dim, arch_view, vendor, settings)
    if len(found) == 1:
        view, line, label, real = found[0]
        return Pairing(
            page_index,
            arch_view.annotation_index,
            dim,
            PairStatus.PAIRED,
            view.number,
            line,
            real,
            (
                "lined up on the floor"
                if dim.span.axis is Axis.VERTICAL
                else "lined up on the run's ends"
            ),
            f"the only vendor {line.axis.value} dimension line drawn at the same place and length",
            label,
        )
    if found:
        return unpaired(
            f"{len(found)} vendor {dim.span.axis.value} dimension lines are drawn at this place "
            "and length, so which one is the same span is not known"
        )
    if notes and (len(notes) == len(vendor) or notes[0].startswith("the architect's")):
        return unpaired("; ".join(notes))
    return unpaired(
        f"the vendor draws no labelled {dim.span.axis.value} dimension line at this place and "
        "length"
    )


def pair_page(page: ComparePage, settings: PairSettings) -> tuple[Pairing, ...]:
    """Every architect dimension on the sheet exactly once, paired or with the reason it is not.

    A vendor line that is the only match for two or more architect dimensions pairs with none of
    them: which architect span it draws is not known."""
    first = [
        _one(page.page_index, arch_view, dim, page.vendor, settings)
        for arch_view in page.architect
        for dim in arch_view.dimensions
    ]
    uses = Counter(_line_key(p) for p in first if p.status is PairStatus.PAIRED)
    return tuple(
        (
            replace(
                p,
                status=PairStatus.UNPAIRED,
                vendor_view=None,
                vendor=None,
                vendor_inches_drawn=None,
                how=None,
                vendor_label=None,
                reason=(
                    f"one vendor dimension line is the only match for {uses[_line_key(p)]} of the "
                    "architect's dimensions, so which span it draws is not known"
                ),
            )
            if p.status is PairStatus.PAIRED and uses[_line_key(p)] > 1
            else p
        )
        for p in first
    )


def _line_key(p: Pairing) -> tuple[int | None, str, Fraction, Fraction, Fraction]:
    assert p.vendor is not None
    return (
        p.vendor_view,
        p.vendor.axis.value,
        Fraction(p.vendor.offset_pt),
        Fraction(p.vendor.start_pt),
        Fraction(p.vendor.end_pt),
    )


def compare_pdf(
    pdf: bytes | Path, *, dpi: int = 150, settings: PairSettings = MEASURED_PAIR_SETTINGS
) -> tuple[tuple[ComparePage, tuple[Pairing, ...]], ...]:
    """Every page of a combined-sheet PDF read and paired, with no database: for each page with an
    architect view, the page and every architect dimension on it exactly once."""
    import pdfplumber

    data = pdf.read_bytes() if isinstance(pdf, Path) else pdf
    with pdfplumber.open(BytesIO(data)) as document:
        count = len(document.pages)
    result: list[tuple[ComparePage, tuple[Pairing, ...]]] = []
    for index in range(count):
        page = read_compare_page(data, index, settings=MEASURED_COMPARE_SETTINGS, dpi=dpi)
        if page.architect:
            result.append((page, pair_page(page, settings)))
    return tuple(result)
