"""Which drawing on a combined sheet is the architect's: two judgments that must agree (#1052).

A combined sheet pastes two drawings on one page: the architect's (the ID set) and the vendor's. A
reading from the wrong one, checked as the other's, could pass a vendor's wrong drawing on the
architect's right number. So the role is decided by code only when **two independent judgments**
say the same thing (decision D2, 2026-10-09):

* **(a) the heading** the sheet prints above the drawing — `ID SET ELEVATION` or `VENDOR'S SHOP
  DRAWING ELEVATION` — read exactly (`extraction/panels.py`);
* **(b) the drawing's own content**: how its dimensions are written and the scale it prints. The
  architect writes feet and inches (`4' - 2"`) and an architectural scale (`1/4" = 1'-0"`); the
  vendor on the client's sheets writes inches or millimetres with inches in brackets (`457 [18]`)
  and a ratio scale (`1:10`).

Both say the same role → that role, decided by code. Either is silent, or they differ → no role,
with the reason, and a person confirms as before. **Nothing is decided where either judgment is
silent.**

**How (b) decides, and why it is strict.** A drawing is the architect's by its content only when it
prints an architectural scale and no ratio scale, *and* its feet-and-inches labels outnumber its
vendor-style labels. Feet-and-inches labels alone are not enough: on the client's set a vendor's
drawing reprints the architect's feet-and-inches row (and prints no scale), so labels without a
scale leave (b) silent. A drawing is the vendor's by its content when it prints a ratio scale and no
architectural one, and its feet-and-inches labels do not outnumber its vendor-style ones; or, with
no scale at all, when it has at least two vendor-style labels and no feet-and-inches label.
Inch-only labels under a foot (`11"`) are written by both and count for neither.

**A page with no headings at all** (the client's first set prints none) is decided by the content of
its drawings alone, only when it is clear on both sides (`decide_without_headings`): exactly one
drawing is clearly the architect's — feet-and-inches labels outnumbering the vendor-style ones,
**and** an architectural scale, printed in it or (when it prints no scale at all) the scale its own
labels are drawn to, through the factor it was pasted at, being a standard architectural one
(`drawn_architectural_scale`); at least one other drawing is clearly the vendor's by (b); and every
other drawing is neither (no role, labels not leaning to feet and inches). Then both roles are code's,
recorded as decided by "the content of both drawings". Anything else: no role, a person decides.

Pure: plain values in, plain values out. Source: issue #1052 · Verification:
`tests/extraction/architect/test_views.py`
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from typing import Final

from extraction.architect.text import PrintedDimension, ScaleKind, ScaleNote
from extraction.panels import PanelRoleProposal
from units.normalise import plain_marks

__all__ = [
    "ContentJudgment",
    "LabelCounts",
    "Role",
    "ViewJudgment",
    "count_labels",
    "decide_without_headings",
    "drawn_architectural_scale",
    "judge_content",
    "judge_view",
]

#: The architectural scales an architect draws to, as paper per real length, with how each is
#: written: `x" = 1'-0"` for x from 1/16" to 3".
ARCHITECTURAL_SCALES: Final = {
    Fraction(1, 192): '1/16" = 1\'-0"',
    Fraction(1, 128): '3/32" = 1\'-0"',
    Fraction(1, 96): '1/8" = 1\'-0"',
    Fraction(1, 64): '3/16" = 1\'-0"',
    Fraction(1, 48): '1/4" = 1\'-0"',
    Fraction(1, 32): '3/8" = 1\'-0"',
    Fraction(1, 24): '1/2" = 1\'-0"',
    Fraction(1, 16): '3/4" = 1\'-0"',
    Fraction(1, 12): '1" = 1\'-0"',
    Fraction(1, 8): '1 1/2" = 1\'-0"',
    Fraction(1, 4): '3" = 1\'-0"',
}

_POINTS_PER_INCH: Final = Decimal(72)

#: A millimetre value with its inches in brackets, `457 [18]`, or the bracketed inches alone.
_MM_BRACKET: Final = re.compile(r"\[\s*\d[\d\s/\-]*\]")

#: Inch-only labels at or over a foot are the vendor's way; the architect writes feet.
_A_FOOT: Final = Fraction(12)


class Role(StrEnum):
    ARCH = "arch"
    SHOP = "shop"


@dataclass(frozen=True, slots=True)
class LabelCounts:
    """How a drawing's own text writes its dimensions."""

    feet_and_inches: int
    vendor_style: int
    """Millimetres with bracketed inches, and inch-only labels of a foot or more."""
    small_inches: int
    """Inch-only labels under a foot: written by both, counted for neither."""
    architectural_scales: tuple[str, ...]
    ratio_scales: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContentJudgment:
    role: Role | None
    reason: str
    counts: LabelCounts
    labels_lean: Role | None
    """Which way the labels alone point, scale ignored — reported, never decisive."""


