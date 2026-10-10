"""Which view of the architect's own file shows the same countertop as a vendor view: code's half (#1166).

When the architect's drawings are uploaded as their own PDF, each vendor countertop row must first be
matched with the one architect view that draws the same countertop. Vendors rarely print the
architect's sheet reference, and the two drawings share almost no text, so code has two judgments
of its own and nothing else:

1. **A printed reference** (`find_references`): `REF 3/A-401` printed inside the vendor's drawing
   that holds the row (the caller keeps only those), compared after `normalise_reference` (upper
   case, no spaces, dashes or dots) with each architect view's own `<view>/<sheet>` — the view
   number from its bubble, the sheet from the bubble or the title block. Exactly one view named →
   `reference`, unless the drawing contradicts it: that view's run measured and more than
   `run_length_tolerance_in + clear_margin_in` off, or another view the clear geometry winner →
   no pick (`geometry_tie`, the reviewer decides).
2. **Geometry** (`match_by_code`): the vendor row's run (its overall, else its chain end to end) in
   real inches through the vendor's scale, against each architect row's run through its view's own
   scale; each view's best row counts. A view **fits** only on two geometric agreements: the two
   runs agree within `run_length_tolerance_in`, AND the shapes agree both ways round — the row has
   at least three ticks (both ends and an inner one), every one of them lands on a vendor tick
   within the pairing's tick tolerance (left ends or right ends together), and at least half of the
   vendor's inner ticks land on one of its ticks. One length alone is one number another drawing
   matches by chance: on a split keyed set (#1166, local proof) a single-span row of the wrong view
   matched a vendor run to the inch. Exactly one view fits, every other measured view is more than
   `run_length_tolerance_in + clear_margin_in` off, AND no other view has dimension rows whose run
   cannot be measured (its scale unknown: a twin code cannot rule out) → `geometry_clear`; two or
   more fit (twins), one fits with another too close behind, or another cannot be measured →
   `geometry_tie`; none fits → `geometry_none`; the vendor's scale unknown or no architect row with
   a scale anywhere → `no_geometry`. The number of bays and how well the position-pairing lines the
   two rows up (`pairing.pair_rows`) are kept as evidence and break exact ties in the *ranking*
   only, never the verdict: on a split set no architect span may sit on the outline, and the
   pairing then has nothing to say.

**The decision** (`decide_match`, decision D1 of 2026-10-10): automatic **only** when code has a
pick (`reference` or `geometry_clear`) AND both AIs picked that same view AND it was shown to them
AND the view stands clearly apart from its neighbours. The same three judgments on a view that is
not clearly apart → `not_separated` (its dimensions were not read). No view at all →
`no_candidates`. Everything else → `needs_reviewer`, with nothing chosen: the AIs agreeing without
code is evidence for the reviewer, never a match.

**What it never does.** It never reads or compares a printed dimension value (the run is drawn
length through scale, the reference is a sheet name), never rounds (`Fraction` throughout), and never
picks between twins.

Pure: stdlib only, no database, no models, no network; no defaults for any threshold.
Source: issue #1166 · Plan: "Type 1 with a separate architect PDF (2026-10-10)" §3 ·
Verification: `tests/extraction/architect/test_view_matching.py`
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from typing import Final

from extraction.architect.pairing import DrawnRow, PairingSettings, PairingStatus, pair_rows

__all__ = [
    "ArchitectViewFacts",
    "CandidateScore",
    "CodeMatch",
    "CodeVerdict",
    "MatchDecision",
    "MatchSettings",
    "VendorViewFacts",
    "decide_match",
    "find_references",
    "match_by_code",
    "normalise_reference",
    "reference_keys",
]

_DROPPED: Final = re.compile(r"[\s\-.‐-―]+")
#: `3/A-401`, `4 / ID 7.4`: a view mark (1-3 letters or digits), a slash, and a sheet (1-3 letters,
#: an optional space, dash or dot, then digits, with an optional decimal part). A fraction such as
#: `3/4` never matches: a sheet starts with a letter.
_REFERENCE: Final = re.compile(
    r"(?<![A-Z0-9])([A-Z0-9]{1,3})\s*/\s*([A-Z]{1,3}[\s.\-]?\d{1,4}(?:\.\d{1,3})?)(?![0-9])"
)


class CodeVerdict(StrEnum):
    REFERENCE = "reference"
    """Exactly one view is named by a reference printed on the vendor's sheet."""
    GEOMETRY_CLEAR = "geometry_clear"
    """Exactly one view's run fits, and every other is clearly off."""
    GEOMETRY_TIE = "geometry_tie"
    """Two or more views fit (twins), or the one that fits has another too close behind."""
    GEOMETRY_NONE = "geometry_none"
    """No view's run fits."""
    NO_GEOMETRY = "no_geometry"
    """The vendor's scale is unknown, or no architect view has a dimension row with a scale."""


