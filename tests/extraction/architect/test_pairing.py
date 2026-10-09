"""Pairing the architect's drawn spans with the vendor's countertop row by drawn position (#1053).

Verification for: `extraction/architect/pairing.py`.

**Every row here is synthetic.** Widths are written in real inches and placed on an imaginary page
at a stated scale (page points per real inch) and a stated left edge, so each test reads as "this
is what is drawn". No client drawing, number or page appears in this file.

The tests worth reading first:

* `test_printed_values_never_change_the_pairing` — pairing by printed numbers is circular (it can
  only ever pair numbers that already agree), so the algorithm must not read them at all;
* `test_a_real_half_inch_mismatch_still_pairs_so_the_compare_can_fail` — the reason pairing is by
  position: a vendor piece drawn 1/2" narrower than the architect's bay must still be paired;
* `test_a_centre_line_span_is_never_paired_even_when_it_aligns_perfectly` — the D4 false-PASS
  guard: an architect dimension between two outlet centres that happens to equal a cabinet width.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction

import pytest

from extraction.architect.pairing import (
    DrawnRow,
    DrawnSpan,
    PairingResult,
    PairingSettings,
    PairingStatus,
    PiecePair,
    pair_rows,
)

# 1/2" = 1'-0" is 36 page points per 12 real inches: 3 pt per inch.
ARCH_PPI = Fraction(3)
# 1:15 is 72/15 page points per real inch: 4.8 pt per inch.
VENDOR_PPI = Fraction(24, 5)

SETTINGS = PairingSettings(
    tick_tolerance_in=Fraction(1),
    tolerance_fraction_of_smallest_bay=Fraction(1, 3),
    minimum_coincident_ticks=3,
    minimum_support_margin=1,
)


def _pt(value: Fraction) -> Decimal:
    """A page position as an exact Decimal; the fixtures only use scales that give finite ones."""
    decimal = Decimal(value.numerator) / Decimal(value.denominator)
    assert Fraction(decimal) == value, f"{value} is not a finite decimal"
    return decimal


def _row(
    key: str,
    widths_in: Sequence[Fraction | int | str],
    *,
    ppi: Fraction | None,
    left_pt: Fraction | int,
    outline: Sequence[bool | None] | bool | None = True,
    overall: bool | None | str = "absent",
    printed: Sequence[str] | None = None,
) -> DrawnRow:
    """A chain of `widths_in` real inches drawn at `ppi` from `left_pt`.

    `overall`: "absent" for none; otherwise an overall spanning the whole chain whose `on_outline`
    is this value.
    """
    scale = ppi if ppi is not None else Fraction(3)
    if isinstance(outline, Sequence):
        flags = list(outline)
    else:
        flags = [outline] * len(widths_in)
    labels = list(printed) if printed is not None else [f"label {i}" for i in range(len(widths_in))]
    spans: list[DrawnSpan] = []
    x = Fraction(left_pt)
    for width, flag, label in zip(widths_in, flags, labels, strict=True):
        end = x + Fraction(width) * scale
        spans.append(DrawnSpan(x0_pt=_pt(x), x1_pt=_pt(end), on_outline=flag, printed=label))
        x = end
    whole = (
        None
        if overall == "absent"
        else DrawnSpan(
            x0_pt=_pt(Fraction(left_pt)),
            x1_pt=_pt(x),
            on_outline=None if overall is None else bool(overall),
            printed="overall label",
        )
    )
    return DrawnRow(key=key, spans=tuple(spans), overall=whole, pt_per_inch=ppi)


def _vendor(
    widths_in: Sequence[Fraction | int | str],
    *,
    left_pt: Fraction | int = 50,
    overall: bool = False,
    ppi: Fraction | None = VENDOR_PPI,
    printed: Sequence[str] | None = None,
) -> DrawnRow:
    """The vendor's row: `on_outline` is unknown (None) on the vendor's side by contract."""
    return _row(
        "vendor",
        widths_in,
        ppi=ppi,
        left_pt=left_pt,
        outline=None,
        overall=None if overall else "absent",
        printed=printed,
    )


def _pieces(result: PairingResult) -> dict[int | str, tuple[int, ...]]:
    return {
        pair.architect_span_index: pair.vendor_span_indices
        for pair in result.pairs
        if pair.kind == "piece"
    }


def _excluded(result: PairingResult) -> dict[int | str, str]:
    return dict(result.excluded)


# --- fits ---------------------------------------------------------------------------------------


def test_an_exact_fit_at_different_scales_and_offsets_pairs_every_bay() -> None:
    vendor = _vendor([24, 30, 18], left_pt=50)
    architect = _row("A", [24, 30, 18], ppi=ARCH_PPI, left_pt=400)

    result = pair_rows(vendor, [architect], SETTINGS)

    assert result.status is PairingStatus.PAIRED
    assert result.architect_row_key == "A"
    assert result.offset_in == 0
    assert result.support == 4
    assert _pieces(result) == {0: (0,), 1: (1,), 2: (2,)}
    for pair in result.pairs:
        assert pair.start_error_in == 0 and pair.end_error_in == 0
        assert isinstance(pair.start_error_in, Fraction)


def test_the_vendor_may_split_one_architect_bay_into_two_pieces() -> None:
    vendor = _vendor([24, 18, 18])
    architect = _row("A", [24, 36], ppi=ARCH_PPI, left_pt=10)

    result = pair_rows(vendor, [architect], SETTINGS)

    assert result.status is PairingStatus.PAIRED
    assert _pieces(result) == {0: (0,), 1: (1, 2)}


def test_one_vendor_piece_never_covers_two_architect_bays() -> None:
    """The reverse of a split: the architect's middle tick lands on no vendor tick, so neither
    architect bay has both ends on vendor ticks and neither is paired."""
    vendor = _vendor([24, 36, 12])
    architect = _row("A", [24, 18, 18, 12], ppi=ARCH_PPI, left_pt=10)

    result = pair_rows(vendor, [architect], SETTINGS)

    assert result.status is PairingStatus.PAIRED
    assert _pieces(result) == {0: (0,), 3: (2,)}
    assert "does not land on a vendor tick" in _excluded(result)[1]
    assert "does not land on a vendor tick" in _excluded(result)[2]


def test_the_vendor_run_may_be_a_sub_range_of_a_whole_wall_elevation() -> None:
    vendor = _vendor([24, 30, 18])
    architect = _row("A", [12, 24, 30, 18, 20], ppi=ARCH_PPI, left_pt=10)

    result = pair_rows(vendor, [architect], SETTINGS)

    assert result.status is PairingStatus.PAIRED
    assert result.offset_in == -12
    assert _pieces(result) == {1: (0,), 2: (1,), 3: (2,)}
    assert set(_excluded(result)) == {0, 4}


def test_a_real_half_inch_mismatch_still_pairs_so_the_compare_can_fail() -> None:
    """The vendor's middle piece is drawn 1/2" narrower than the architect's bay at the same
    scale. Pairing by printed value could never pair these; pairing by position must."""
    vendor = _vendor([24, Fraction(47, 2), 24])
    architect = _row("A", [24, 24, 24], ppi=ARCH_PPI, left_pt=10)

    result = pair_rows(vendor, [architect], SETTINGS)

    assert result.status is PairingStatus.PAIRED
    assert _pieces(result) == {0: (0,), 1: (1,), 2: (2,)}
    middle = next(pair for pair in result.pairs if pair.architect_span_index == 1)
    # The two ends' errors differ by exactly the drawn mismatch.
    assert middle.end_error_in - middle.start_error_in == Fraction(-1, 2)


def test_the_vendor_overall_pairs_with_the_architect_overall() -> None:
    vendor = _vendor([24, 30, 18], overall=True)
    architect = _row("A", [24, 30, 18], ppi=ARCH_PPI, left_pt=10, overall=True)

    result = pair_rows(vendor, [architect], SETTINGS)

    overall = [pair for pair in result.pairs if pair.kind == "overall"]
    assert [pair.architect_span_index for pair in overall] == ["overall"]
    assert overall[0].vendor_span_indices == (0, 1, 2)
    assert _pieces(result) == {0: (0,), 1: (1,), 2: (2,)}


def test_an_architect_bay_spanning_the_whole_vendor_row_pairs_as_the_overall() -> None:
    """A unit-width bay whose ends land on the vendor row's two ends pairs with the vendor's
    overall (a printed value), not with the sum of its pieces."""
    vendor = _vendor([3, 14, 1, 19, 3], overall=True)
    architect = _row("A", [6, 40, 8], ppi=ARCH_PPI, left_pt=10)

    settings = replace(SETTINGS, minimum_coincident_ticks=2)
    result = pair_rows(vendor, [architect], settings)

    assert result.status is PairingStatus.PAIRED
    assert [(p.kind, p.architect_span_index, p.vendor_span_indices) for p in result.pairs] == [
        ("overall", 1, (0, 1, 2, 3, 4))
    ]


def test_without_a_vendor_overall_the_whole_chain_is_the_row_end_to_end() -> None:
    vendor = _vendor([24, 30, 18])
    architect = _row("A", [24, 30, 18], ppi=ARCH_PPI, left_pt=10, overall=True)

    result = pair_rows(vendor, [architect], SETTINGS)

    overall = [pair for pair in result.pairs if pair.kind == "overall"]
    assert [(p.architect_span_index, p.vendor_span_indices) for p in overall] == [
        ("overall", (0, 1, 2))
    ]


# --- printed values never decide ---------------------------------------------------------------


def test_printed_values_never_change_the_pairing() -> None:
    widths = [24, 30, 18]
    vendor = _vendor(widths, overall=True, printed=["24", "30", "18"])
    architect = _row(
        "A", [12, *widths, 20], ppi=ARCH_PPI, left_pt=10, printed=["1'", "2'", "2'-6\"", "18", "x"]
    )
    before = pair_rows(vendor, [architect], SETTINGS)

    garbled_vendor = _vendor(widths, overall=True, printed=["99", "VIF", ""])
    garbled_architect = _row(
        "A", [12, *widths, 20], ppi=ARCH_PPI, left_pt=10, printed=["30", "30", "30", "30", "30"]
    )
    after = pair_rows(garbled_vendor, [garbled_architect], SETTINGS)

    assert before == after
    assert before.status is PairingStatus.PAIRED


def test_printed_values_that_disagree_wildly_still_pair_by_position() -> None:
    vendor = _vendor([24, 30, 18], printed=["1", "2", "3"])
    architect = _row("A", [24, 30, 18], ppi=ARCH_PPI, left_pt=10, printed=["70", "80", "90"])

    assert _pieces(pair_rows(vendor, [architect], SETTINGS)) == {0: (0,), 1: (1,), 2: (2,)}


# --- the outline guard (D4) --------------------------------------------------------------------


def test_a_centre_line_span_is_never_paired_even_when_it_aligns_perfectly() -> None:
    """The trap: an architect dimension between two outlet centres that happens to equal a
    cabinet's width and whose ticks land exactly on that cabinet's edges. Pairing it would make the
    later compare PASS the cabinet against a number that is not its width."""
    vendor = _vendor([18, 30, 18])
    architect = _row("A", [18, 30, 18], ppi=ARCH_PPI, left_pt=10, outline=[True, False, True])

    result = pair_rows(vendor, [architect], SETTINGS)

    assert result.status is PairingStatus.PAIRED
    assert _pieces(result) == {0: (0,), 2: (2,)}
    assert "centre line" in _excluded(result)[1]
    assert all(pair.architect_span_index != 1 for pair in result.pairs)


def test_a_row_of_centre_lines_only_is_nothing_comparable() -> None:
    vendor = _vendor([18, 30, 18])
    architect = _row("A", [18, 30, 18], ppi=ARCH_PPI, left_pt=10, outline=False)

    result = pair_rows(vendor, [architect], SETTINGS)

    assert result.status is PairingStatus.NOTHING_COMPARABLE
    assert result.pairs == ()


def test_an_unknown_outline_is_never_paired() -> None:
    vendor = _vendor([18, 30, 18])
    architect = _row("A", [18, 30, 18], ppi=ARCH_PPI, left_pt=10, outline=[True, None, True])

    result = pair_rows(vendor, [architect], SETTINGS)

    assert _pieces(result) == {0: (0,), 2: (2,)}
    assert "outline unknown" in _excluded(result)[1]


def test_an_all_unknown_outline_is_nothing_comparable() -> None:
    vendor = _vendor([18, 30, 18])
    architect = _row("A", [18, 30, 18], ppi=ARCH_PPI, left_pt=10, outline=None, overall=None)

    assert pair_rows(vendor, [architect], SETTINGS).status is PairingStatus.NOTHING_COMPARABLE


def test_an_architect_overall_with_an_unknown_outline_is_excluded() -> None:
    vendor = _vendor([24, 30, 18], overall=True)
    architect = _row("A", [24, 30, 18], ppi=ARCH_PPI, left_pt=10, overall=None)

    result = pair_rows(vendor, [architect], SETTINGS)

    assert all(pair.kind == "piece" for pair in result.pairs)
    assert "outline unknown" in _excluded(result)["overall"]


def test_no_architect_rows_is_nothing_comparable() -> None:
    result = pair_rows(_vendor([24, 30]), [], SETTINGS)

    assert result.status is PairingStatus.NOTHING_COMPARABLE
    assert result.architect_row_key is None


# --- refusals ------------------------------------------------------------------------------------


def test_two_architect_rows_aligning_equally_are_ambiguous() -> None:
    vendor = _vendor([24, 30, 18])
    upper = _row("upper", [24, 30, 18], ppi=ARCH_PPI, left_pt=10)
    lower = _row("lower", [24, 30, 18], ppi=ARCH_PPI, left_pt=10)

    result = pair_rows(vendor, [upper, lower], SETTINGS)

    assert result.status is PairingStatus.AMBIGUOUS
    assert result.support == result.runner_up_support == 4
    assert result.pairs == ()
    assert result.architect_row_key is None


def test_nothing_aligning_is_no_fit() -> None:
    vendor = _vendor([24, 24, 24])
    architect = _row("A", [10, 50, 7], ppi=ARCH_PPI, left_pt=10)

    result = pair_rows(vendor, [architect], SETTINGS)

    assert result.status is PairingStatus.NO_FIT
    assert result.support < SETTINGS.minimum_coincident_ticks
    assert result.pairs == ()


@pytest.mark.parametrize("missing", ["vendor", "architect"])
def test_a_missing_scale_is_no_scale(missing: str) -> None:
    vendor = _vendor([24, 30, 18], ppi=None if missing == "vendor" else VENDOR_PPI)
    architect = _row(
        "A", [24, 30, 18], ppi=None if missing == "architect" else ARCH_PPI, left_pt=10
    )

    result = pair_rows(vendor, [architect], SETTINGS)

    assert result.status is PairingStatus.NO_SCALE
    assert result.pairs == ()


def test_the_minimum_support_margin_is_respected() -> None:
    """Equal bays repeat: shifting the architect's row one bay along still lands three of its four
    ticks. A margin of one accepts the fit; a margin of two refuses it."""
    vendor = _vendor([24, 24, 24])
    architect = _row("A", [24, 24, 24], ppi=ARCH_PPI, left_pt=10)

    accepted = pair_rows(vendor, [architect], SETTINGS)
    refused = pair_rows(vendor, [architect], replace(SETTINGS, minimum_support_margin=2))

    assert accepted.status is PairingStatus.PAIRED
    assert (accepted.support, accepted.runner_up_support) == (4, 3)
    assert refused.status is PairingStatus.AMBIGUOUS
    assert refused.pairs == ()


def test_the_minimum_coincident_ticks_is_respected() -> None:
    vendor = _vendor([24, 30, 18])
    architect = _row("A", [24, 30, 18], ppi=ARCH_PPI, left_pt=10)

    result = pair_rows(vendor, [architect], replace(SETTINGS, minimum_coincident_ticks=5))

    assert result.status is PairingStatus.NO_FIT


# --- tolerance ----------------------------------------------------------------------------------


def test_drift_beyond_tolerance_at_the_far_end_leaves_the_far_span_unpaired() -> None:
    """The vendor's drawing creeps away from the architect's towards the right: within tolerance
    for the near bays, beyond it at the far end. The near bays pair; the far one does not."""
    vendor = _vendor(["20", "28", "16.2", "24.4", "32"])
    architect = _row("A", [20, 28, 16, 24, 30], ppi=ARCH_PPI, left_pt=10)

    result = pair_rows(vendor, [architect], SETTINGS)

    assert result.status is PairingStatus.PAIRED
    assert _pieces(result) == {0: (0,), 1: (1,), 2: (2,), 3: (3,)}
    assert "does not land on a vendor tick" in _excluded(result)[4]


def test_the_tolerance_never_reaches_past_a_fraction_of_the_smallest_bay() -> None:
    """With a 3" bay the tolerance is 1" (a third), not the 2" asked for, so the far bays, drawn
    3" further right by the vendor, no longer coincide with the near ones' alignment."""
    vendor = _vendor([20, 28, 6, 17])
    architect = _row("A", [20, 28, 3, 17], ppi=ARCH_PPI, left_pt=10)

    result = pair_rows(vendor, [architect], replace(SETTINGS, tick_tolerance_in=Fraction(2)))

    assert result.status is PairingStatus.PAIRED
    assert result.tolerance_in == 1
    assert _pieces(result) == {0: (0,), 1: (1,)}
    assert 2 not in _pieces(result)


