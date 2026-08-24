"""Which revision governs (#184, B11.2).

`docs/DESIGN_EXTRACTION.md` §9 asks for refusal tests on every resolver that can fail and calls them the
safety-critical ones. This resolver can fail in five distinct ways and each has its own test, because
`#185` branches on the cause: a sheet with no number needs a different conversation with the vendor than
two sheets both stamped Rev B.

The positive cases are deliberately few. Getting "C governs" right matters much less than never saying it
when the drawings did not: every other guard in the system assumes the source page was the right page,
and this is the only place that assumption is checked.

Source: `docs/DESIGN_EXTRACTION.md` §7 · Verification: this file
"""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal

import pytest

from extraction.manifest import PageRecord
from extraction.revision import RevisionBlock, RevisionDate, RevisionHistoryRow, RevisionLabel
from extraction.supersession import (
    DATE_CONTRADICTS_ORDER,
    DUPLICATE_REVISION,
    NO_RECORDED_ORDER,
    NO_SHEET_NUMBER,
    UNKNOWN_REVISION,
    GoverningRevision,
    SheetPage,
    Unresolved,
    governing_revision,
    group_by_sheet,
    recorded_order,
)


def _page(index: int, sheet_number: str | None) -> PageRecord:
    return PageRecord(
        index=index,
        content_hash=hashlib.sha256(f"page-{index}".encode()).hexdigest(),
        width_pt=Decimal(842),
        height_pt=Decimal(595),
        rotation=0,
        has_vector_text=True,
        render_failed=False,
        sheet_number=sheet_number,
    )


def _history(*labels: str, dates: dict[str, date] | None = None) -> tuple[RevisionHistoryRow, ...]:
    """A revision history table listing `labels` in order — the drawing stating its own sequence."""
    dates = dates or {}
    rows = []
    for position, label in enumerate(labels):
        when = dates.get(label)
        rows.append(
            RevisionHistoryRow(
                RevisionLabel(
                    label,
                    RevisionDate(when.isoformat(), (when,)) if when else None,
                    sequence_index=position,
                ),
                description="ISSUED",
            )
        )
    return tuple(rows)


def _sheet(
    index: int, number: str | None, *listed: str, dates: dict[str, date] | None = None
) -> SheetPage:
    """A page whose history lists `listed`, the last of which is its current revision."""
    history = _history(*listed, dates=dates)
    return SheetPage(
        _page(index, number), RevisionBlock(current=history[-1].label, history=history)
    )


def _unknown(index: int, number: str | None) -> SheetPage:
    """A page whose title block said nothing about its revision."""
    return SheetPage(_page(index, number), RevisionBlock())


# ---------------------------------------------------------------------------
# Identity is the sheet number
# ---------------------------------------------------------------------------


def test_pages_are_grouped_by_sheet_number_not_by_page_order() -> None:
    """The first acceptance criterion. Interleaved on purpose.

    A package's pages arrive in whatever order the vendor bound them, so grouping that depended on
    adjacency would work on a tidy package and fail on a real one.
    """
    pages = [
        _sheet(0, "A-101", "A"),
        _sheet(1, "A-201", "A"),
        _sheet(2, "A-101", "A", "B"),
        _sheet(3, "A-201", "A", "B"),
    ]

    grouped = group_by_sheet(pages)

    assert set(grouped) == {"A-101", "A-201"}
    assert [p.page.index for p in grouped["A-101"]] == [0, 2]
    assert [p.page.index for p in grouped["A-201"]] == [1, 3]


def test_pages_without_a_sheet_number_are_grouped_apart_not_invented_into_a_sheet() -> None:
    """A page with no sheet number has no identity to group by.

    Grouping it by filename or page position is precisely what §7 forbids, so it lands under `None` and
    the resolver refuses it — visibly, rather than by being quietly attached to a neighbour.
    """
    grouped = group_by_sheet([_sheet(0, "A-101", "A"), _sheet(1, None, "A")])

    assert set(grouped) == {"A-101", None}
    assert len(grouped[None]) == 1


def test_a_sheet_with_no_number_is_unresolved() -> None:
    """And it says which cause, so #185 can act on it rather than parse a sentence."""
    outcome = governing_revision(list(group_by_sheet([_sheet(0, None, "A")])[None]))

    assert isinstance(outcome, Unresolved)
    assert outcome.cause == NO_SHEET_NUMBER
    assert not outcome.is_resolved
    assert "never the filename or the page order" in outcome.detail