#: The verdicts that give code a pick of its own (decision D1).
_PICKING: Final = frozenset({CodeVerdict.REFERENCE, CodeVerdict.GEOMETRY_CLEAR})


@dataclass(frozen=True, slots=True)
class VendorViewFacts:
    """The vendor row being matched: its drawn row (with its scale) and the references printed on
    its sheet (already normalised)."""

    row: DrawnRow | None
    references: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArchitectViewFacts:
    """One view of the architect's file, as code knows it."""

    key: str
    """The caller's id for the view (the index row's id as text)."""
    sheet_number: str | None
    bubble: str | None
    rows: tuple[DrawnRow, ...]
    """Its dimension rows as drawn, each with the view's scale."""
    separated: bool


@dataclass(frozen=True, slots=True)
class MatchSettings:
    """Every threshold, stated by the caller (no defaults: the repo's convention)."""

    run_length_tolerance_in: Fraction
    """A view fits when its row's run and the vendor's run differ by at most this, in real inches."""
    clear_margin_in: Fraction
    """...and is the clear winner only when every other view is more than tolerance + this off."""
    shown_to_ais: int
    """How many of the ranked views the two AIs are shown."""
    pairing: PairingSettings
    """For the position-pairing evidence (a ranking tiebreak only)."""

    def __post_init__(self) -> None:
        if self.run_length_tolerance_in <= 0:
            raise ValueError("run_length_tolerance_in must be positive")
        if self.clear_margin_in < 0:
            raise ValueError("clear_margin_in must not be negative")
        if self.shown_to_ais < 1:
            raise ValueError("shown_to_ais must be at least 1")


@dataclass(frozen=True, slots=True)
class CandidateScore:
    """One architect view, scored by code against the vendor row."""

    key: str
    rank: int
    """1-based place in the ranked list."""
    reference_match: bool
    best_row_key: str | None
    """The view's row whose run is closest to the vendor's; `None` without a usable row."""
    run_length_error_in: Fraction | None
    """|vendor run − that row's run|, real inches; `None` when either scale or row is missing."""
    bays_vendor: int | None
    bays_architect: int | None
    pair_status: str | None
    """`pairing.pair_rows` against all the view's rows; `None` when not run (no vendor row)."""
    pair_support: int | None
    fits: bool
    """The run is within tolerance AND the row's shape agrees (`_same_shape`)."""
    evidence: tuple[str, ...]
    """Plain words for the reviewer."""
    ticks_aligned: int | None = None
    """How many of the best row's ticks land on the vendor's (`_ticks_on_vendor`)."""


@dataclass(frozen=True, slots=True)
class CodeMatch:
    verdict: CodeVerdict
    pick: str | None
    """The view code picks: set only for `reference` and `geometry_clear`."""
    ranked: tuple[CandidateScore, ...]
    """Every view, best first."""
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MatchDecision:
    status: str
    """`auto_matched`, `needs_reviewer`, `not_separated` or `no_candidates`."""
    chosen: str | None
    """The view matched; set only for `auto_matched` and `not_separated`."""
    reasons: tuple[str, ...]


# --- references ----------------------------------------------------------------------------------


def normalise_reference(text: str) -> str:
    """Upper case, with every space, dash and dot dropped: `3 / a-401.` → `3/A401`."""
    return _DROPPED.sub("", text.upper())


