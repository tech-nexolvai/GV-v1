"""Reading the revision block (#183, B11.1).

`docs/DESIGN_EXTRACTION.md` §9 asks for refusal tests on every resolver that can fail, and says they are
the safety-critical ones. Here the refusals are the *unknown* cases: a sheet with no revision block, a
date that cannot be read, a label that is really a column heading. Each of those has to come back as "we
could not read this" rather than as a value, because §7 is explicit that unknown revision and the first
revision are different facts — and the whole of B11.2 and B11.3 is built on this module telling the truth
about which it has.

No fixtures pretending to be real drawings. §9: *"a fixture invented today encodes today's guess as
ground truth."* The inputs here are the shapes the module claims to handle, and the tests for everything
else assert that it declines.

Source: `docs/DESIGN_EXTRACTION.md` §7 · Verification: this file
"""

from __future__ import annotations

from datetime import date

import pytest

from extraction.revision import (
    RevisionBlock,
    RevisionDate,
    RevisionLabel,
    normalise,
    read_revision_block,
    read_revision_date,
)

# ---------------------------------------------------------------------------
# Unknown is not zero, and not the first revision
# ---------------------------------------------------------------------------


def test_a_sheet_with_no_revision_block_is_unknown() -> None:
    """The acceptance criterion, and §7's central point.

    A sheet that says nothing about its revision is not revision 0, not revision A, and not "the
    original". Treating it as any of those is how a superseded sheet gets used with confidence.
    """
    block = read_revision_block(["GRANITI VICENTIA", "COUNTERTOP PLAN", "SCALE 1:20"])

    assert block.current is None
    assert block.is_unknown
    assert block.history == ()


def test_unknown_is_not_expressible_as_a_revision_value() -> None:
    """There is deliberately no `RevisionLabel` that means unknown.

    Absence is modelled by `current is None`, not by a sentinel like `as_printed=""` or `"0"` — a
    sentinel is a value, and values get compared, sorted and displayed. An empty label is refused
    outright so no caller can construct the ambiguous thing.
    """
    for empty in ("", "   ", "\t"):
        with pytest.raises(ValueError, match="as_printed"):
            RevisionLabel(empty)


def test_an_empty_block_reports_unknown_rather_than_raising() -> None:
    """No text at all is a legitimate input: a page whose title block could not be read."""
    assert read_revision_block([]).is_unknown


def test_a_bare_letter_is_not_read_as_a_revision() -> None:
    """`A` in a title block is a column heading as often as a revision.

    Reading it either way is a guess, and a wrong guess here picks a governing sheet. So the label word
    is required and this fails to unknown — the direction that is visible rather than confident.
    """
    assert read_revision_block(["A", "B", "SHEET 3 OF 7"]).is_unknown


# ---------------------------------------------------------------------------
# As printed, with normalisation kept separate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("REV A", "A"),
        ("REV. C", "C"),
        ("Revision: 01", "01"),
        ("REVISION 12", "12"),
        ("rev b", "b"),
    ],
)
def test_the_identifier_is_preserved_exactly_as_printed(line: str, expected: str) -> None:
    """The first acceptance criterion. `01` stays `01`, and `rev b` keeps its lowercase `b`.

    A reviewer's report cites the sheet, not our tidied version of it — so if the drawing prints `01`,
    quoting `1` back at the vendor is quoting something they did not write.
    """
    block = read_revision_block([line])
    assert block.current is not None
    assert block.current.as_printed == expected


@pytest.mark.parametrize(
    ("printed", "normalised"),
    [("A", "A"), ("Rev C", "C"), ("REV. c", "C"), ("01", "1"), ("1", "1"), ("0A", "0A")],
)
def test_normalisation_is_a_separate_function(printed: str, normalised: str) -> None:
    """Comparison needs a normal form; the record needs the printed one. Both, separately.

    `0A` stays `0A`: stripping a leading zero is only safe when what remains is a number, and `0A` is
    not `A`.
    """
    assert normalise(printed) == normalised


def test_normalisation_does_not_order_revisions() -> None:
    """Deciding that C is later than A is B11.2's, and this module must not smuggle it in.

    Normalising to a comparable string is not the same as ranking, and a helper here that returned
    something sortable by recency would be that decision made in the wrong file.
    """
    from extraction import revision

    exported = set(revision.__all__)
    for forbidden in ("governing_revision", "latest", "compare", "is_later", "rank"):
        assert not any(
            forbidden in name.lower() for name in exported
        ), f"{forbidden} belongs to B11.2 (#184), not to reading a block"


