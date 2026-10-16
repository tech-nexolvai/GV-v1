"""Pair every architect dimension with the vendor's dimension line for the same span, by geometry only.

The direct compare puts each number the architect printed beside the vendor's number for the same
span. This module decides **which vendor dimension line is that span**, from where and how long
things are drawn, never from a printed number (pairing by value could only pair numbers that
already agree, so a real difference could never show).

**The rule** (`pair_page`), for each architect dimension on the sheet:

* its real length is the length drawn between its ticks through the architect view's scale;
* a vendor dimension line qualifies when it runs on the same axis, on the same sheet, and its own
  real length (drawn length through the vendor view's printed scale) equals the architect's within
  `length_fraction` of it or `length_floor_in`, whichever is larger;
* a vendor view that prints no scale is laid over the architect's view by `register.py` (its ticks
  lining up); there a line qualifies when it lands on the architect's span and has the same real
  length at the registered scale. A view that does not register gives no candidates, and says so;
* **exactly one** qualifying line is the pair. None or several: "?" with the reason in plain words.

Every architect dimension appears **exactly once** in the result, held ones too (a held value or a
label on no line is a "?" with its reason). Nothing is dropped. Pure: plain values in and out.

Source: the direct-compare issue · Verification: `tests/extraction/compare/test_pair.py`
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from fractions import Fraction
from io import BytesIO
from pathlib import Path
from typing import Final

from extraction.compare.dimensions import ArchitectDimension, DrawnDimension
from extraction.compare.page import (
    MEASURED_COMPARE_SETTINGS,
    ArchitectViewDimensions,
    ComparePage,
    VendorViewDimensions,
    read_compare_page,
)
from extraction.compare.register import RegisterSettings, register, vendor_candidates

__all__ = [
    "MEASURED_PAIR_SETTINGS",
    "PairSettings",
    "PairStatus",
    "Pairing",
    "compare_pdf",
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
    register: RegisterSettings
    """How a vendor view with no printed scale is laid over the architect's."""


MEASURED_PAIR_SETTINGS: Final = PairSettings(
    # Measured $0 on the client's second set: a vendor view's printed scale through the paste
    # factor draws lengths up to about 4% short of what the vendor prints, so 5% (at least half an
    # inch) is the smallest band that still finds the vendor's line for a span both drew alike.
    length_fraction=Fraction(5, 100),
    length_floor_in=Fraction(1, 2),
    register=RegisterSettings(
        tick_tolerance_in=Fraction(1),
        minimum_coinciding_ticks=4,
        minimum_margin=2,
        stretch_range=(Fraction(1, 4), Fraction(4)),
        stretch_agreement=Fraction(5, 100),
        candidate_fraction=Fraction(5, 100),
        extent_tolerance_in=Fraction(2),
    ),
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
    """The paired line's drawn length through the vendor view's scale (never its printed number)."""
    how: str | None
    """"printed scale" or "registered": how the vendor view's scale was known."""
    reason: str
    """In plain words: why this is the pair, or why there is none."""


def _qualifies(
    arch_inches: Fraction, line: DrawnDimension, ppi: Fraction, settings: PairSettings
) -> Fraction | None:
    real = Fraction(line.length_pt) / ppi
    allowed = max(settings.length_floor_in, settings.length_fraction * arch_inches)
    return real if abs(real - arch_inches) <= allowed else None


def _candidates(
    dim: ArchitectDimension,
    arch_view: ArchitectViewDimensions,
    vendor: Sequence[VendorViewDimensions],
    settings: PairSettings,
) -> tuple[list[tuple[VendorViewDimensions, DrawnDimension, Fraction, str]], list[str]]:
    assert dim.span is not None and dim.drawn_inches is not None
    found: list[tuple[VendorViewDimensions, DrawnDimension, Fraction, str]] = []
    notes: list[str] = []
    for view in vendor:
        if view.points_per_inch is not None:
            for line in view.lines:
                if line.axis is not dim.span.axis:
                    continue
                real = _qualifies(dim.drawn_inches, line, view.points_per_inch, settings)
                if real is not None:
                    found.append((view, line, real, "printed scale"))
            continue
        if arch_view.points_per_inch is None or not view.lines:
            continue
        arch_ppi = Fraction(arch_view.points_per_inch)
        laid = register(
            arch_view.lines, view.lines, arch_points_per_inch=arch_ppi, settings=settings.register
        )
        placed = laid.on(dim.span.axis)
        if placed is None:
            notes.append(f"vendor view {view.number} prints no scale and does not line up")
            continue
        near = vendor_candidates(
            dim.span,
            view.lines,
            laid,
            arch_points_per_inch=arch_ppi,
            settings=settings.register,
        )
        vendor_ppi = arch_ppi * placed.stretch
        for candidate in near.found:
            real = _qualifies(dim.drawn_inches, candidate.span, vendor_ppi, settings)
            if real is not None:
                found.append((view, candidate.span, real, "registered"))
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
    if dim.drawn_inches is None:
        return unpaired("the architect view's scale is not known, so its drawn length is not")
    if not vendor:
        return unpaired("no drawing on this sheet is named the vendor's")
    found, notes = _candidates(dim, arch_view, vendor, settings)
    if len(found) == 1:
        view, line, real, how = found[0]
        return Pairing(
            page_index,
            arch_view.annotation_index,
            dim,
            PairStatus.PAIRED,
            view.number,
            line,
            real,
            how,
            f"the only vendor {line.axis.value} dimension drawn this long",
        )
    if found:
        return unpaired(
            f"{len(found)} vendor {dim.span.axis.value} dimensions are drawn this long, "
            "so which one is the same span is not known"
        )
    if notes and all(view.points_per_inch is None for view in vendor):
        return unpaired("; ".join(notes))
    return unpaired(f"the vendor draws no {dim.span.axis.value} dimension this long")


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