def find_references(phrases: Iterable[str]) -> tuple[str, ...]:
    """Every `<view>/<sheet>` reference printed in `phrases`, normalised, in first-seen order:
    `REF 3/A-401` → `3/A401`. A dimension or a fraction is never one (a sheet starts with a
    letter)."""
    found: list[str] = []
    for phrase in phrases:
        for match in _REFERENCE.finditer(phrase.upper()):
            key = normalise_reference(f"{match.group(1)}/{match.group(2)}")
            if key not in found:
                found.append(key)
    return tuple(found)


def reference_keys(bubble: str | None, sheet_number: str | None) -> frozenset[str]:
    """The `<view>/<sheet>` keys an architect view answers to.

    The bubble prints the view's mark first and, usually, a sheet reference after it (`4 ID 7.4`).
    The keys are the mark with the bubble's own sheet (`4/ID74`) and the mark with the sheet number
    from the title block (`4/A501`). No bubble → no key: a sheet number alone does not say which view.
    """
    if bubble is None:
        return frozenset()
    tokens = [token for token in re.split(r"[\s/]+", bubble.strip().upper()) if token]
    if not tokens:
        return frozenset()
    mark, rest = tokens[0], "".join(tokens[1:])
    keys: set[str] = set()
    if rest:
        keys.add(normalise_reference(f"{mark}/{rest}"))
    if sheet_number:
        keys.add(normalise_reference(f"{mark}/{sheet_number}"))
    return frozenset(keys)


# --- geometry ------------------------------------------------------------------------------------


def _run_pt(row: DrawnRow) -> Fraction | None:
    """The row's drawn run end to end in page points: its overall, else its chain."""
    if row.overall is not None:
        return Fraction(row.overall.x1_pt) - Fraction(row.overall.x0_pt)
    if not row.spans:
        return None
    return Fraction(row.spans[-1].x1_pt) - Fraction(row.spans[0].x0_pt)


def _run_in(row: DrawnRow) -> Fraction | None:
    drawn = _run_pt(row)
    if drawn is None or row.pt_per_inch is None:
        return None
    return drawn / row.pt_per_inch


def _inches(value: Fraction) -> str:
    """A length for a reason line, to a tenth of an inch (never a decision)."""
    return str((Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal("0.1")))


@dataclass(frozen=True, slots=True)
class _Scored:
    position: int
    view: ArchitectViewFacts
    reference_match: bool
    best_row: DrawnRow | None
    error: Fraction | None
    bays_architect: int | None
    pair_status: str | None
    pair_support: int | None
    fits: bool
    evidence: tuple[str, ...]
    ticks_aligned: int | None


def _ticks_in(row: DrawnRow) -> list[Fraction]:
    """The row's chain ticks (and its overall's ends) in real inches from its own left end."""
    assert row.pt_per_inch is not None
    xs = {Fraction(span.x0_pt) for span in row.spans} | {Fraction(span.x1_pt) for span in row.spans}
    if row.overall is not None:
        xs |= {Fraction(row.overall.x0_pt), Fraction(row.overall.x1_pt)}
    origin = min(xs)
    return sorted((x - origin) / row.pt_per_inch for x in xs)


#: How the architect row's ticks and the vendor's meet: `(architect ticks on a vendor tick,
#: architect ticks, vendor inner ticks on an architect tick, vendor inner ticks)`.
_Shape = tuple[int, int, int, int]


def _ticks_on_vendor(vendor: DrawnRow, row: DrawnRow, tolerance: Fraction) -> _Shape:
    """How the architect row's ticks meet the vendor's, with the two rows' left ends together or
    their right ends together, whichever lands more architect ticks (then more vendor ticks)."""
    if vendor.pt_per_inch is None or row.pt_per_inch is None or not row.spans:
        return 0, 0, 0, 0
    mine, theirs = _ticks_in(row), _ticks_in(vendor)
    inner = theirs[1:-1]
    best: _Shape = (0, len(mine), 0, len(inner))
    for offset in (Fraction(0), theirs[-1] - mine[-1]):
        landed = sum(
            1 for tick in mine if any(abs(tick + offset - other) <= tolerance for other in theirs)
        )
        met = sum(
            1 for other in inner if any(abs(tick + offset - other) <= tolerance for tick in mine)
        )
        best = max(
            best, (landed, len(mine), met, len(inner)), key=lambda shape: (shape[0], shape[2])
        )
    return best