def test_mixing_sheets_in_one_call_is_a_programming_error() -> None:
    """Refused loudly rather than resolved for whichever sheet happened to be first.

    Silently picking one would produce a governing revision for a sheet nobody asked about, which is a
    wrong answer dressed as a right one.
    """
    with pytest.raises(ValueError, match="one sheet"):
        governing_revision([_sheet(0, "A-101", "A"), _sheet(1, "A-201", "A")])


# ---------------------------------------------------------------------------
# Ordering comes from the drawing
# ---------------------------------------------------------------------------


def test_the_later_revision_governs_when_a_history_records_the_order() -> None:
    """The ordinary case: the Rev B sheet's own table lists A then B, so B is later. Stated, not inferred."""
    earlier = _sheet(0, "A-101", "A")
    later = _sheet(1, "A-101", "A", "B")

    outcome = governing_revision([earlier, later])

    assert isinstance(outcome, GoverningRevision)
    assert outcome.governing is later
    assert outcome.sheet_number == "A-101"


def test_the_order_comes_from_the_history_and_not_from_the_spelling() -> None:
    """The safety property, tested where a collation would give the opposite answer.

    `AA` sorts before `B` alphabetically. Here the drawing's history lists `B` then `AA`, so `AA` is
    later — and any implementation that sorted labels would confidently choose `B` and read a superseded
    sheet.
    """
    outcome = governing_revision([_sheet(0, "A-101", "B"), _sheet(1, "A-101", "B", "AA")])

    assert isinstance(outcome, GoverningRevision)
    assert outcome.governing.label == "AA", "a spelling-based collation would have chosen B"


def test_a_numeric_restart_does_not_pick_the_wrong_sheet() -> None:
    """`10` sorts before `9` as text and after it as a number; the history settles it either way."""
    outcome = governing_revision([_sheet(0, "A-101", "9"), _sheet(1, "A-101", "9", "10")])

    assert isinstance(outcome, GoverningRevision)
    assert outcome.governing.label == "10"


def test_no_recorded_order_is_unresolved_rather_than_highest_letter_wins() -> None:
    """§7: supersession *"never resolves to 'the last page wins' or 'the highest letter wins'"*.

    Two pages, each with only its own label and no history connecting them. Every convention a person
    might reach for — later letter, later page, longer label — would answer confidently here, and this
    refuses instead.
    """
    solo_a = SheetPage(_page(0, "A-101"), RevisionBlock(current=RevisionLabel("A")))
    solo_c = SheetPage(_page(1, "A-101"), RevisionBlock(current=RevisionLabel("C")))

    outcome = governing_revision([solo_a, solo_c])

    assert isinstance(outcome, Unresolved)
    assert outcome.cause == NO_RECORDED_ORDER
    assert "highest letter wins" in outcome.detail
    assert len(outcome.candidates) == 2, "a reviewer needs both sheets, not a message about a gap"


def test_recorded_order_reads_the_union_of_the_history_tables() -> None:
    """One sheet listing A, B, C orders all three, even though only C is printed on it."""
    order = recorded_order([_sheet(0, "A-101", "A"), _sheet(1, "A-101", "A", "B", "C")])

    assert order == {"A": 0, "B": 1, "C": 2}


def test_a_label_in_no_history_table_is_absent_from_the_order() -> None:
    """Not knowing where a revision sits is different from it being early.

    Absence here is what makes the resolver refuse; a default of 0 would silently make an unplaced
    revision the earliest and let the others supersede it.
    """
    order = recorded_order(
        [SheetPage(_page(0, "A-101"), RevisionBlock(current=RevisionLabel("Q")))]
    )
    assert "Q" not in order


def test_two_histories_disagreeing_at_equal_length_leave_the_label_unplaced() -> None:
    """An inconsistency between two tables must reach a human, not be averaged.

    Same length, contradictory positions: the label is dropped from the order, which makes the sheet
    unresolved rather than resolved by a coin toss.
    """
    one = _sheet(0, "A-101", "A", "B")
    two = _sheet(1, "A-101", "B", "A")

    order = recorded_order([one, two])

    assert "A" not in order and "B" not in order
    assert isinstance(governing_revision([one, two]), Unresolved)


# ---------------------------------------------------------------------------
# Unknown and duplicate revisions
# ---------------------------------------------------------------------------