def test_an_architect_tick_near_two_vendor_ticks_pairs_neither() -> None:
    """A 3/4" scribe beside a cabinet: the architect's wall tick lands within tolerance of both the
    scribe's ticks, so which piece the architect's bay includes is not decided by position — even
    at an offset that would reach only one of them, since the choice would rest on the tolerance's
    edge, not on the drawing."""
    vendor = _vendor([Fraction(3, 4), Fraction(93, 4), 24, 30])
    architect = _row("A", [24, 24, 30], ppi=ARCH_PPI, left_pt=10)

    result = pair_rows(vendor, [architect], SETTINGS)

    assert result.status is PairingStatus.PAIRED
    assert _pieces(result) == {1: (2,), 2: (3,)}
    assert "two vendor ticks" in _excluded(result)[0]


# --- determinism and contracts ---------------------------------------------------------------


def test_the_order_of_the_architect_rows_does_not_matter() -> None:
    vendor = _vendor([24, 30, 18])
    rows = [
        _row("A", [24, 30, 18], ppi=ARCH_PPI, left_pt=10),
        _row("B", [10, 50, 7], ppi=ARCH_PPI, left_pt=10),
        _row("C", [5, 5, 5], ppi=ARCH_PPI, left_pt=200, outline=False),
    ]

    forward = pair_rows(vendor, rows, SETTINGS)
    backward = pair_rows(vendor, list(reversed(rows)), SETTINGS)

    assert forward == backward
    assert forward.architect_row_key == "A"