def _same_shape(shape: _Shape) -> bool:
    """A second geometric judgment beside the run's length, both ways round: the architect row has
    at least three ticks — both ends and an inner one — and every one of them lands on a vendor
    tick; and at least half of the vendor's inner ticks land on an architect tick (a vendor may
    split an architect bay, but a row that misses most of the vendor's joints is another drawing).
    One length alone is one number, which another drawing matches by chance."""
    landed, ticks, met, inner = shape
    return ticks >= 3 and landed == ticks and 2 * met >= inner


def _score(
    position: int,
    view: ArchitectViewFacts,
    vendor: VendorViewFacts,
    vendor_run: Fraction | None,
    settings: MatchSettings,
) -> _Scored:
    evidence: list[str] = []
    reference = bool(reference_keys(view.bubble, view.sheet_number) & set(vendor.references))
    if reference:
        evidence.append("the vendor's sheet prints a reference to this view")
    best: DrawnRow | None = None
    error: Fraction | None = None
    aligned: _Shape | None = None
    fitting: list[tuple[Fraction, str, DrawnRow, _Shape]] = []
    measured: list[tuple[Fraction, str, DrawnRow, _Shape]] = []
    for row in view.rows:
        run = _run_in(row)
        if run is None or vendor_run is None or vendor.row is None:
            continue
        difference = abs(vendor_run - run)
        shape = _ticks_on_vendor(vendor.row, row, settings.pairing.tick_tolerance_in)
        measured.append((difference, row.key, row, shape))
        if difference <= settings.run_length_tolerance_in and _same_shape(shape):
            fitting.append((difference, row.key, row, shape))
    chosen = min(fitting or measured, key=lambda item: (item[0], item[1]), default=None)
    if chosen is not None:
        error, _key, best, aligned = chosen
        evidence.append(
            f"its run is {_inches(error)} in off the vendor's (row {best.key}, "
            f"{len(best.spans)} bays against the vendor's "
            f"{0 if vendor.row is None else len(vendor.row.spans)}; "
            f"{aligned[0]} of its {aligned[1]} ticks line up with the vendor's, and "
            f"{aligned[2]} of the vendor's {aligned[3]} inner ticks with its)"
        )
    elif not view.rows:
        evidence.append("no dimension row was read in this view")
    elif vendor_run is None:
        evidence.append("the vendor's run or scale is unknown, so lengths cannot be compared")
    else:
        evidence.append("this view's scale is unknown, so its run cannot be measured")
    pair_status: str | None = None
    pair_support: int | None = None
    if vendor.row is not None and view.rows:
        try:
            paired = pair_rows(vendor.row, view.rows, settings.pairing)
        except ValueError as error_raised:
            evidence.append(f"position pairing not run: {error_raised}")
        else:
            pair_status, pair_support = paired.status.value, paired.support
            if paired.status is PairingStatus.PAIRED:
                evidence.append(f"position pairing lines {paired.support} ticks up")
    fits = bool(fitting)
    return _Scored(
        position=position,
        view=view,
        reference_match=reference,
        best_row=best,
        error=error,
        bays_architect=None if best is None else len(best.spans),
        pair_status=pair_status,
        pair_support=pair_support,
        fits=fits,
        evidence=tuple(evidence),
        ticks_aligned=None if aligned is None else aligned[0],
    )


def _order(item: _Scored) -> tuple[int, int, Fraction, int, int]:
    """Reference first; then views that fit, by error; then the rest by error; then views with rows
    whose run cannot be measured; then views with no row; each of the last two in document order.
    Pairing support breaks an exact tie in error."""
    if item.error is None:
        group = 3 if item.view.rows else 4
        return (0 if item.reference_match else 1, group, Fraction(0), 0, item.position)
    group = 1 if item.fits else 2
    return (
        0 if item.reference_match else 1,
        group,
        item.error,
        -(item.pair_support or 0),
        item.position,
    )