def test_an_unknown_revision_makes_the_sheet_unresolved() -> None:
    """Even though the other page orders perfectly well on its own.

    The unknown page might be the later one. Resolving around it would mean choosing the sheet we *can*
    read over the sheet we cannot, which is a preference for legibility over correctness.
    """
    outcome = governing_revision([_sheet(0, "A-101", "A", "B"), _unknown(1, "A-101")])

    assert isinstance(outcome, Unresolved)
    assert outcome.cause == UNKNOWN_REVISION
    assert "how a superseded sheet gets used with confidence" in outcome.detail


def test_two_pages_claiming_the_same_revision_are_unresolved() -> None:
    """Two sheets both stamped Rev B is a package problem, not a tie to break.

    One of them is wrong and nothing here can tell which — so it is reported rather than halved.
    """
    outcome = governing_revision([_sheet(0, "A-101", "A", "B"), _sheet(1, "A-101", "A", "B")])

    assert isinstance(outcome, Unresolved)
    assert outcome.cause == DUPLICATE_REVISION
    assert "One of them is wrong" in outcome.detail


def test_a_single_page_governs_its_own_sheet() -> None:
    """The ordinary one-revision sheet must resolve, or the flag becomes noise.

    A refusal here would mark every single-revision sheet in a package as needing review, and a warning
    that fires on everything is a warning nobody reads.
    """
    only = _sheet(0, "A-101", "A")
    outcome = governing_revision([only])

    assert isinstance(outcome, GoverningRevision)
    assert outcome.governing is only
    assert outcome.superseded == ()


def test_a_single_page_with_an_unknown_revision_is_still_unresolved() -> None:
    """One page and no idea which revision it is: there is nothing to compare, and nothing known either.

    This is the boundary of the rule above. "Nothing to supersede" is a reason to resolve; "we cannot
    read what this is" is not.
    """
    outcome = governing_revision([_unknown(0, "A-101")])

    assert isinstance(outcome, Unresolved)
    assert outcome.cause == UNKNOWN_REVISION


# ---------------------------------------------------------------------------
# Dates corroborate; they never decide
# ---------------------------------------------------------------------------


def test_dates_agreeing_with_the_recorded_order_resolve_normally() -> None:
    """Corroboration doing its job quietly."""
    dates = {"A": date(2026, 1, 15), "B": date(2026, 3, 4)}
    outcome = governing_revision(
        [_sheet(0, "A-101", "A", dates=dates), _sheet(1, "A-101", "A", "B", dates=dates)]
    )

    assert isinstance(outcome, GoverningRevision)
    assert outcome.governing.label == "B"


def test_a_date_contradicting_the_recorded_order_is_unresolved() -> None:
    """The sheet is telling two stories, and this refuses rather than preferring one.

    A date never overrides the recorded order — and it is not ignored when it contradicts it either.
    Silently trusting the history here would discard the only evidence that something is wrong.
    """
    dates = {"A": date(2026, 5, 1), "B": date(2026, 1, 15)}  # B listed later, dated earlier
    outcome = governing_revision(
        [_sheet(0, "A-101", "A", dates=dates), _sheet(1, "A-101", "A", "B", dates=dates)]
    )

    assert isinstance(outcome, Unresolved)
    assert outcome.cause == DATE_CONTRADICTS_ORDER
    assert "two different stories" in outcome.detail


def test_an_ambiguous_date_cannot_corroborate_and_cannot_contradict() -> None:
    """#183 keeps both readings of `03/04/26`; neither may be used as evidence here.

    Treating an ambiguous date as agreement would let the weaker signal quietly confirm the stronger one,
    and treating it as disagreement would flag ordinary sheets. It is silent.

    **Constructed so the ambiguous date would contradict if it were used.** `A` is dated certainly later
    than either reading of `B`'s date, so an implementation reaching for `candidates[0]` instead of
    `certain_date` sees a contradiction and refuses. My first version put the ambiguous date where it
    could not form a comparison pair at all, so it passed whether the guard existed or not — found by
    deleting the guard and watching the suite stay green.
    """
    ambiguous = RevisionDate("03/04/26", (date(2026, 3, 4), date(2026, 4, 3)), century_assumed=True)
    certainly_later = RevisionDate("2026-05-01", (date(2026, 5, 1),))

    later_history = (
        RevisionHistoryRow(RevisionLabel("A", certainly_later, 0)),
        RevisionHistoryRow(RevisionLabel("B", ambiguous, 1)),
    )
    pages = [
        SheetPage(
            _page(0, "A-101"),
            RevisionBlock(
                RevisionLabel("A", certainly_later),
                (RevisionHistoryRow(RevisionLabel("A", certainly_later, 0)),),
            ),
        ),
        SheetPage(_page(1, "A-101"), RevisionBlock(later_history[-1].label, later_history)),
    ]

    outcome = governing_revision(pages)

    assert isinstance(outcome, GoverningRevision), (
        "an ambiguous date was used as evidence: A is dated later than either reading of B, so taking "
        "one of them makes the dates look like they contradict the recorded order"
    )
    assert outcome.governing.label == "B"


