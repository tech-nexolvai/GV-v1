"""Which architect dimension measures the same thing as which vendor piece: paired by drawn position.

On a combined sheet the architect's elevation and the vendor's shop drawing sit on the same page at
different scales and in different places. Type 1 asks whether the vendor's printed sizes equal the
architect's, and exact maths can answer that only once each architect dimension has been paired
with the vendor piece (or pieces) it measures. This module makes that pairing, and nothing else.

**By position, never by printed value.** Pairing by matching printed numbers is circular: it can
only ever pair numbers that already agree, so a real mismatch could never FAIL. Here every tick is
converted to real inches through its own view's scale, the architect's row is slid along the
vendor's, and a span pairs when both its ends land on vendor ticks. `DrawnSpan.printed` is carried
for the caller and is never read by anything in this file.

**How an alignment is chosen.** For one architect row, a tick `a` coincides with a vendor tick `v`
at offset `d` when `|a + d - v| <= tolerance`. Every offset where the set of coinciding ticks
changes is an end of one of the intervals `[v - a - tol, v - a + tol]`, so evaluating the set at
every such end and at the midpoint between consecutive ends visits every distinct alignment, exactly
(all `Fraction`; no float, no step size). The best alignment is the one with the most coinciding
ticks, across all architect rows. It is accepted only when it reaches `minimum_coincident_ticks` and
beats every *conflicting* alignment — one that sends some tick to a different partner, or comes from
another architect row — by `minimum_support_margin`. An alignment that agrees with the best on every
tick they share is the same correspondence seen through a slightly shifted window (cumulative drift),
not a rival. Where several equally good alignments agree, only the ticks common to all of them pair.

**What never pairs.**

* An architect span whose `on_outline` is not `True`. `False` is a fixture centre line (an outlet,
  a sink, an artwork) and `None` is not known: pairing either with a cabinet could make the later
  compare PASS a cabinet against a number that is not its width (decision D4, the false-PASS
  guard). Ticks that belong to no outline span do not count towards an alignment either, so a
  centre line landing on a joint by coincidence cannot make an alignment look stronger.
* A tick with another tick of its own row within twice the tolerance (a 3/4" scribe beside a
  cabinet). Which of the two the other drawing's tick means would be decided by where the tolerance
  happens to end, not by the drawing, so neither is used.
* One vendor piece against two architect bays. The vendor may split an architect bay into several
  pieces; the reverse leaves the architect's middle tick on no vendor tick.

**Tolerance.** `min(tick_tolerance_in, tolerance_fraction_of_smallest_bay × the smallest gap between
the architect row's outline ticks)`, with the fraction below one half so that two neighbouring
architect ticks can never both reach one vendor tick. Note that two ticks may each be off by the
tolerance in opposite directions, so one span can absorb a drawn difference of up to twice the
tolerance; a larger difference leaves that span unpaired (then the AI step, never a guess).

Pure: stdlib only, no database, no models, no network. Nothing here imports `verdict/` and
`verdict/` must never import this.

Source: issue #1053 · Plan: "Type 1 vendor vs architect (reasoned, 2026-10-09)" §5 T2 (vault) ·
Verification: `tests/extraction/architect/test_pairing.py`
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from itertools import pairwise
from typing import Literal

__all__ = [
    "DrawnRow",
    "DrawnSpan",
    "PairingResult",
    "PairingSettings",
    "PairingStatus",
    "PiecePair",
    "SpanIndex",
    "pair_rows",
]

SpanIndex = int | Literal["overall"]
"""A span of a row by its position in `DrawnRow.spans`, or the row's overall."""


# --- inputs --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DrawnSpan:
    """One dimension as drawn: its two tick positions on the page, and what the ticks sit on."""

    x0_pt: Decimal
    """The left tick, in page points."""
    x1_pt: Decimal
    """The right tick, in page points; strictly right of `x0_pt`."""
    on_outline: bool | None
    """Architect: `True` when both ticks sit on solid drawn casework edges, `False` when either is
    a fixture centre line, `None` when not known. The vendor's spans pass `None`."""
    printed: str | None = None
    """The label as printed, carried for the caller. Never read here: see the module docstring."""

    def __post_init__(self) -> None:
        for name in ("x0_pt", "x1_pt"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"{name} must be an exact Decimal, not {getattr(self, name)!r}")
        if not self.x0_pt < self.x1_pt:
            raise ValueError(f"x0_pt {self.x0_pt} must be left of x1_pt {self.x1_pt}")