# ---------------------------------------------------------------------------
# Dates: ambiguity kept
# ---------------------------------------------------------------------------


def test_an_ambiguous_date_keeps_both_readings() -> None:
    """The acceptance criterion, quoting its own example: `03/04/26` is not silently assumed.

    Dates are how supersession gets decided, so a silently chosen interpretation is a silently chosen
    governing sheet. Both readings are kept and the date reports itself as uncertain.
    """
    read = read_revision_date("REV B  03/04/26  ISSUED FOR CONSTRUCTION")

    assert read is not None
    assert set(read.candidates) == {date(2026, 3, 4), date(2026, 4, 3)}
    assert not read.is_certain
    assert read.certain_date is None
    assert read.as_printed == "03/04/26"


def test_an_iso_date_is_certain() -> None:
    """The unambiguous case must be usable, or every date becomes uncertain and the flag says nothing."""
    read = read_revision_date("REV B 2026-03-04")

    assert read is not None
    assert read.candidates == (date(2026, 3, 4),)
    assert read.is_certain
    assert read.certain_date == date(2026, 3, 4)
    assert not read.century_assumed


def test_a_two_digit_year_is_never_certain_even_with_one_reading() -> None:
    """`25/12/26` has exactly one field-order reading and still had a century invented.

    This is the case a naive "one candidate means certain" check gets wrong. 25 cannot be a month, so
    only one reading survives — but the year came from an assumption, and `century_assumed` is how the
    caller finds out. Without this the date would look settled.
    """
    read = read_revision_date("REV C 25/12/26")

    assert read is not None
    assert read.candidates == (date(2026, 12, 25),)
    assert read.century_assumed
    assert not read.is_certain, "a date with an invented century must not report itself as certain"
    assert read.certain_date is None


def test_a_four_digit_year_removes_the_century_assumption() -> None:
    """Same date printed in full: still two field-order readings, but no century was guessed."""
    read = read_revision_date("REV C 04/05/2026")

    assert read is not None
    assert set(read.candidates) == {date(2026, 4, 5), date(2026, 5, 4)}
    assert not read.century_assumed
    assert not read.is_certain, "two readings is still not certain"


def test_an_impossible_date_is_kept_as_printed_with_no_reading() -> None:
    """A date that cannot be a date is still evidence about the sheet.

    Returning a `RevisionDate` with no candidates rather than `None` keeps "there is a date here we
    cannot interpret" distinct from "there is no date" — a caller deciding whether to trust a sheet needs
    both, and they call for different actions.
    """
    read = read_revision_date("REV D 45/45/26")

    assert read is not None
    assert read.candidates == ()
    assert read.as_printed == "45/45/26"
    assert not read.is_certain


def test_no_date_at_all_returns_none() -> None:
    """The other side of that distinction."""
    assert read_revision_date("REV A ISSUED FOR TENDER") is None


def test_candidates_are_ordered_so_equal_dates_compare_equal() -> None:
    """Two `RevisionDate`s read from the same text must be equal, whatever order the set iterated in.

    These are frozen dataclasses compared by value, and an unordered tuple would make equality depend on
    set iteration order — which would be a comparison that usually works.
    """
    first = read_revision_date("03/04/26")
    second = read_revision_date("03/04/26")
    assert first == second
    assert first is not None and list(first.candidates) == sorted(first.candidates)

    with pytest.raises(ValueError, match="in order"):
        RevisionDate("03/04/26", (date(2026, 4, 3), date(2026, 3, 4)))


def test_a_repeated_reading_is_refused() -> None:
    """Two identical candidates would make an unambiguous date look ambiguous."""
    with pytest.raises(ValueError, match="repeat"):
        RevisionDate("03/04/26", (date(2026, 3, 4), date(2026, 3, 4)))


# ---------------------------------------------------------------------------
# History rows, in order
# ---------------------------------------------------------------------------


def test_history_rows_are_captured_in_order() -> None:
    """The acceptance criterion. Order is the fact a revision table carries.

    Rows are appended as revisions are issued, so the order *is* the history — and `sequence_index`
    records where each sat rather than leaving a caller to trust the tuple's order downstream.
    """
    block = read_revision_block(
        [
            "REVISION HISTORY",
            "REV A  2026-01-15  ISSUED FOR REVIEW",
            "REV B  2026-02-03  CLIENT COMMENTS",
            "REV C  2026-03-04  ISSUED FOR CONSTRUCTION",
        ]
    )

    assert [row.label.as_printed for row in block.history] == ["A", "B", "C"]
    assert [row.label.sequence_index for row in block.history] == [0, 1, 2]
    assert block.history[2].description == "ISSUED FOR CONSTRUCTION"


