"""The verdict table's own rules: the whole enum grid, and the invariant that closes the leak.

No client data and no drawings here: the table knows nothing about a finding, so these are facts
built by hand. The grid is small and closed (presence x presence x comparable x maths = 16
combinations), so it is walked whole rather than sampled; `hypothesis` is used only for the one
continuous part, the inch comparison.
"""

from __future__ import annotations

import itertools
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.review.assistant.verdict_facts import (
    ROWS,
    Absence,
    Comparable,
    Differs,
    Fact,
    Missing,
    NotCompared,
    Present,
    Same,
    VerdictError,
    same_inches,
    verdict,
)

GRID = list(itertools.product([True, False], repeat=4))
"""Every combination of: the vendor has a value, the reference has one, they are comparable, the
maths says they differ."""


def _fact(found: bool) -> Fact:
    return (
        Present(value='22"', unit="inches", inches=Decimal(22)) if found else Missing("unreadable")
    )


def _pair(comparable: bool) -> Comparable:
    return (
        Comparable(True) if comparable else Comparable(False, "different_reference", "unlike ends")
    )


# ---- the table is exhaustive and has no overlaps ------------------------------------------------


@pytest.mark.parametrize(("vendor", "reference", "comparable", "differs"), GRID)
def test_exactly_one_row_fires_for_every_combination(
    vendor: bool, reference: bool, comparable: bool, differs: bool
) -> None:
    fired = [row for row in ROWS if row.fires(vendor, reference, comparable, differs)]
    assert len(fired) == 1, f"{len(fired)} rows fired: {[row.id for row in fired]}"


def test_the_grid_is_covered_once_over() -> None:
    """No gap and no overlap, counted over the whole grid rather than cell by cell."""
    fired = [
        row.id for combination in GRID for row in ROWS if row.fires(*combination)  # type: ignore[misc]
    ]
    assert len(fired) == len(GRID) == 16
    assert set(fired) == {row.id for row in ROWS}


def test_every_row_is_reachable() -> None:
    for row in ROWS:
        assert any(row.fires(*combination) for combination in GRID), f"{row.id} never fires"


@pytest.mark.parametrize(("vendor", "reference", "comparable", "differs"), GRID)
def test_the_verdict_names_the_row_that_gave_it(
    vendor: bool, reference: bool, comparable: bool, differs: bool
) -> None:
    found = verdict(_fact(vendor), _fact(reference), _pair(comparable), differs)
    assert found.row in {row.id for row in ROWS}
    assert isinstance(found.outcome, (Same, Differs, NotCompared))


# ---- the invariant: a difference can never be filed as Not compared -----------------------------


@pytest.mark.parametrize("differs", [True, False])
def test_both_sides_present_and_comparable_is_never_not_compared(differs: bool) -> None:
    """The leak this replaces: an answer holding both values, measuring the same thing, filed as
    "Not compared" because some reason had been named for it."""
    found = verdict(_fact(True), _fact(True), Comparable(True), differs)
    assert not isinstance(found.outcome, NotCompared)
    assert found.differs is differs
    assert found.same is not differs
    assert found.row == ("V6" if differs else "V5")


def test_not_compared_cannot_be_built_from_a_bare_reason() -> None:
    with pytest.raises(VerdictError):
        NotCompared("unreadable")  # type: ignore[arg-type]


def test_an_absence_cannot_be_made_by_hand() -> None:
    with pytest.raises(VerdictError):
        Absence("pair", "different_reference", "because I say so")


def test_an_absence_refuses_a_side_that_has_a_value() -> None:
    with pytest.raises(VerdictError):
        Absence.of_fact("vendor", Present(value='22"'))


def test_an_absence_refuses_a_pair_that_is_comparable() -> None:
    with pytest.raises(VerdictError):
        Absence.of_pair(Comparable(True))


def test_a_pair_that_is_not_comparable_must_say_why() -> None:
    with pytest.raises(VerdictError):
        Comparable(False)


def test_a_comparable_pair_carries_no_reason() -> None:
    with pytest.raises(VerdictError):
        Comparable(True, "different_reference")


# ---- which absence each Not-compared row names --------------------------------------------------


def test_the_reason_comes_from_the_side_that_has_nothing() -> None:
    vendor = Missing("unreadable", "the vendor's value could not be read")
    reference = Present(value="2'-1\"", unit="inches", inches=Decimal(25))
    found = verdict(vendor, reference, Comparable(True), differs=True)
    assert isinstance(found.outcome, NotCompared)
    assert found.outcome.reason == "unreadable"
    assert found.absent is not None and found.absent.side == "vendor"


def test_the_reason_of_a_pair_sits_on_the_pair() -> None:
    pair = Comparable(
        False, "different_reference", "the architect's dimension ends at a centre line"
    )
    found = verdict(_fact(True), _fact(True), pair, differs=True)
    assert isinstance(found.outcome, NotCompared)
    assert found.absent is not None and found.absent.side == "pair"
    assert found.outcome.reason == "different_reference"
    assert found.row == "V4"


def test_neither_side_has_a_value() -> None:
    found = verdict(Missing("no_sheet"), Missing("nothing_to_compare"), Comparable(True), False)
    assert found.row == "V1"
    assert found.absent is not None and found.absent.side == "both"


# ---- the one continuous part: inches, compared exactly ------------------------------------------

#: Sizes a drawing can print, in inches: the settled rule is that inches count and are compared
#: exactly.
SIZES = st.decimals(min_value=0, max_value=400, places=4, allow_nan=False, allow_infinity=False)


@given(SIZES)
def test_a_size_agrees_with_itself(size: Decimal) -> None:
    assert same_inches(size, size)


@given(SIZES, SIZES)
def test_the_inch_comparison_does_not_care_which_side_it_is_given(
    vendor: Decimal, reference: Decimal
) -> None:
    assert same_inches(vendor, reference) == same_inches(reference, vendor)


@given(SIZES, SIZES)
def test_any_gap_at_all_is_a_difference(vendor: Decimal, reference: Decimal) -> None:
    """No tolerance (settled with GV): 22" against 22 1/2" differs, and so does 1/32"."""
    assert same_inches(vendor, reference) == (vendor == reference)


@given(SIZES, SIZES)
def test_the_table_follows_the_inch_comparison(vendor: Decimal, reference: Decimal) -> None:
    left = Present(value=str(vendor), unit="inches", inches=vendor)
    right = Present(value=str(reference), unit="inches", inches=reference)
    found = verdict(left, right, Comparable(True), not same_inches(vendor, reference))
    assert found.same == (vendor == reference)
    assert found.differs == (vendor != reference)