def test_the_same_input_gives_the_same_output() -> None:
    vendor = _vendor([24, 18, 18, 30])
    architect = _row("A", [12, 24, 36, 30, 9], ppi=ARCH_PPI, left_pt=10)

    assert pair_rows(vendor, [architect], SETTINGS) == pair_rows(vendor, [architect], SETTINGS)


def test_every_number_in_the_result_is_exact() -> None:
    vendor = _vendor([24, Fraction(47, 2), 24], overall=True)
    architect = _row("A", [24, 24, 24], ppi=ARCH_PPI, left_pt=10, overall=True)

    result = pair_rows(vendor, [architect], SETTINGS)

    assert isinstance(result.offset_in, Fraction)
    assert isinstance(result.tolerance_in, Fraction)
    for pair in result.pairs:
        assert isinstance(pair, PiecePair)
        assert isinstance(pair.start_error_in, Fraction)
        assert isinstance(pair.end_error_in, Fraction)
        assert abs(pair.start_error_in) <= result.tolerance_in
        assert abs(pair.end_error_in) <= result.tolerance_in


def test_every_result_explains_itself_in_plain_english() -> None:
    vendor = _vendor([24, 30, 18])
    for rows in ([], [_row("A", [24, 30, 18], ppi=ARCH_PPI, left_pt=10)]):
        assert pair_rows(vendor, rows, SETTINGS).reasons