def match_by_code(
    vendor: VendorViewFacts, views: Sequence[ArchitectViewFacts], s: MatchSettings
) -> CodeMatch:
    """Code's judgment: a printed reference, else a clear geometry winner, else no pick.

    Deterministic: the same inputs give the same result. Raises `ValueError` for two views with
    one key.
    """
    keys = [view.key for view in views]
    if len(set(keys)) != len(keys):
        raise ValueError("architect view keys must be unique")
    vendor_run = None if vendor.row is None else _run_in(vendor.row)
    scored = [_score(position, view, vendor, vendor_run, s) for position, view in enumerate(views)]
    ordered = sorted(scored, key=_order)
    ranked = tuple(
        CandidateScore(
            key=item.view.key,
            rank=rank,
            reference_match=item.reference_match,
            best_row_key=None if item.best_row is None else item.best_row.key,
            run_length_error_in=item.error,
            bays_vendor=None if vendor.row is None else len(vendor.row.spans),
            bays_architect=item.bays_architect,
            pair_status=item.pair_status,
            pair_support=item.pair_support,
            fits=item.fits,
            evidence=item.evidence,
            ticks_aligned=item.ticks_aligned,
        )
        for rank, item in enumerate(ordered, start=1)
    )
    if not views:
        return CodeMatch(
            CodeVerdict.NO_GEOMETRY, None, (), ("The architect's file has no view to match.",)
        )
    geometry = _geometry(scored, vendor_run, s)
    referenced = [item for item in scored if item.reference_match]
    if len(referenced) == 1:
        (named,) = referenced
        clear = s.run_length_tolerance_in + s.clear_margin_in
        if named.error is not None and named.error > clear:
            return CodeMatch(
                CodeVerdict.GEOMETRY_TIE,
                None,
                ranked,
                (
                    (
                        "The vendor's drawing prints a reference to one architect view, but that "
                        f"view's run is {_inches(named.error)} in off the vendor's: the reference "
                        "and the drawing disagree, so code does not decide."
                    ),
                ),
            )
        if geometry.verdict is CodeVerdict.GEOMETRY_CLEAR and geometry.pick != named.view.key:
            return CodeMatch(
                CodeVerdict.GEOMETRY_TIE,
                None,
                ranked,
                (
                    (
                        "The vendor's drawing prints a reference to one architect view, but "
                        "another view is the clear geometry winner: code does not decide."
                    ),
                ),
            )
        return CodeMatch(
            CodeVerdict.REFERENCE,
            named.view.key,
            ranked,
            (
                (
                    "The vendor's drawing prints a reference to exactly one of the architect's "
                    "views, and its geometry does not contradict it."
                ),
            ),
        )
    reasons: tuple[str, ...] = ()
    if len(referenced) > 1:
        reasons = (
            (
                f"The vendor's drawing prints references matching {len(referenced)} architect views, "
                "so the reference does not decide."
            ),
        )
    return CodeMatch(geometry.verdict, geometry.pick, ranked, (*reasons, *geometry.reasons))


@dataclass(frozen=True, slots=True)
class _Geometry:
    verdict: CodeVerdict
    pick: str | None
    reasons: tuple[str, ...]