@dataclass(frozen=True, slots=True)
class DrawnRow:
    """One dimension row: a contiguous chain of spans left to right, an optional overall, and the
    scale of the view it is drawn in."""

    key: str
    """The caller's id for the row; returned on the pairs."""
    spans: tuple[DrawnSpan, ...]
    """Left to right; each span's `x1_pt` is exactly the next span's `x0_pt`."""
    overall: DrawnSpan | None
    pt_per_inch: Fraction | None
    """This view's scale: page points per real inch. `None` when not known."""

    def __post_init__(self) -> None:
        for left, right in pairwise(self.spans):
            if left.x1_pt != right.x0_pt:
                raise ValueError(
                    f"row {self.key!r}: spans must be contiguous, left to right "
                    f"({left.x1_pt} is not {right.x0_pt})"
                )
        if self.pt_per_inch is not None:
            if not isinstance(self.pt_per_inch, Fraction):
                raise TypeError(f"row {self.key!r}: pt_per_inch must be an exact Fraction")
            if self.pt_per_inch <= 0:
                raise ValueError(f"row {self.key!r}: pt_per_inch must be positive")


@dataclass(frozen=True, slots=True)
class PairingSettings:
    """Every threshold, stated by the caller (no defaults: the repo's convention for thresholds)."""

    tick_tolerance_in: Fraction
    """How far apart, in real inches, two ticks may be and still coincide."""
    tolerance_fraction_of_smallest_bay: Fraction
    """The tolerance also stays at or below this fraction of the smallest gap between the
    architect row's outline ticks. Below one half, so neighbouring ticks can never both coincide."""
    minimum_coincident_ticks: int
    """An alignment needs at least this many coinciding ticks. At least 2: one always coincides."""
    minimum_support_margin: int
    """The best alignment must beat every conflicting one by at least this many ticks."""

    def __post_init__(self) -> None:
        if self.tick_tolerance_in <= 0:
            raise ValueError("tick_tolerance_in must be positive")
        if not 0 < self.tolerance_fraction_of_smallest_bay < Fraction(1, 2):
            raise ValueError("tolerance_fraction_of_smallest_bay must be above 0 and below 1/2")
        if self.minimum_coincident_ticks < 2:
            raise ValueError("minimum_coincident_ticks must be at least 2")
        if self.minimum_support_margin < 1:
            raise ValueError("minimum_support_margin must be at least 1")


# --- outputs -------------------------------------------------------------------------------------


class PairingStatus(StrEnum):
    PAIRED = "paired"
    """One alignment is clearly best; `pairs` holds what it pairs (possibly nothing)."""
    AMBIGUOUS = "ambiguous"
    """Two alignments (or two architect rows) are too close to choose between."""
    NO_FIT = "no_fit"
    """No alignment reaches the minimum number of coinciding ticks."""
    NO_SCALE = "no_scale"
    """A scale needed to convert drawn positions to inches is unknown."""
    NOTHING_COMPARABLE = "nothing_comparable"
    """No architect row, no architect span on the drawn outline, or an empty vendor row."""


@dataclass(frozen=True, slots=True)
class PiecePair:
    """One architect dimension and the vendor piece(s) it measures."""

    kind: Literal["piece", "overall"]
    """`overall`: the architect dimension spans the vendor row end to end, and pairs with the
    vendor's printed overall (or, where the vendor printed none, the whole chain)."""
    architect_row_key: str
    architect_span_index: SpanIndex
    vendor_span_indices: tuple[int, ...]
    """Contiguous indices into the vendor row's spans. For `overall`, the pieces between the
    overall's ends, or empty when its ends are not on the chain's ticks."""
    start_error_in: Fraction
    """Vendor tick minus the aligned architect tick, at the left end, in real inches."""
    end_error_in: Fraction
    """The same at the right end."""


