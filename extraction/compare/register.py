"""Lay the architect's view over the vendor's, and list the vendor dimensions that could measure each architect span.

The architect and the vendor draw the same wall at different scales and in different places on the
sheet. Before any number is compared, code works out **where each architect span lands on the
vendor's drawing**, from where things are drawn, never from the printed numbers (pairing by value is
circular: it could only pair numbers that already agree, so a real difference could never show).

**Registration** (`register`), one axis at a time. A guess is "architect span `a` and vendor span `v`
draw the same thing": that fixes the stretch `k` (vendor points per architect point) and the shift.
Each guess is scored by how many of the architect's tick positions then land on a vendor tick within
`tick_tolerance_in` real inches. The best guess counts only when it has at least
`minimum_coinciding_ticks` and beats every guess that disagrees with it (another stretch, or a shift
that lands the architect's outermost ticks more than the tolerance away) by `minimum_margin`. When both axes register, their stretches must
agree within `stretch_agreement` (one drawing has one scale): a third witness. When only one axis
registers, the other axis uses its stretch and needs only a shift.

**Candidates** (`vendor_candidates`). An architect span, laid over the vendor's drawing, lands at
`[m0, m1]`. Every vendor span on the same axis whose two ticks are each within
`max(tick_tolerance_in, candidate_fraction × the architect's length)` of `m0` and `m1` is a
candidate; nearest first. The vendor may draw the span a little differently (that is what the
compare is for), so the window is wider than the registration's. `pair.py` keeps only those
drawn as long as the architect's span, and pairs only when exactly one is left.
When none fits but both ends land on vendor ticks of one line with pieces between, the reason says
the vendor prints only the pieces.

Floats are never used: every comparison is on exact `Fraction`s of page points.

Pure: stdlib only, no database, no models. Source: the direct-compare issue · Verification:
`tests/extraction/compare/test_pair.py`
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction

from extraction.compare.dimensions import Axis, DrawnDimension

__all__ = [
    "AxisRegistration",
    "Candidate",
    "Candidates",
    "RegisterSettings",
    "Registration",
    "register",
    "vendor_candidates",
]


@dataclass(frozen=True, slots=True)
class RegisterSettings:
    """Every threshold, stated by the caller (no defaults)."""

    tick_tolerance_in: Fraction
    """Two ticks coincide when this close, in real inches at the architect's scale."""
    minimum_coinciding_ticks: int
    minimum_margin: int
    """The best guess must beat every disagreeing guess by this many coinciding ticks."""
    stretch_range: tuple[Fraction, Fraction]
    """The vendor's points per architect point must lie in this range."""
    stretch_agreement: Fraction
    """The two axes' stretches may differ by this fraction of the larger."""
    candidate_fraction: Fraction
    """A candidate's ticks may be off by this fraction of the architect's length."""
    extent_tolerance_in: Fraction
    """The outermost ticks of the two views land on each other when this close, in real inches."""


@dataclass(frozen=True, slots=True)
class AxisRegistration:
    axis: Axis
    stretch: Fraction
    """Vendor page points per architect page point."""
    shift: Fraction
    """vendor = stretch × architect + shift, in page points along the axis."""
    coinciding: int
    runner_up: int
    """The best disagreeing guess's coinciding ticks."""
    method: str
    """What laid it over: "ticks", "ticks and middles", or "outer ends"."""


@dataclass(frozen=True, slots=True)
class Registration:
    horizontal: AxisRegistration | None
    vertical: AxisRegistration | None
    reason: str
    """In plain words: what lined up, or why nothing did."""

    def on(self, axis: Axis) -> AxisRegistration | None:
        return self.horizontal if axis is Axis.HORIZONTAL else self.vertical


@dataclass(frozen=True, slots=True)
class Candidate:
    span: DrawnDimension
    off_in: Fraction
    """The larger of its two ticks' distances from where the architect's land, in real inches."""


@dataclass(frozen=True, slots=True)
class Candidates:
    found: tuple[Candidate, ...]
    reason: str


def _ends(spans: Sequence[DrawnDimension]) -> list[Fraction]:
    return sorted({Fraction(end) for span in spans for end in (span.start_pt, span.end_pt)})


def _near(sorted_ends: Sequence[Fraction], at: Fraction, within: Fraction) -> bool:
    low = bisect_left(sorted_ends, at - within)
    return low < bisect_right(sorted_ends, at + within)


def _score(
    arch_ends: Sequence[Fraction],
    vendor_ends: Sequence[Fraction],
    stretch: Fraction,
    shift: Fraction,
    within: Fraction,
) -> int:
    return sum(1 for end in arch_ends if _near(vendor_ends, stretch * end + shift, within))


def _centre_error(
    stretch: Fraction,
    shift: Fraction,
    a_ends: Sequence[Fraction],
    v_ends: Sequence[Fraction],
) -> Fraction:
    """How far, in vendor page points, the middle of the architect's dimensioned extent lands from
    the middle of the vendor's."""
    return abs(stretch * (a_ends[0] + a_ends[-1]) / 2 + shift - (v_ends[0] + v_ends[-1]) / 2)