@dataclass(frozen=True, slots=True)
class ViewJudgment:
    """Both judgments for one drawing, and the role they agree on, or why there is none."""

    annotation_index: int
    heading_role: Role | None
    heading: str | None
    heading_reason: str
    content: ContentJudgment
    agreed: Role | None
    reason: str
    by_content_alone: bool = False
    """The role was decided on a page with no headings, by the content of both drawings."""


def count_labels(
    dimensions: Sequence[PrintedDimension], phrases: Sequence[str], scales: Sequence[ScaleNote]
) -> LabelCounts:
    """Count a drawing's labels by how they are written.

    `dimensions` are the labels `text.find_printed` found in the drawing (held ones included: how a
    label is written does not depend on whether its number may be used); `phrases` every phrase of
    text in it, for the bracketed millimetre labels the dimension pattern does not take.
    """
    feet = sum(1 for dimension in dimensions if dimension.is_feet_and_inches)
    big_inches = 0
    small = 0
    for dimension in dimensions:
        if dimension.is_feet_and_inches:
            continue
        value = dimension.reading.printed_inches
        if value is None or _MM_BRACKET.search(plain_marks(dimension.phrase)):
            continue
        if value >= _A_FOOT:
            big_inches += 1
        else:
            small += 1
    brackets = sum(len(_MM_BRACKET.findall(plain_marks(phrase))) for phrase in phrases)
    return LabelCounts(
        feet_and_inches=feet,
        vendor_style=brackets + big_inches,
        small_inches=small,
        architectural_scales=tuple(
            note.text for note in scales if note.kind is ScaleKind.ARCHITECTURAL
        ),
        ratio_scales=tuple(note.text for note in scales if note.kind is ScaleKind.RATIO),
    )


def judge_content(counts: LabelCounts) -> ContentJudgment:
    """Judgment (b): what the drawing's own text says it is, or silence with the reason."""
    feet, vendor = counts.feet_and_inches, counts.vendor_style
    architectural, ratio = bool(counts.architectural_scales), bool(counts.ratio_scales)
    lean: Role | None = None
    if feet > vendor:
        lean = Role.ARCH
    elif vendor > feet:
        lean = Role.SHOP
    tally = f"{feet} feet-and-inches label(s), {vendor} inch or millimetre label(s)"
    if architectural and ratio:
        return ContentJudgment(
            None, f"it prints both an architectural and a ratio scale; {tally}", counts, lean
        )
    if architectural:
        if feet >= 1 and feet > vendor:
            return ContentJudgment(
                Role.ARCH,
                f"it prints an architectural scale ({counts.architectural_scales[0]}) and its "
                f"labels are feet and inches ({tally})",
                counts,
                lean,
            )
        return ContentJudgment(
            None,
            f"it prints an architectural scale but its labels are not mostly feet and inches "
            f"({tally})",
            counts,
            lean,
        )
    if ratio:
        if feet <= vendor:
            return ContentJudgment(
                Role.SHOP,
                f"it prints a ratio scale ({counts.ratio_scales[0]}) and its labels are not "
                f"feet and inches ({tally})",
                counts,
                lean,
            )
        return ContentJudgment(
            None,
            f"it prints a ratio scale but its labels are mostly feet and inches ({tally})",
            counts,
            lean,
        )
    if feet == 0 and vendor >= 2:
        return ContentJudgment(
            Role.SHOP,
            f"it prints no scale and its labels are inches or millimetres ({tally})",
            counts,
            lean,
        )
    if feet > vendor:
        return ContentJudgment(
            None,
            f"its labels are feet and inches but it prints no scale, and a vendor may reprint the "
            f"architect's labels ({tally})",
            counts,
            lean,
        )
    return ContentJudgment(None, f"its own text does not say ({tally}, no scale)", counts, lean)