@dataclass(frozen=True, slots=True)
class PairingResult:
    status: PairingStatus
    architect_row_key: str | None
    """The architect row paired; `None` unless PAIRED."""
    offset_in: Fraction | None
    """Where the architect row's left end sits, in real inches right of the vendor row's left end."""
    tolerance_in: Fraction | None
    """The tolerance actually used for the paired row."""
    support: int
    """Coinciding ticks in the best alignment."""
    runner_up_support: int
    """Coinciding ticks in the best conflicting alignment (or another row's best)."""
    pairs: tuple[PiecePair, ...]
    excluded: tuple[tuple[SpanIndex, str], ...]
    """Each architect span of the paired row that did not pair, with why."""
    reasons: tuple[str, ...]


# --- internals -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Tick:
    at: Fraction
    """Real inches from the row's own left end."""
    chain: int | None
    """Its index among the chain's ticks (0..len(spans)), or `None` for an overall end alone."""
    overall_end: bool


@dataclass(frozen=True, slots=True)
class _Prepared:
    """One architect row made ready for alignment."""

    row: DrawnRow
    tolerance: Fraction
    ticks: tuple[_Tick, ...]
    """Every tick, sorted; the indices below point into it."""
    scored: tuple[int, ...]
    """Outline ticks that may coincide: on an outline span and not crowded."""
    crowded: frozenset[int]
    """Outline ticks with another outline tick within twice the tolerance."""


_Match = frozenset[tuple[int, int]]
"""An alignment: (architect tick index, vendor tick index) pairs that coincide."""


def _inches(x: Decimal, origin: Decimal, scale: Fraction) -> Fraction:
    return (Fraction(x) - Fraction(origin)) / scale


def _origin(row: DrawnRow) -> Decimal:
    starts = [span.x0_pt for span in row.spans]
    if row.overall is not None:
        starts.append(row.overall.x0_pt)
    return min(starts)


def _ticks(
    row: DrawnRow, scale: Fraction, tolerance: Fraction, *, include_overall: bool
) -> tuple[_Tick, ...]:
    """The row's ticks in real inches, an overall end merged into the one chain tick within
    tolerance of it (and kept separate where none or several are)."""
    origin = _origin(row)
    chain_at: list[Fraction] = []
    if row.spans:
        chain_at.append(_inches(row.spans[0].x0_pt, origin, scale))
        chain_at.extend(_inches(span.x1_pt, origin, scale) for span in row.spans)
    overall_ends: set[int] = set()
    extra: list[Fraction] = []
    if include_overall and row.overall is not None:
        for x in (row.overall.x0_pt, row.overall.x1_pt):
            at = _inches(x, origin, scale)
            near = [k for k, c in enumerate(chain_at) if abs(c - at) <= tolerance]
            if len(near) == 1:
                overall_ends.add(near[0])
            else:
                extra.append(at)
    ticks = [_Tick(at, k, k in overall_ends) for k, at in enumerate(chain_at)]
    ticks.extend(_Tick(at, None, True) for at in extra)
    return tuple(sorted(ticks, key=lambda tick: (tick.at, tick.chain is None, tick.chain or 0)))


def _outline_chain_ticks(row: DrawnRow) -> set[int]:
    on: set[int] = set()
    for k, span in enumerate(row.spans):
        if span.on_outline is True:
            on.update((k, k + 1))
    return on


def _crowded(
    positions: Sequence[Fraction], indices: Iterable[int], tolerance: Fraction
) -> set[int]:
    chosen = sorted(indices, key=lambda i: positions[i])
    crowded: set[int] = set()
    for left, right in pairwise(chosen):
        if positions[right] - positions[left] <= 2 * tolerance:
            crowded.update((left, right))
    return crowded