def _axis(
    axis: Axis,
    arch: Sequence[DrawnDimension],
    vendor: Sequence[DrawnDimension],
    arch_ppi: Fraction,
    settings: RegisterSettings,
    stretch_fixed: Fraction | None = None,
) -> tuple[AxisRegistration | None, str]:
    a_spans = [span for span in arch if span.axis is axis]
    v_spans = [span for span in vendor if span.axis is axis]
    if not a_spans or not v_spans:
        whose = "the architect" if not a_spans else "the vendor"
        return None, f"no {axis.value} dimensions are drawn in {whose}'s view"
    a_ends, v_ends = _ends(a_spans), _ends(v_spans)
    low, high = settings.stretch_range
    guesses: dict[tuple[Fraction, Fraction], tuple[int, Fraction]] = {}
    for a in a_spans:
        for v in v_spans:
            stretch = (
                stretch_fixed
                if stretch_fixed is not None
                else Fraction(v.length_pt) / Fraction(a.length_pt)
            )
            if not low <= stretch <= high:
                continue
            within = settings.tick_tolerance_in * arch_ppi * stretch
            for a_end, v_end in ((a.start_pt, v.start_pt), (a.end_pt, v.end_pt)):
                shift = Fraction(v_end) - stretch * Fraction(a_end)
                key = (stretch, shift)
                if key not in guesses:
                    guesses[key] = (
                        _score(a_ends, v_ends, stretch, shift, within),
                        _centre_error(stretch, shift, a_ends, v_ends) / (arch_ppi * stretch),
                    )
    if not guesses:
        return None, f"no {axis.value} guess fits the allowed scale range"
    ranked = sorted(
        guesses.items(), key=lambda item: (-item[1][0], item[1][1], item[0][0], item[0][1])
    )
    (best_stretch, best_shift), (best, best_extent) = ranked[0]
    within = settings.tick_tolerance_in * arch_ppi * best_stretch
    rival, rival_extent = 0, Fraction(0)
    for (stretch, shift), (score, extent) in ranked[1:]:
        if (
            abs((stretch - best_stretch) * a_ends[0] + shift - best_shift) <= within
            and abs((stretch - best_stretch) * a_ends[-1] + shift - best_shift) <= within
        ):
            continue  # the same lining-up seen from a neighbouring tick: both ends land alike
        rival, rival_extent = score, extent
        break
    if best < settings.minimum_coinciding_ticks:
        return None, (
            f"the two views' {axis.value} ticks do not line up: at best {best} coincide, "
            f"{settings.minimum_coinciding_ticks} are needed"
        )
    clear_lead = best - rival >= settings.minimum_margin
    # A repeating drawing (equal cabinets) lines up equally well shifted by one bay: then the way
    # that keeps the two drawings' middles clearly closer together counts.
    extent_decides = best_extent + settings.extent_tolerance_in <= rival_extent
    if not (clear_lead or extent_decides):
        return None, (
            f"the two views' {axis.value} ticks line up two ways equally well ({best} and "
            f"{rival} coinciding), and where the drawings' middles fall does not say which is right"
        )
    how = "ticks" if clear_lead else "ticks and middles"
    return AxisRegistration(axis, best_stretch, best_shift, best, rival, how), (
        f"{best} {axis.value} ticks line up (the next best way: {rival})"
        + ("" if clear_lead else "; where the drawings' middles fall decides between them")
    )


def _by_extent(
    axis: Axis,
    arch: Sequence[DrawnDimension],
    vendor: Sequence[DrawnDimension],
    arch_ppi: Fraction,
    stretch: Fraction,
    settings: RegisterSettings,
) -> tuple[AxisRegistration | None, str]:
    """The axis laid over by the middles of the two dimensioned extents, at a stretch already
    fixed, when the two extents are about as long (within `extent_tolerance_in` or the candidate
    fraction of the architect's, whichever is larger)."""
    a_ends = _ends([span for span in arch if span.axis is axis])
    v_ends = _ends([span for span in vendor if span.axis is axis])
    if not a_ends or not v_ends:
        return None, f"no {axis.value} dimensions to lay over"
    shift = (v_ends[0] + v_ends[-1]) / 2 - stretch * (a_ends[0] + a_ends[-1]) / 2
    a_length = (a_ends[-1] - a_ends[0]) / arch_ppi
    v_length = (v_ends[-1] - v_ends[0]) / (arch_ppi * stretch)
    allowed = max(settings.extent_tolerance_in, settings.candidate_fraction * a_length)
    if abs(a_length - v_length) > allowed:
        return None, (
            f"the {axis.value} dimensions of the two views do not cover about the same length "
            f"at the known scale (about {round(a_length)} in against {round(v_length)} in)"
        )
    return AxisRegistration(axis, stretch, shift, 0, 0, "middles"), (
        f"the {axis.value} dimensions of the two views cover about the same length at the known "
        "scale, laid over by their middles"
    )