def test_spans_that_are_not_contiguous_are_refused() -> None:
    with pytest.raises(ValueError, match="contiguous"):
        DrawnRow(
            key="A",
            spans=(
                DrawnSpan(x0_pt=Decimal(0), x1_pt=Decimal(10), on_outline=True),
                DrawnSpan(x0_pt=Decimal(11), x1_pt=Decimal(20), on_outline=True),
            ),
            overall=None,
            pt_per_inch=ARCH_PPI,
        )


def test_a_span_drawn_backwards_is_refused() -> None:
    with pytest.raises(ValueError, match="x0"):
        DrawnSpan(x0_pt=Decimal(10), x1_pt=Decimal(10), on_outline=True)


def test_duplicate_architect_row_keys_are_refused() -> None:
    row = _row("A", [24, 30], ppi=ARCH_PPI, left_pt=10)
    with pytest.raises(ValueError, match="key"):
        pair_rows(_vendor([24, 30]), [row, row], SETTINGS)


@pytest.mark.parametrize(
    "change",
    [
        {"tick_tolerance_in": Fraction(0)},
        {"tolerance_fraction_of_smallest_bay": Fraction(1, 2)},
        {"tolerance_fraction_of_smallest_bay": Fraction(0)},
        {"minimum_coincident_ticks": 1},
        {"minimum_support_margin": 0},
    ],
)
def test_settings_that_would_let_neighbouring_ticks_both_coincide_are_refused(
    change: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        replace(SETTINGS, **change)  # type: ignore[arg-type]


def test_a_scale_that_is_not_positive_is_refused() -> None:
    span = DrawnSpan(x0_pt=Decimal(0), x1_pt=Decimal(10), on_outline=True)
    with pytest.raises(ValueError, match="pt_per_inch"):
        DrawnRow(key="A", spans=(span,), overall=None, pt_per_inch=Fraction(0))


class _Untouchable(str):
    """A printed label that raises if anything reads, compares, parses or formats it."""

    def _refuse(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("the pairing read a printed value")

    __eq__ = __ne__ = __lt__ = __hash__ = __len__ = __iter__ = __getitem__ = _refuse  # type: ignore[assignment]
    __str__ = __format__ = __contains__ = __add__ = __float__ = __int__ = _refuse  # type: ignore[assignment]
    split = strip = replace = lower = upper = startswith = endswith = _refuse  # type: ignore[assignment]


def test_the_pairing_never_touches_a_printed_value() -> None:
    labels = [_Untouchable("24"), _Untouchable("30"), _Untouchable("18")]
    vendor = _vendor([24, 30, 18], overall=True, printed=labels)
    architect = _row("A", [24, 30, 18], ppi=ARCH_PPI, left_pt=10, overall=True, printed=labels)

    result = pair_rows(vendor, [architect], SETTINGS)

    assert result.status is PairingStatus.PAIRED