def _prepare(row: DrawnRow, settings: PairingSettings) -> _Prepared | None:
    """The row's outline ticks and its tolerance, or `None` when it has no outline to align."""
    scale = row.pt_per_inch
    assert scale is not None
    origin = _origin(row)
    outline_chain = _outline_chain_ticks(row)
    chain_positions = sorted(
        _inches(row.spans[0].x0_pt if k == 0 else row.spans[k - 1].x1_pt, origin, scale)
        for k in outline_chain
    )
    gaps = [right - left for left, right in pairwise(chain_positions)]
    overall_on = row.overall is not None and row.overall.on_outline is True
    if overall_on:
        assert row.overall is not None
        gaps.append((Fraction(row.overall.x1_pt) - Fraction(row.overall.x0_pt)) / scale)
    if not gaps:
        return None
    tolerance = min(
        settings.tick_tolerance_in, settings.tolerance_fraction_of_smallest_bay * min(gaps)
    )
    ticks = _ticks(row, scale, tolerance, include_overall=True)
    outline = [
        i
        for i, tick in enumerate(ticks)
        if (tick.chain is not None and tick.chain in outline_chain)
        or (tick.overall_end and overall_on)
    ]
    crowded = _crowded([tick.at for tick in ticks], outline, tolerance)
    return _Prepared(
        row=row,
        tolerance=tolerance,
        ticks=ticks,
        scored=tuple(i for i in outline if i not in crowded),
        crowded=frozenset(crowded),
    )


def _within(positions: Sequence[Fraction], at: Fraction, tolerance: Fraction) -> range:
    return range(bisect_left(positions, at - tolerance), bisect_right(positions, at + tolerance))


def _match_at(
    prepared: _Prepared,
    vendor_at: Sequence[Fraction],
    vendor_usable: frozenset[int],
    offset: Fraction,
) -> _Match:
    pairs: set[tuple[int, int]] = set()
    for a in prepared.scored:
        near = _within(vendor_at, prepared.ticks[a].at + offset, prepared.tolerance)
        if len(near) == 1 and near[0] in vendor_usable:
            pairs.add((a, near[0]))
    return frozenset(pairs)


def _alignments(
    prepared: _Prepared, vendor_at: Sequence[Fraction], vendor_usable: frozenset[int]
) -> set[_Match]:
    """Every distinct alignment of the row along the vendor's, by an exact sweep of offsets."""
    tol = prepared.tolerance
    ends: set[Fraction] = set()
    for a in prepared.scored:
        for v in vendor_usable:
            centre = vendor_at[v] - prepared.ticks[a].at
            ends.update((centre - tol, centre + tol))
    probes = sorted(ends)
    probes.extend((left + right) / 2 for left, right in pairwise(sorted(ends)))
    found = {_match_at(prepared, vendor_at, vendor_usable, offset) for offset in probes}
    found.discard(frozenset())
    return found


def _conflict(first: _Match, second: _Match) -> bool:
    partner_of_a = dict(first)
    partner_of_v = {v: a for a, v in first}
    return any(
        (a in partner_of_a and partner_of_a[a] != v) or (v in partner_of_v and partner_of_v[v] != a)
        for a, v in second
    )


def _fmt(value: Fraction) -> str:
    """A Fraction of an inch for a reason line, e.g. `1 1/2`."""
    sign = "-" if value < 0 else ""
    value = abs(value)
    whole, part = divmod(value.numerator, value.denominator)
    if part == 0:
        return f"{sign}{whole}"
    fraction = f"{part}/{value.denominator}"
    return f"{sign}{whole} {fraction}" if whole else f"{sign}{fraction}"


def _refusal(
    status: PairingStatus,
    reasons: Iterable[str],
    *,
    support: int = 0,
    runner_up: int = 0,
) -> PairingResult:
    return PairingResult(
        status=status,
        architect_row_key=None,
        offset_in=None,
        tolerance_in=None,
        support=support,
        runner_up_support=runner_up,
        pairs=(),
        excluded=(),
        reasons=tuple(reasons),
    )


# --- the pairing ---------------------------------------------------------------------------------


