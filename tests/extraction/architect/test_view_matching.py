"""Which architect view shows the vendor's countertop: code's half and decision D1 (#1166).

Verification for `extraction/architect/view_matching.py`. Invented geometry only: the vendor's view
at 2 pt per inch, the architect's at 3/2 pt per inch, pieces of 30, 24 and 18 inches (a 72-inch
run). No client value.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction

import pytest

from extraction.architect.pairing import DrawnRow, DrawnSpan, PairingSettings
from extraction.architect.view_matching import (
    ArchitectViewFacts,
    CodeMatch,
    CodeVerdict,
    MatchSettings,
    VendorViewFacts,
    decide_match,
    find_references,
    match_by_code,
    normalise_reference,
    reference_keys,
)

VENDOR_PT = Fraction(2)
ARCH_PT = Fraction(3, 2)
WIDTHS = (30, 24, 18)

PAIRING = PairingSettings(
    tick_tolerance_in=Fraction(1, 2),
    tolerance_fraction_of_smallest_bay=Fraction(1, 3),
    minimum_coincident_ticks=3,
    minimum_support_margin=2,
)
SETTINGS = MatchSettings(
    run_length_tolerance_in=Fraction(1),
    clear_margin_in=Fraction(3),
    shown_to_ais=3,
    pairing=PAIRING,
)


def drawn(
    key: str,
    widths: Sequence[int],
    *,
    scale: Fraction | None,
    start: int = 100,
    printed: str = "x",
    outline: bool | None = True,
) -> DrawnRow:
    unit = scale if scale is not None else Fraction(1)
    at = [Fraction(start)]
    for width in widths:
        at.append(at[-1] + width * unit)
    ticks = [Decimal(value.numerator) / Decimal(value.denominator) for value in at]
    return DrawnRow(
        key=key,
        spans=tuple(
            DrawnSpan(ticks[k], ticks[k + 1], outline, printed=f"{printed}{k}")
            for k in range(len(widths))
        ),
        overall=None,
        pt_per_inch=scale,
    )


def vendor(
    widths: Sequence[int] = WIDTHS,
    *,
    scale: Fraction | None = VENDOR_PT,
    references: tuple[str, ...] = (),
) -> VendorViewFacts:
    return VendorViewFacts(drawn("vendor", widths, scale=scale, outline=None), references)


def view(
    key: str,
    *widths: Sequence[int],
    bubble: str | None = None,
    sheet: str | None = "Z-101",
    separated: bool = True,
    scale: Fraction | None = ARCH_PT,
) -> ArchitectViewFacts:
    return ArchitectViewFacts(
        key=key,
        sheet_number=sheet,
        bubble=bubble,
        rows=tuple(
            drawn(f"arch-row:{n}", row, scale=scale) for n, row in enumerate(widths, start=1)
        ),
        separated=separated,
    )


SAME = view("same", WIDTHS, bubble="4 QX 1.1")
TWIN = view("twin", WIDTHS, bubble="6 QX 1.1")  # the same run and bays: a twin
FAR = view("far", (40, 40), bubble="2 QX 1.1")
CLOSE = view("close", (40, 34), bubble="3 QX 1.1")  # 74 in: 2 in off, inside 1 + 3
ROWLESS = view("rowless", bubble="5 QX 1.1")


# --- references ---------------------------------------------------------------------------------


def test_a_reference_is_compared_without_spaces_dashes_or_dots() -> None:
    assert normalise_reference("3 / a-401.") == "3/A401"
    assert normalise_reference("4/ID 7.4") == "4/ID74"


def test_references_are_found_in_printed_phrases_and_fractions_never_are() -> None:
    phrases = ["REF 3/A-401", "SEE ELEV 4 / ID 7.4", '3/4" PLY', "A-401", "1/2", '5/8"', "3/A-401"]

    assert find_references(phrases) == ("3/A401", "4/ID74")


def test_a_view_answers_to_its_bubble_mark_with_either_sheet() -> None:
    assert reference_keys("4 ID 7.4", "A-501") == frozenset({"4/ID74", "4/A501"})
    assert reference_keys("4", "A-501") == frozenset({"4/A501"})
    assert reference_keys(None, "A-501") == frozenset()


# --- code -----------------------------------------------------------------------------------------


def test_a_reference_to_exactly_one_view_is_codes_pick_even_against_its_geometry() -> None:
    """The vendor printed which view it is; a different run length is the mismatch type 1 exists
    to catch, so it is never a reason to doubt the reference."""
    off = replace(FAR, bubble="9 QX 1.1")
    code = match_by_code(vendor(references=("9/QX11",)), [SAME, off], SETTINGS)

    assert code.verdict is CodeVerdict.REFERENCE and code.pick == "far"
    assert [score.key for score in code.ranked] == ["far", "same"]
    assert code.ranked[0].reference_match


def test_two_views_answering_the_reference_do_not_decide() -> None:
    first = replace(SAME, bubble="4 QX 1.1")
    second = replace(TWIN, bubble="4 QX 1.1")
    code = match_by_code(vendor(references=("4/QX11",)), [first, second], SETTINGS)

    assert code.verdict is CodeVerdict.GEOMETRY_TIE and code.pick is None
    assert "references matching 2" in code.reasons[0]


def test_one_view_that_fits_clearly_is_codes_pick() -> None:
    code = match_by_code(vendor(), [FAR, SAME, ROWLESS], SETTINGS)

    assert code.verdict is CodeVerdict.GEOMETRY_CLEAR and code.pick == "same"
    best = code.ranked[0]
    assert (best.key, best.fits, best.run_length_error_in) == ("same", True, Fraction(0))
    assert (best.bays_vendor, best.bays_architect) == (3, 3)


def test_twins_tie_and_code_picks_nothing() -> None:
    code = match_by_code(vendor(), [SAME, TWIN, FAR], SETTINGS)

    assert code.verdict is CodeVerdict.GEOMETRY_TIE and code.pick is None
    assert [score.fits for score in code.ranked] == [True, True, False]


def test_one_length_alone_never_fits_however_exactly_it_matches() -> None:
    """A single span (two ticks) is one number: another drawing matches it by chance."""
    single = view("single", (72,))
    code = match_by_code(vendor(), [single, FAR], SETTINGS)

    assert code.verdict is CodeVerdict.GEOMETRY_NONE and code.pick is None
    assert code.ranked[0].run_length_error_in == 0 and not code.ranked[0].fits


def test_the_same_run_with_bays_that_do_not_line_up_never_fits() -> None:
    other_bays = view("other-bays", (36, 36))
    code = match_by_code(vendor(), [other_bays, FAR], SETTINGS)

    assert code.verdict is CodeVerdict.GEOMETRY_NONE
    assert code.ranked[0].ticks_aligned == 2, "both ends, not the inner tick"


def test_a_vendor_splitting_an_architect_bay_still_fits() -> None:
    """The vendor's 30 + 24 + 18 inside the architect's 30 + 42: every architect tick lands."""
    coarser = view("coarser", (30, 42))
    code = match_by_code(vendor(), [coarser, FAR], SETTINGS)

    assert code.verdict is CodeVerdict.GEOMETRY_CLEAR and code.pick == "coarser"
    assert code.ranked[0].ticks_aligned == 3


def test_one_fit_with_another_view_close_behind_is_a_tie() -> None:
    code = match_by_code(vendor(), [SAME, CLOSE], SETTINGS)

    assert code.verdict is CodeVerdict.GEOMETRY_TIE and code.pick is None
    assert "not a clear winner" in code.reasons[-1]


def test_no_view_fitting_is_geometry_none() -> None:
    code = match_by_code(vendor(), [FAR, CLOSE], SETTINGS)

    assert code.verdict is CodeVerdict.GEOMETRY_NONE and code.pick is None


@pytest.mark.parametrize(
    ("facts", "views"),
    [
        (vendor(scale=None), [SAME, TWIN]),
        (VendorViewFacts(None, ()), [SAME]),
        (vendor(), [ROWLESS, replace(SAME, rows=(drawn("arch-row:1", WIDTHS, scale=None),))]),
    ],
    ids=["vendor-scale-unknown", "no-vendor-row", "no-architect-row-with-a-scale"],
)
def test_without_geometry_code_picks_nothing_and_still_lists_every_view(
    facts: VendorViewFacts, views: list[ArchitectViewFacts]
) -> None:
    code = match_by_code(facts, views, SETTINGS)

    assert code.verdict is CodeVerdict.NO_GEOMETRY and code.pick is None
    assert sorted(score.key for score in code.ranked) == sorted(view.key for view in views)


def test_the_list_ranks_reference_then_fits_then_the_rest_then_rowless_views_in_order() -> None:
    rowless_b = replace(ROWLESS, key="rowless-b", bubble=None)
    referenced = replace(FAR, key="referenced", bubble="8 QX 1.1")
    code = match_by_code(
        vendor(references=("8/QX11",)),
        [ROWLESS, CLOSE, rowless_b, SAME, referenced, FAR],
        SETTINGS,
    )

    assert [score.key for score in code.ranked] == [
        "referenced",
        "same",
        "close",
        "far",
        "rowless",
        "rowless-b",
    ]
    assert [score.rank for score in code.ranked] == [1, 2, 3, 4, 5, 6]


def test_printed_text_never_changes_codes_judgment() -> None:
    renamed = replace(SAME, rows=(drawn("arch-row:1", WIDTHS, scale=ARCH_PT, printed="99"),))
    first = match_by_code(vendor(), [SAME, FAR], SETTINGS)
    second = match_by_code(vendor(), [renamed, FAR], SETTINGS)

    assert (first.verdict, first.pick) == (second.verdict, second.pick)
    assert [s.run_length_error_in for s in first.ranked] == [
        s.run_length_error_in for s in second.ranked
    ]


def test_the_best_row_of_a_view_counts() -> None:
    two_rows = view("two-rows", (40, 40), WIDTHS)
    code = match_by_code(vendor(), [two_rows, FAR], SETTINGS)

    assert code.pick == "two-rows" and code.ranked[0].best_row_key == "arch-row:2"


def test_two_views_with_one_key_are_refused() -> None:
    with pytest.raises(ValueError, match="unique"):
        match_by_code(vendor(), [SAME, replace(TWIN, key="same")], SETTINGS)


def test_settings_are_checked() -> None:
    with pytest.raises(ValueError):
        replace(SETTINGS, run_length_tolerance_in=Fraction(0))
    with pytest.raises(ValueError):
        replace(SETTINGS, clear_margin_in=Fraction(-1))
    with pytest.raises(ValueError):
        replace(SETTINGS, shown_to_ais=0)


# --- decision D1 ------------------------------------------------------------------------------------


def _code(verdict: CodeVerdict, pick: str | None) -> CodeMatch:
    return CodeMatch(verdict, pick, (), ("test",))


VIEWS = [SAME, TWIN, FAR]
SHOWN = ["same", "twin", "far"]


@pytest.mark.parametrize(
    ("verdict", "pick", "ai_picks", "shown", "status", "chosen"),
    [
        (CodeVerdict.REFERENCE, "same", ["same", "same"], SHOWN, "auto_matched", "same"),
        (CodeVerdict.GEOMETRY_CLEAR, "same", ["same", "same"], SHOWN, "auto_matched", "same"),
        (CodeVerdict.GEOMETRY_CLEAR, "same", ["same", "twin"], SHOWN, "needs_reviewer", None),
        (CodeVerdict.GEOMETRY_CLEAR, "same", ["twin", "twin"], SHOWN, "needs_reviewer", None),
        (CodeVerdict.GEOMETRY_CLEAR, "same", ["same", None], SHOWN, "needs_reviewer", None),
        (CodeVerdict.GEOMETRY_CLEAR, "same", [None, None], SHOWN, "needs_reviewer", None),
        (CodeVerdict.GEOMETRY_CLEAR, "same", ["same"], SHOWN, "needs_reviewer", None),
        (CodeVerdict.GEOMETRY_CLEAR, "same", [], SHOWN, "needs_reviewer", None),
        (CodeVerdict.GEOMETRY_CLEAR, "same", ["same", "same"], ["twin"], "needs_reviewer", None),
        (CodeVerdict.GEOMETRY_TIE, None, ["same", "same"], SHOWN, "needs_reviewer", None),
        (CodeVerdict.GEOMETRY_NONE, None, ["same", "same"], SHOWN, "needs_reviewer", None),
        (CodeVerdict.NO_GEOMETRY, None, ["same", "same"], SHOWN, "needs_reviewer", None),
        (CodeVerdict.NO_GEOMETRY, None, [None, None], SHOWN, "needs_reviewer", None),
    ],
    ids=[
        "reference-and-both-ais",
        "geometry-and-both-ais",
        "ais-disagree",
        "both-ais-pick-another-view",
        "one-ai-unsure-or-silent",
        "both-ais-none",
        "only-one-ai",
        "ais-not-asked",
        "codes-pick-not-shown",
        "twins-with-both-ais-agreeing",
        "no-fit-with-both-ais-agreeing",
        "no-geometry-with-both-ais-agreeing",
        "nothing",
    ],
)
def test_decision_d1(
    verdict: CodeVerdict,
    pick: str | None,
    ai_picks: list[str | None],
    shown: list[str],
    status: str,
    chosen: str | None,
) -> None:
    decision = decide_match(_code(verdict, pick), ai_picks, shown, VIEWS)

    assert (decision.status, decision.chosen) == (status, chosen)
    assert decision.reasons


def test_the_ais_agreeing_without_code_is_evidence_only() -> None:
    decision = decide_match(_code(CodeVerdict.GEOMETRY_TIE, None), ["twin", "twin"], SHOWN, VIEWS)

    assert decision.status == "needs_reviewer" and decision.chosen is None
    assert "evidence" in decision.reasons[0]


def test_three_judgments_on_a_view_not_clearly_apart_name_it_and_read_nothing() -> None:
    crowded = replace(SAME, separated=False)
    decision = decide_match(
        _code(CodeVerdict.GEOMETRY_CLEAR, "same"), ["same", "same"], SHOWN, [crowded, TWIN]
    )

    assert (decision.status, decision.chosen) == ("not_separated", "same")


def test_no_view_at_all_is_no_candidates() -> None:
    decision = decide_match(_code(CodeVerdict.NO_GEOMETRY, None), [None, None], [], [])

    assert (decision.status, decision.chosen) == ("no_candidates", None)