def register(
    arch: Sequence[DrawnDimension],
    vendor: Sequence[DrawnDimension],
    *,
    arch_points_per_inch: Fraction,
    vendor_points_per_inch: Fraction | None = None,
    settings: RegisterSettings,
) -> Registration:
    """Where the architect's view lands on the vendor's, axis by axis, or why it does not.

    `vendor_points_per_inch`, when the vendor's scale is known (its printed scale note through the
    paste factor, or its sealed readings), fixes the stretch on both axes, so only the shifts are
    found from the ticks: far steadier when the architect prints only a few dimensions."""
    fixed = (
        None
        if vendor_points_per_inch is None
        else Fraction(vendor_points_per_inch) / arch_points_per_inch
    )
    horizontal, h_reason = _axis(
        Axis.HORIZONTAL, arch, vendor, arch_points_per_inch, settings, stretch_fixed=fixed
    )
    vertical, v_reason = _axis(
        Axis.VERTICAL, arch, vendor, arch_points_per_inch, settings, stretch_fixed=fixed
    )
    if fixed is not None:
        if horizontal is None:
            found, reason = _by_extent(
                Axis.HORIZONTAL, arch, vendor, arch_points_per_inch, fixed, settings
            )
            horizontal, h_reason = found, (reason if found else f"{h_reason}; {reason}")
        if vertical is None:
            found, reason = _by_extent(
                Axis.VERTICAL, arch, vendor, arch_points_per_inch, fixed, settings
            )
            vertical, v_reason = found, (reason if found else f"{v_reason}; {reason}")
        return Registration(horizontal, vertical, f"widths: {h_reason}; heights: {v_reason}")
    if horizontal and vertical:
        larger = max(horizontal.stretch, vertical.stretch)
        if abs(horizontal.stretch - vertical.stretch) > settings.stretch_agreement * larger:
            return Registration(
                None,
                None,
                "the widths and the heights give the vendor's drawing two different scales: the "
                "views are not laid over each other",
            )
    elif horizontal or vertical:
        known = horizontal or vertical
        assert known is not None
        other = Axis.VERTICAL if known.axis is Axis.HORIZONTAL else Axis.HORIZONTAL
        found, reason = _axis(
            other, arch, vendor, arch_points_per_inch, settings, stretch_fixed=known.stretch
        )
        if found is None:
            found, reason = _by_extent(
                other, arch, vendor, arch_points_per_inch, known.stretch, settings
            )
        if other is Axis.VERTICAL:
            vertical, v_reason = found, reason
        else:
            horizontal, h_reason = found, reason
    return Registration(horizontal, vertical, f"widths: {h_reason}; heights: {v_reason}")


def vendor_candidates(
    span: DrawnDimension,
    vendor: Sequence[DrawnDimension],
    registration: Registration,
    *,
    arch_points_per_inch: Fraction,
    settings: RegisterSettings,
) -> Candidates:
    """The vendor spans that could measure the same thing as the architect's `span`, nearest first."""
    placed = registration.on(span.axis)
    if placed is None:
        return Candidates((), f"the {span.axis.value} dimensions of the two views do not line up")
    vendor_ppi = arch_points_per_inch * placed.stretch
    m0 = placed.stretch * Fraction(span.start_pt) + placed.shift
    m1 = placed.stretch * Fraction(span.end_pt) + placed.shift
    arch_length_in = Fraction(span.length_pt) / arch_points_per_inch
    window_in = max(settings.tick_tolerance_in, settings.candidate_fraction * arch_length_in)
    window = window_in * vendor_ppi
    found: list[Candidate] = []
    for other in vendor:
        if other.axis is not span.axis:
            continue
        off = max(abs(Fraction(other.start_pt) - m0), abs(Fraction(other.end_pt) - m1))
        if off <= window:
            found.append(Candidate(other, off / vendor_ppi))
    found.sort(key=lambda candidate: (candidate.off_in, candidate.span.row, candidate.span.index))
    if found:
        return Candidates(tuple(found), f"{len(found)} vendor dimension(s) run about the same span")
    tight = settings.tick_tolerance_in * vendor_ppi
    lines: dict[tuple[Fraction, int], list[DrawnDimension]] = {}
    for other in vendor:
        if other.axis is span.axis:
            lines.setdefault((Fraction(other.offset_pt), other.row), []).append(other)
    for parts in lines.values():
        starts = [part for part in parts if abs(Fraction(part.start_pt) - m0) <= tight]
        ends = [part for part in parts if abs(Fraction(part.end_pt) - m1) <= tight]
        if starts and ends and ends[0].index > starts[0].index:
            pieces = ends[0].index - starts[0].index + 1
            return Candidates(
                (),
                f"the vendor prints this span only as {pieces} pieces, with no single dimension "
                "for the whole of it",
            )
    return Candidates((), "no vendor dimension runs about the same span")