def pair_rows(
    vendor: DrawnRow, architect_rows: Sequence[DrawnRow], settings: PairingSettings
) -> PairingResult:
    """Pair the architect's spans with the vendor's pieces by where they are drawn.

    Deterministic: the same rows give the same result whatever order `architect_rows` come in.
    Printed values are never read. Raises `ValueError` for two architect rows with one key.
    """
    keys = [row.key for row in architect_rows]
    if len(set(keys)) != len(keys):
        raise ValueError("architect row keys must be unique")
    rows = sorted(architect_rows, key=lambda row: row.key)

    if not rows:
        return _refusal(
            PairingStatus.NOTHING_COMPARABLE,
            ["The architect's view has no dimension row to compare with."],
        )
    if not vendor.spans and vendor.overall is None:
        return _refusal(PairingStatus.NOTHING_COMPARABLE, ["The vendor's row has no dimension."])
    candidates = [
        row
        for row in rows
        if any(span.on_outline is True for span in row.spans)
        or (row.overall is not None and row.overall.on_outline is True)
    ]
    if not candidates:
        return _refusal(
            PairingStatus.NOTHING_COMPARABLE,
            [
                (
                    "No architect dimension has both ends on the drawn casework outline (they run to "
                    "fixture centre lines, or it is not known), so none can measure a cabinet."
                )
            ],
        )
    missing = [row.key for row in candidates if row.pt_per_inch is None]
    if vendor.pt_per_inch is None or missing:
        which = (["the vendor's view"] if vendor.pt_per_inch is None else []) + [
            f"architect row {key!r}" for key in missing
        ]
        return _refusal(
            PairingStatus.NO_SCALE,
            [
                (
                    f"The drawing scale is unknown for {', '.join(which)}, so drawn positions cannot "
                    "be compared in inches."
                )
            ],
        )

    prepared = [p for p in (_prepare(row, settings) for row in candidates) if p is not None]
    vendor_scale = vendor.pt_per_inch
    found: list[tuple[int, _Match]] = []
    vendor_ticks_by_row: list[tuple[_Tick, ...]] = []
    for index, prep in enumerate(prepared):
        vticks = _ticks(vendor, vendor_scale, prep.tolerance, include_overall=True)
        vendor_ticks_by_row.append(vticks)
        vendor_at = [tick.at for tick in vticks]
        crowded = _crowded(vendor_at, range(len(vticks)), prep.tolerance)
        usable = frozenset(i for i in range(len(vticks)) if i not in crowded)
        found.extend((index, match) for match in _alignments(prep, vendor_at, usable))

    support = max((len(match) for _, match in found), default=0)
    best = [(index, match) for index, match in found if len(match) == support]
    best_rows = {index for index, _ in best}

    def rival(index: int, match: _Match) -> bool:
        return any(i != index or _conflict(m, match) for i, m in best)

    runner_up = max((len(m) for i, m in found if (i, m) not in best and rival(i, m)), default=0)
    if len(best_rows) > 1 or any(_conflict(m1, m2) for _, m1 in best for _, m2 in best):
        runner_up = support

    if support < settings.minimum_coincident_ticks:
        return _refusal(
            PairingStatus.NO_FIT,
            [
                (
                    f"No architect row lines up with the vendor's: at best {support} tick(s) "
                    f"coincide, and {settings.minimum_coincident_ticks} are needed."
                )
            ],
            support=support,
            runner_up=runner_up,
        )
    if support - runner_up < settings.minimum_support_margin:
        return _refusal(
            PairingStatus.AMBIGUOUS,
            [
                (
                    f"Two alignments are too close to choose between: {support} and {runner_up} "
                    f"coinciding ticks (a margin of {settings.minimum_support_margin} is needed)."
                )
            ],
            support=support,
            runner_up=runner_up,
        )

    (row_index,) = best_rows
    common = frozenset.intersection(*(match for _, match in best))
    if len(common) < settings.minimum_coincident_ticks:
        return _refusal(
            PairingStatus.AMBIGUOUS,
            [
                (
                    "The equally good alignments of the architect's row drift apart and share "
                    f"only {len(common)} tick(s)."
                )
            ],
            support=support,
            runner_up=runner_up,
        )
    return _paired(
        prepared[row_index],
        vendor,
        vendor_ticks_by_row[row_index],
        common,
        support=support,
        runner_up=runner_up,
    )


