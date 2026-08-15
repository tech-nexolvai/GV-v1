"""`docs/CLIENT_FACTS.md` is the authority, so it has to stay parseable and complete.

Before it existed, client facts lived in four documents that disagreed, and reconciling them by hand
is what produced two real errors: `CT011`–`CT013` recorded as undefined when a diagram defines them,
and `±1/8″` treated as a client tolerance when it is our own placeholder.

A document that silently stops parsing would put us straight back there — every lookup would report
"unknown question" and the gate would fall through to its old behaviour, which is the behaviour that
cost the time.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from client_facts import (
    Blocks,
    ClientFactsError,
    Status,
    load,
    lookup,
)

FACTS = load()

#: Every question tracked as an issue. Pinned literally rather than derived from the file, so
#: deleting an entry fails here instead of quietly shrinking what we think we know.
EXPECTED = tuple(f"Q{n}" for n in range(1, 21))


def test_every_question_is_present() -> None:
    assert tuple(sorted(FACTS, key=lambda r: int(r[1:]))) == EXPECTED


def test_every_fact_cites_a_specific_source() -> None:
    """ "It's in the checklist" is not a source. A fact without one is an assertion, and assertions
    are what we were reconciling."""
    for fact in FACTS.values():
        assert len(fact.source) > 30, f"{fact.ref} has no real source"


@pytest.mark.parametrize("ref", EXPECTED)
def test_status_is_binary(ref: str) -> None:
    """Only ANSWERED or OPEN. "Implied" and "not stated" are exactly the hedges that made four
    documents disagree with each other."""
    assert FACTS[ref].status in (Status.ANSWERED, Status.OPEN)


@pytest.mark.parametrize("ref", EXPECTED)
def test_every_fact_declares_what_it_blocks(ref: str) -> None:
    assert FACTS[ref].blocks in (Blocks.FORMULA, Blocks.VALUE, Blocks.NOTHING)


def test_an_answered_question_actually_carries_an_answer() -> None:
    """An ANSWERED entry with a dash would be worse than an OPEN one: it would unblock work on the
    strength of nothing."""
    for fact in FACTS.values():
        if fact.status is Status.ANSWERED:
            assert fact.answer.strip() not in (
                "",
                "—",
            ), f"{fact.ref} claims ANSWERED with no answer"


# ---------------------------------------------------------------------------
# The distinction the whole file exists for
# ---------------------------------------------------------------------------


def test_an_open_formula_question_stops_work() -> None:
    """Q5 — sink offset minimum versus exact — changes the operation, not a number. A rule authored
    before it lands computes the wrong thing at exactly 4 inches and passes confidently."""
    assert FACTS["Q5"].stops_work


def test_an_open_value_question_does_not_stop_work() -> None:
    """Q2 — tolerance — supplies a number. The rule is authorable with UNCONFIRMED, which returns
    REVIEW REQUIRED for everything and cannot reach production (ADR-0011).

    Conflating these two is what cost three round trips of argument before this file existed.
    """
    assert not FACTS["Q2"].stops_work
    assert FACTS["Q2"].blocks is Blocks.VALUE


def test_an_answered_question_stops_nothing_whatever_it_blocks() -> None:
    """Q1 is `blocks: formula` and ANSWERED. Once the answer exists the classification is history."""
    assert FACTS["Q1"].status is Status.ANSWERED
    assert FACTS["Q1"].blocks is Blocks.FORMULA
    assert not FACTS["Q1"].stops_work


def test_the_gate_line_says_which_kind_of_blocked() -> None:
    assert "changes the formula" in FACTS["Q5"].gate_line()
    assert "PROVISIONAL" in FACTS["Q2"].gate_line()
    assert "ANSWERED" in FACTS["Q1"].gate_line()


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["Q5", "Q5 (#13)", "- Q5 (#13)", "requires Q5"])
def test_lookup_tolerates_how_a_dependency_is_actually_written(text: str) -> None:
    """`requires:` entries are hand-written and inconsistent. Matching only a bare `Q5` would make
    the gate silently ignore most of them — failing open, which is the wrong direction."""
    fact = lookup(text, FACTS)
    assert fact is not None and fact.ref == "Q5"


def test_lookup_returns_none_for_something_that_is_not_a_question() -> None:
    assert lookup("#215", FACTS) is None


def test_an_unparseable_file_raises_rather_than_reporting_no_facts() -> None:
    """Returning an empty mapping would make every question look unknown, and the gate would fall
    back to the behaviour this file replaced — silently."""
    with pytest.raises(ClientFactsError):
        load(Path(__file__))  # this file has no CLIENT FACTS markers


# ---------------------------------------------------------------------------
# The two facts that correct work already shipped
# ---------------------------------------------------------------------------


def test_q20_blocks_the_formula_not_merely_the_names() -> None:
    """ADR-0017 treated Q20 as name confirmation. It is stricter than that: the A–G to CT0xx
    mapping is inferred by us and never stated, so a rule referencing Sheet1's letters cannot be
    resolved. The vocabulary adoption stands; the letter mapping must not be relied on."""
    assert FACTS["Q20"].blocks is Blocks.FORMULA
    assert FACTS["Q20"].stops_work


def test_q1_records_that_field_cuts_are_added_not_trimmed() -> None:
    """The answer changes CT-1's arithmetic, and the source warns against folding in the 5" cabinet
    filler field-cut, which is a different element."""
    answer = FACTS["Q1"].answer
    assert "ADDED" in answer
    assert '5"' in FACTS["Q1"].source or "5" in FACTS["Q1"].source