def test_the_current_revision_is_the_last_history_row() -> None:
    """What a revision table means: the newest issue is the most recently added row."""
    block = read_revision_block(
        ["REV A  2026-01-15  FIRST", "REV B  2026-02-03  SECOND", "REV C  2026-03-04  THIRD"]
    )

    assert block.current is not None
    assert block.current.as_printed == "C"
    assert not block.is_unknown


def test_the_sequence_index_is_not_a_revision_number() -> None:
    """A table whose first row is `C` has `sequence_index=0` and `as_printed="C"`.

    Conflating them makes "the first revision" and "the earliest row listed" the same thing, which they
    are not — a sheet reissued from an earlier drawing can start its table anywhere.
    """
    block = read_revision_block(["REV C  2026-03-04  REISSUED", "REV D  2026-04-01  UPDATED"])

    assert block.history[0].label.sequence_index == 0
    assert block.history[0].label.as_printed == "C"


def test_a_negative_sequence_index_is_refused() -> None:
    """0-based, and a negative index would silently reverse an ordering somewhere downstream."""
    with pytest.raises(ValueError, match="0-based"):
        RevisionLabel("A", sequence_index=-1)


def test_a_standalone_label_becomes_the_current_revision_with_no_history() -> None:
    """Not every sheet carries a table. One is not a table of one."""
    block = read_revision_block(["GRANITI VICENTIA", "REV C", "SHEET 2 OF 5"])

    assert block.current is not None
    assert block.current.as_printed == "C"
    assert block.current.sequence_index is None, "there is no table, so there is no position in one"
    assert block.history == ()


def test_a_description_mentioning_revision_is_not_read_as_a_label() -> None:
    """The word appears in descriptions — "revised per client comment" — and must not become a label.

    The label is a revision table's first column, so the search is anchored near the start of the line.
    Without that bound, the description's own wording would be read as the row's identifier.
    """
    block = read_revision_block(["REV A  2026-01-15  DRAWING REVISION PER CLIENT REV B COMMENTS"])

    assert [row.label.as_printed for row in block.history] == ["A"]


def test_the_history_row_keeps_its_date_with_ambiguity_intact() -> None:
    """The two criteria meet here: rows in order, each date still honest about what it could mean."""
    block = read_revision_block(["REV B  03/04/26  ISSUED"])

    row = block.history[0]
    assert row.label.date is not None
    assert not row.label.date.is_certain
    assert len(row.label.date.candidates) == 2


# ---------------------------------------------------------------------------
# This module reads; it does not open files or decide
# ---------------------------------------------------------------------------


def test_reading_takes_text_and_touches_nothing_else() -> None:
    """No rendering, no file access, no database.

    Which is what makes B11.1 implementable while B2 is still blocked on real PDFs — and what keeps the
    reader testable at all. A reader that needed a drawing to run could not be tested until drawings
    arrive.
    """
    import ast
    from pathlib import Path

    from extraction import revision

    tree = ast.parse(Path(revision.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported <= {
        "re",
        "dataclasses",
        "datetime",
        "typing",
        "__future__",
    }, f"reading a revision block should need nothing but the standard library: {sorted(imported)}"


def test_the_reader_refuses_input_that_is_not_lines_of_text() -> None:
    """A string is iterable, so passing one would silently read it character by character."""
    with pytest.raises(TypeError, match="list of strings"):
        read_revision_block("REV A")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="list of strings"):
        read_revision_block([b"REV A"])  # type: ignore[list-item]


def test_the_block_is_frozen() -> None:
    """Read once, quoted later. A block that could be edited after the fact would answer "which
    revision?" with whatever was most recently convenient."""
    from dataclasses import FrozenInstanceError

    block = RevisionBlock(current=RevisionLabel("A"))
    # `FrozenInstanceError` specifically, not a blind `Exception`: a bare `pytest.raises(Exception)`
    # would also pass on a typo in the attribute name, which proves nothing about frozenness.
    with pytest.raises(FrozenInstanceError):
        block.current = RevisionLabel("B")  # type: ignore[misc]