def _paired(
    prep: _Prepared,
    vendor: DrawnRow,
    vticks: tuple[_Tick, ...],
    common: _Match,
    *,
    support: int,
    runner_up: int,
) -> PairingResult:
    row, tol = prep.row, prep.tolerance
    lows = [vticks[v].at - prep.ticks[a].at - tol for a, v in common]
    highs = [vticks[v].at - prep.ticks[a].at + tol for a, v in common]
    offset = (max(lows) + min(highs)) / 2
    partner = dict(common)
    vendor_at = [tick.at for tick in vticks]

    def tick_of(chain: int | None, *, overall_end: int | None = None) -> int:
        """The architect tick index of a chain tick, or of the overall's left (0) / right (1) end."""
        if chain is not None:
            return next(i for i, t in enumerate(prep.ticks) if t.chain == chain)
        ends = [i for i, t in enumerate(prep.ticks) if t.overall_end]
        return ends[0] if overall_end == 0 else ends[-1]

    if vendor.overall is not None:
        vendor_ends = [i for i, t in enumerate(vticks) if t.overall_end]
        vendor_left, vendor_right = vendor_ends[0], vendor_ends[-1]
    else:
        vendor_left = next(i for i, t in enumerate(vticks) if t.chain == 0)
        vendor_right = next(i for i, t in enumerate(vticks) if t.chain == len(vendor.spans))

    vendor_crowded = _crowded(vendor_at, range(len(vticks)), tol)

    def end_reason(a: int) -> str:
        if a in prep.crowded:
            return "it sits within twice the tolerance of another architect tick"
        near = _within(vendor_at, prep.ticks[a].at + offset, tol)
        if len(near) > 1 or any(v in vendor_crowded for v in near):
            return "it lands near two vendor ticks too close together to tell apart"
        if near:
            return "it lands on a vendor tick in only some of the equally good alignments"
        return "it does not land on a vendor tick at this alignment"

    pairs: list[PiecePair] = []
    excluded: list[tuple[SpanIndex, str]] = []
    elements: list[tuple[SpanIndex, DrawnSpan, int, int]] = [
        (k, span, tick_of(k), tick_of(k + 1)) for k, span in enumerate(row.spans)
    ]
    if row.overall is not None:
        elements.append(
            ("overall", row.overall, tick_of(None, overall_end=0), tick_of(None, overall_end=1))
        )
    for index, span, left, right in elements:
        if span.on_outline is False:
            excluded.append((index, "centre line, never paired with a cabinet"))
            continue
        if span.on_outline is None:
            excluded.append((index, "outline unknown, never paired with a cabinet"))
            continue
        missing = [(name, a) for name, a in (("left", left), ("right", right)) if a not in partner]
        if missing:
            excluded.append(
                (index, "; ".join(f"its {name} end: {end_reason(a)}" for name, a in missing))
            )
            continue
        v_left, v_right = partner[left], partner[right]
        errors = (
            vticks[v_left].at - (prep.ticks[left].at + offset),
            vticks[v_right].at - (prep.ticks[right].at + offset),
        )
        if (v_left, v_right) == (vendor_left, vendor_right):
            k_left, k_right = vticks[v_left].chain, vticks[v_right].chain
            pieces = () if k_left is None or k_right is None else tuple(range(k_left, k_right))
            pairs.append(PiecePair("overall", row.key, index, pieces, *errors))
            continue
        k_left, k_right = vticks[v_left].chain, vticks[v_right].chain
        if index == "overall":
            excluded.append((index, "it does not span the vendor's row end to end"))
            continue
        if k_left is None or k_right is None:
            excluded.append((index, "an end lands on the vendor's overall only, not on a piece"))
            continue
        pairs.append(PiecePair("piece", row.key, index, tuple(range(k_left, k_right)), *errors))

    pairs.sort(key=lambda p: (p.kind != "overall", _order(p.architect_span_index)))
    paired_text = (
        f"{len(pairs)} architect dimension(s) pair with the vendor's pieces"
        if pairs
        else "but no architect dimension has both ends on vendor ticks and on the outline"
    )
    reasons = (
        (
            f"The architect's row {row.key!r} lines up with the vendor's row: {support} of its ticks "
            f'land within {_fmt(tol)}" of a vendor tick (the best rival alignment: {runner_up}).'
        ),
        (
            f"Its left end sits {_fmt(offset)}\" right of the vendor row's left end; "
            f"{paired_text}."
        ),
    )
    return PairingResult(
        status=PairingStatus.PAIRED,
        architect_row_key=row.key,
        offset_in=offset,
        tolerance_in=tol,
        support=support,
        runner_up_support=runner_up,
        pairs=tuple(pairs),
        excluded=tuple(excluded),
        reasons=reasons,
    )


def _order(index: SpanIndex) -> tuple[int, int]:
    return (1, 0) if index == "overall" else (0, index)