def judge_view(proposal: PanelRoleProposal, content: ContentJudgment) -> ViewJudgment:
    """Both judgments for one drawing, and the role only when they agree."""
    heading_role = None if proposal.role is None else Role(proposal.role)
    if heading_role is None:
        agreed, reason = None, f"the heading says nothing: {proposal.reason}"
    elif content.role is None:
        agreed, reason = None, f"the drawing's content says nothing: {content.reason}"
    elif heading_role is not content.role:
        agreed = None
        reason = (
            f"the heading says {heading_role.value} but the content says {content.role.value} "
            f"({content.reason})"
        )
    else:
        agreed = heading_role
        reason = (
            f"the heading {proposal.heading!r} and the drawing's content agree: {content.reason}"
        )
    return ViewJudgment(
        annotation_index=proposal.annotation_index,
        heading_role=heading_role,
        heading=proposal.heading,
        heading_reason=proposal.reason,
        content=content,
        agreed=agreed,
        reason=reason,
    )


def drawn_architectural_scale(
    points_per_inch: Decimal | None, paste: Decimal | None, agreement: Decimal
) -> str | None:
    """The architectural scale a drawing is drawn to, from the page points per real inch its own
    witnessed labels give and the factor it was pasted at; `None` when either is unknown or no
    standard architectural scale is within `agreement` (a fraction) of it."""
    if points_per_inch is None or paste is None or paste <= 0 or points_per_inch <= 0:
        return None
    paper_per_real = points_per_inch / paste / _POINTS_PER_INCH
    for scale, written in ARCHITECTURAL_SCALES.items():
        exact = Decimal(scale.numerator) / Decimal(scale.denominator)
        if abs(paper_per_real - exact) <= agreement * exact:
            return written
    return None


def _clearly_architect(view: ViewJudgment, drawn: str | None) -> str | None:
    """Why the drawing is clearly the architect's by its content, or `None`."""
    content = view.content
    if content.role is Role.ARCH:
        return content.reason
    counts = content.counts
    if (
        drawn is not None
        and not counts.architectural_scales
        and not counts.ratio_scales
        and counts.feet_and_inches > counts.vendor_style
    ):
        return (
            f"it prints no scale, but its {counts.feet_and_inches} feet-and-inches labels "
            f"(against {counts.vendor_style} inch or millimetre labels) are drawn to {drawn}"
        )
    return None


def decide_without_headings(
    views: Sequence[ViewJudgment], drawn_scales: Mapping[int, str | None]
) -> tuple[ViewJudgment, ...]:
    """Both roles on a page that prints no heading at all, from the content of both drawings.

    `drawn_scales` gives, by annotation index, the architectural scale each drawing's labels are
    drawn to (`drawn_architectural_scale`), where known. Returns `views` unchanged unless every
    drawing is headless, exactly one is clearly the architect's, at least one other is clearly the
    vendor's, and every remaining drawing is neither and does not lean to feet and inches.
    """
    if any(view.heading_role is not None or view.heading is not None for view in views):
        return tuple(views)
    architect = {
        view.annotation_index: why
        for view in views
        for why in [_clearly_architect(view, drawn_scales.get(view.annotation_index))]
        if why is not None
    }
    vendor = {
        view.annotation_index
        for view in views
        if view.annotation_index not in architect and view.content.role is Role.SHOP
    }
    others = [
        view
        for view in views
        if view.annotation_index not in architect and view.annotation_index not in vendor
    ]
    if (
        len(architect) != 1
        or not vendor
        or any(
            view.content.role is not None or view.content.labels_lean is Role.ARCH
            for view in others
        )
    ):
        return tuple(views)
    ((architect_index, architect_why),) = architect.items()
    vendor_why = "; ".join(view.content.reason for view in views if view.annotation_index in vendor)
    decided: list[ViewJudgment] = []
    for view in views:
        if view.annotation_index == architect_index:
            role: Role | None = Role.ARCH
        elif view.annotation_index in vendor:
            role = Role.SHOP
        else:
            decided.append(view)
            continue
        decided.append(
            replace(
                view,
                agreed=role,
                by_content_alone=True,
                reason=(
                    "no heading is printed on this page; decided by the content of both drawings: "
                    f"the architect's because {architect_why}; the vendor's because {vendor_why}"
                ),
            )
        )
    return tuple(decided)