# ---------------------------------------------------------------------------
# Nothing is deleted
# ---------------------------------------------------------------------------


def test_a_superseded_page_is_retained_and_says_why() -> None:
    """The third and fourth criteria together.

    A reviewer asking "what did Rev A say?" is asking a legitimate question, so the page is kept — and
    the reason names the sheet whose history decided it, because "superseded" without a why is not an
    answer.
    """
    earlier = _sheet(0, "A-101", "A")
    later = _sheet(1, "A-101", "A", "B")

    outcome = governing_revision([earlier, later])
    assert isinstance(outcome, GoverningRevision)

    assert len(outcome.superseded) == 1
    lost = outcome.superseded[0]
    assert lost.page is earlier, "the page itself is retained, not merely its number"
    assert lost.superseded_by == "B"
    assert "revision history printed on sheet A-101" in lost.reason
    assert "Retained" in lost.reason


def test_every_page_is_accounted_for_exactly_once() -> None:
    """Governing plus superseded equals what went in — nothing dropped, nothing duplicated.

    A page that silently vanished here would be a drawing the system never checked and never reported.
    """
    pages = [
        _sheet(0, "A-101", "A"),
        _sheet(1, "A-101", "A", "B"),
        _sheet(2, "A-101", "A", "B", "C"),
    ]

    outcome = governing_revision(pages)
    assert isinstance(outcome, GoverningRevision)

    accounted = [outcome.governing, *(s.page for s in outcome.superseded)]
    assert sorted(p.page.index for p in accounted) == [0, 1, 2]


def test_an_unresolved_sheet_still_carries_every_candidate() -> None:
    """Refusing must not lose the drawings. #185 has to show a reviewer what it could not decide."""
    pages = [
        SheetPage(_page(0, "A-101"), RevisionBlock(current=RevisionLabel("A"))),
        SheetPage(_page(1, "A-101"), RevisionBlock(current=RevisionLabel("C"))),
    ]

    outcome = governing_revision(pages)
    assert isinstance(outcome, Unresolved)
    assert sorted(p.page.index for p in outcome.candidates) == [0, 1]


# ---------------------------------------------------------------------------
# Boundaries of this story
# ---------------------------------------------------------------------------


def test_this_module_does_not_reach_into_the_verdict_engine() -> None:
    """`extraction/` must never import `verdict/` or `rules/` — `docs/DESIGN_EXTRACTION.md` §2.

    An extractor that knows which rule is coming can be tuned to satisfy it, and the evidence gate exists
    because reading and judging are separate acts.
    """
    import ast
    from pathlib import Path

    import extraction.supersession as module

    tree = ast.parse(Path(module.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    for forbidden in ("verdict", "rules", "retrieval"):
        assert forbidden not in imported, f"extraction/supersession.py imports {forbidden}"


def test_empty_input_is_a_programming_error_not_an_unresolved_sheet() -> None:
    """ "No pages" is not a sheet that could not be resolved; it is a caller bug.

    Returning `Unresolved` would put a phantom sheet in front of a reviewer.
    """
    with pytest.raises(ValueError, match="at least one page"):
        governing_revision([])


def test_the_outcome_types_are_frozen() -> None:
    """A governing revision that could be edited after the fact would answer "which sheet?" with
    whatever was most recently convenient."""
    from dataclasses import FrozenInstanceError

    outcome = governing_revision([_sheet(0, "A-101", "A")])
    assert isinstance(outcome, GoverningRevision)
    with pytest.raises(FrozenInstanceError):
        outcome.sheet_number = "A-999"  # type: ignore[misc]