def _geometry(
    scored: Sequence[_Scored], vendor_run: Fraction | None, s: MatchSettings
) -> _Geometry:
    """Geometry's verdict alone: a clear winner, a tie, none, or no geometry."""
    measured = [item for item in scored if item.error is not None]
    if vendor_run is None or not measured:
        why = (
            "the vendor's scale or run is unknown"
            if vendor_run is None
            else "no architect view has a dimension row with a known scale"
        )
        return _Geometry(
            CodeVerdict.NO_GEOMETRY, None, (f"Code cannot compare the drawn runs: {why}.",)
        )
    fitting = [item for item in measured if item.fits]
    if not fitting:
        return _Geometry(
            CodeVerdict.GEOMETRY_NONE,
            None,
            (
                (
                    "No architect view's run is within "
                    f"{_inches(s.run_length_tolerance_in)} in of the vendor's with the same shape."
                ),
            ),
        )
    if len(fitting) > 1:
        return _Geometry(
            CodeVerdict.GEOMETRY_TIE,
            None,
            (
                (
                    f"{len(fitting)} architect views fit the vendor's run within "
                    f"{_inches(s.run_length_tolerance_in)} in: code cannot tell them apart."
                ),
            ),
        )
    (winner,) = fitting
    clear = s.run_length_tolerance_in + s.clear_margin_in
    close = [
        item
        for item in measured
        if item is not winner and item.error is not None and item.error <= clear
    ]
    if close:
        return _Geometry(
            CodeVerdict.GEOMETRY_TIE,
            None,
            (
                (
                    "One architect view's run fits, but another is within "
                    f"{_inches(clear)} in of the vendor's too: not a clear winner."
                ),
            ),
        )
    unmeasured = [
        item for item in scored if item is not winner and item.view.rows and item.error is None
    ]
    if unmeasured:
        return _Geometry(
            CodeVerdict.GEOMETRY_TIE,
            None,
            (
                (
                    "One architect view's run fits, but "
                    f"{len(unmeasured)} other view(s) have dimension rows whose run cannot be measured "
                    "(their scale is unknown), so code cannot rule them out: not a clear winner."
                ),
            ),
        )
    return _Geometry(
        CodeVerdict.GEOMETRY_CLEAR,
        winner.view.key,
        (
            (
                "Exactly one architect view's run fits the vendor's, every other is more than "
                f"{_inches(clear)} in off, and no other view's run is unknown."
            ),
        ),
    )


# --- the decision (D1) ---------------------------------------------------------------------------


def decide_match(
    code: CodeMatch,
    ai_picks: Sequence[str | None],
    shown: Sequence[str],
    views: Sequence[ArchitectViewFacts],
) -> MatchDecision:
    """Decision D1: automatic only when code picks a view AND both AIs pick that same view AND it
    was shown to them AND it stands clearly apart; otherwise the reviewer picks, nothing chosen.

    `ai_picks` holds each AI's pick as a view key, `None` for "none", "unsure" or no answer;
    `shown` the views the AIs were shown, in the order shown.
    """
    if not views:
        return MatchDecision(
            "no_candidates", None, ("The architect's file has no view to match with.",)
        )
    by_key = {view.key: view for view in views}
    picks = list(ai_picks)
    agreed = len(picks) >= 2 and len(set(picks)) == 1 and picks[0] is not None
    if code.verdict in _PICKING and code.pick is not None:
        if agreed and picks[0] == code.pick and code.pick in shown:
            view = by_key[code.pick]
            if not view.separated:
                return MatchDecision(
                    "not_separated",
                    code.pick,
                    (
                        (
                            "Code and both AIs chose the same architect view, but it is not clearly "
                            "apart from its neighbour, so its dimensions were not read."
                        ),
                    ),
                )
            return MatchDecision(
                "auto_matched",
                code.pick,
                (f"Code ({code.verdict.value}) and both AIs chose the same architect view.",),
            )
        if code.pick not in shown:
            why = "the view code chose was not among those shown to the AIs"
        elif len(picks) < 2:
            why = "the two AIs were not both asked"
        elif not agreed:
            why = "the two AIs did not both choose one view"
        else:
            why = "both AIs chose a different view from code's"
        return MatchDecision(
            "needs_reviewer", None, (f"Code chose a view, but {why}: the reviewer picks.",)
        )
    if agreed:
        return MatchDecision(
            "needs_reviewer",
            None,
            (
                (
                    "Both AIs chose the same view, but code has no printed reference or clear geometry "
                    "winner of its own: the AIs' choice is shown as evidence, and the reviewer picks."
                ),
            ),
        )
    return MatchDecision(
        "needs_reviewer",
        None,
        ("Code has no printed reference or clear geometry winner: the reviewer picks.",),
    )
