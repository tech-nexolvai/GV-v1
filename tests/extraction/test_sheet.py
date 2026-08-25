"""Sheet identity and the `same_view` resolver (#162, B6.3).

The property that matters is negative: **this never returns more than it can justify.**
`docs/DESIGN_EXTRACTION.md` §3.3 — *"silently widening scope is how a rule finds a number that satisfies
it somewhere."* A rule looking for one cabinet width will find *a* cabinet width if you let it search far
enough, and then compute a confident, fully traced, completely wrong answer.

So every refusal has its own test asserting the scope is empty, rather than one test covering them
collectively — a widening introduced in one branch would otherwise hide behind the others.

Source: `docs/DESIGN_EXTRACTION.md` §3.3 · Verification: this file
"""

from __future__ import annotations

import pytest

from extraction.page_type import Classification, Signal
from extraction.sheet import (
    SameViewScope,
    ScopeBasis,
    SheetIdentity,
    ViewCandidate,
    normalise_sheet_number,
    read_sheet_identity,
    resolve_same_view,
)
from vocabulary.page_types import PageType

_PLAN = Classification(
    PageType.PLAN, Signal.TITLE_TEXT, "FLOOR PLAN", "the title block says 'PLAN'"
)
_UNKNOWN = Classification(None, None, None, "nothing on this page names a page type")


def _candidate(index: int, number: str | None, *, classified: bool = True) -> ViewCandidate:
    return ViewCandidate(
        SheetIdentity(page_index=index, number_as_printed=number),
        _PLAN if classified else _UNKNOWN,
    )


# ---------------------------------------------------------------------------
# Sheet number: as printed, normalisation separate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("A-101", "A-101"),
        ("SHEET A-101", "A-101"),
        ("SHEET NO. M-2.1", "M-2.1"),
        ("DWG: AD 07", "AD 07"),
        ("A101", "A101"),
    ],
)
def test_the_sheet_number_is_read_as_printed(line: str, expected: str) -> None:
    """The first acceptance criterion. `A 101` keeps its space; `M-2.1` keeps its dot.

    A report cites the sheet, so quoting a tidied form back at a vendor quotes something they did not
    draw — the same rule `extraction/revision.py` follows for a revision label.
    """
    assert read_sheet_identity(0, [line]).number_as_printed == expected


@pytest.mark.parametrize(
    ("printed", "normalised"),
    [("A-101", "A101"), ("A101", "A101"), ("a 101", "A101"), ("M-2.1", "M2.1"), ("AD 07", "AD07")],
)
def test_normalisation_is_separate_and_keeps_the_decimal(printed: str, normalised: str) -> None:
    """`M-2.1` and `M-2.10` are different sheets, so the dot survives.

    Stripping it would merge two sheets into one and put a rule's operands on the wrong drawing.
    """
    assert normalise_sheet_number(printed) == normalised
    assert normalise_sheet_number("M-2.10") != normalise_sheet_number("M-2.1")


def test_the_printed_form_is_never_replaced_by_the_normalised_one() -> None:
    identity = read_sheet_identity(0, ["SHEET A-101"])
    assert identity.number_as_printed == "A-101"
    assert identity.normalised_number == "A101"


def test_sheet_n_of_m_is_not_read_as_a_sheet_number() -> None:
    """**The refusal that would do the most damage if it were missing.**

    `SHEET 3 OF 7` is a position in the package. Reading `3` out of it would give unrelated pages the
    same sheet number — and a scope resolver handed that groups two different drawings into one view,
    which is precisely the widening this module exists to prevent.
    """
    identity = read_sheet_identity(0, ["SHEET 3 OF 7", "GRANITI VICENTIA"])
    assert identity.number_as_printed is None


def test_sheet_n_of_m_does_not_hide_a_real_number_on_the_same_line() -> None:
    """The phrase is removed, then the line is still searched — a real number beside it still reads."""
    identity = read_sheet_identity(0, ["A-101    SHEET 3 OF 7"])
    assert identity.number_as_printed == "A-101"


def test_a_page_with_no_sheet_number_says_so() -> None:
    identity = read_sheet_identity(4, ["GRANITI VICENTIA", "JOB 4471"])
    assert identity.number_as_printed is None
    assert identity.normalised_number is None
    assert identity.page_index == 4


def test_the_sheet_title_is_read_when_it_is_labelled() -> None:
    identity = read_sheet_identity(0, ["SHEET A-101", "TITLE: KITCHEN COUNTERTOP PLAN"])
    assert identity.title == "KITCHEN COUNTERTOP PLAN"


def test_a_negative_page_index_is_refused() -> None:
    """0-based, and a negative index would quietly reverse an ordering downstream."""
    with pytest.raises(ValueError, match="0-based"):
        SheetIdentity(page_index=-1)


def test_a_bare_string_is_refused_rather_than_read_per_character() -> None:
    with pytest.raises(TypeError, match="list of strings"):
        read_sheet_identity(0, "SHEET A-101")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The resolver groups by sheet number
# ---------------------------------------------------------------------------


def test_pages_sharing_a_sheet_number_are_the_same_view() -> None:
    subject = _candidate(0, "A-101")
    candidates = [subject, _candidate(1, "A-101"), _candidate(2, "A-201")]

    scope = resolve_same_view(subject, candidates)

    assert scope.members == (0, 1)
    assert scope.basis is ScopeBasis.SHEET_NUMBER
    assert scope.is_resolved


def test_grouping_uses_the_normalised_number() -> None:
    """`A-101` and `A101` are one sheet however the vendor printed them on each page."""
    subject = _candidate(0, "A-101")
    scope = resolve_same_view(subject, [subject, _candidate(1, "A101"), _candidate(2, "a 101")])

    assert scope.members == (0, 1, 2)


def test_the_scope_never_includes_a_different_sheet() -> None:
    """The bound. A rule may look across the sheet and no further."""
    subject = _candidate(0, "A-101")
    scope = resolve_same_view(subject, [subject, _candidate(1, "A-201"), _candidate(2, "M-2.1")])

    assert scope.members == (0,)


# ---------------------------------------------------------------------------
# No sheet number: the page alone, and it says so
# ---------------------------------------------------------------------------


def test_a_page_with_no_sheet_number_resolves_to_itself_and_says_so() -> None:
    """The third acceptance criterion.

    One page is trivially the same view as itself, so this is a real answer rather than a fallback. The
    basis is recorded because "we grouped by sheet number" and "we could only see this page" are
    different confidence claims.
    """
    subject = _candidate(3, None)
    scope = resolve_same_view(subject, [subject, _candidate(1, "A-101"), _candidate(2, "A-101")])

    assert scope.members == (3,)
    assert scope.basis is ScopeBasis.PAGE_INDEX
    assert "that page alone" in scope.reason
    assert scope.is_resolved, "the page itself is a resolvable scope"


def test_a_numberless_page_does_not_absorb_other_numberless_pages() -> None:
    """Two pages that both lack a number are not thereby the same sheet.

    Grouping them would be inventing a sheet out of a shared absence — and `None == None` is exactly the
    comparison that makes that mistake look correct.
    """
    subject = _candidate(3, None)
    scope = resolve_same_view(subject, [subject, _candidate(4, None), _candidate(5, None)])

    assert scope.members == (3,)


# ---------------------------------------------------------------------------
# It never widens — each refusal asserted on its own
# ---------------------------------------------------------------------------


def test_an_unclassified_subject_resolves_to_nothing() -> None:
    """§3.2's one consequence of an unknown page type, and this module is where it applies.

    A page we cannot say is a plan cannot be asserted to be the same *view* as anything. It is still
    extracted — the reason says so, because a reviewer seeing an empty scope will otherwise assume the
    page was dropped.
    """
    subject = _candidate(0, "A-101", classified=False)
    scope = resolve_same_view(subject, [subject, _candidate(1, "A-101")])

    assert scope.members == ()
    assert scope.basis is None
    assert not scope.is_resolved
    assert "still extracted" in scope.reason


def test_an_unclassified_candidate_is_not_a_member() -> None:
    """The same rule from the other side: it excludes members, not only subjects."""
    subject = _candidate(0, "A-101")
    scope = resolve_same_view(
        subject, [subject, _candidate(1, "A-101", classified=False), _candidate(2, "A-101")]
    )

    assert scope.members == (0, 2)


def test_a_subject_absent_from_the_candidates_resolves_to_nothing() -> None:
    """Returning the others would answer a question nobody asked."""
    subject = _candidate(9, "A-101")
    scope = resolve_same_view(subject, [_candidate(0, "A-101"), _candidate(1, "A-101")])

    assert scope.members == ()
    assert scope.basis is None
    assert "not among" in scope.reason


def test_no_branch_ever_returns_the_whole_package() -> None:
    """**The property this module exists for, checked across every branch at once.**

    §3.3: *"It never widens to 'the whole package'."* Each case above asserts its own result; this asserts
    the shared invariant — no input produces a scope containing a page that is neither the subject nor a
    sheet-number match. A widening added to one branch would pass its neighbours' tests and fail here.
    """
    package = [
        _candidate(0, "A-101"),
        _candidate(1, "A-201"),
        _candidate(2, None),
        _candidate(3, "A-101", classified=False),
        _candidate(4, "M-2.1"),
    ]

    for subject in [*package, _candidate(99, "A-999"), _candidate(98, None, classified=False)]:
        scope = resolve_same_view(subject, package)
        assert len(scope.members) < len(package), (
            f"subject page {subject.identity.page_index} resolved to {len(scope.members)} of "
            f"{len(package)} pages — that is a widening"
        )
        for member in scope.members:
            in_package = next(c for c in package if c.identity.page_index == member)
            same_number = (
                in_package.identity.normalised_number == subject.identity.normalised_number
                and subject.identity.normalised_number is not None
            )
            assert member == subject.identity.page_index or same_number, (
                f"page {member} is in scope for subject {subject.identity.page_index} without sharing "
                "its sheet number"
            )


def test_an_empty_candidate_list_resolves_to_nothing() -> None:
    """Not to the subject: a subject absent from an empty list is still absent."""
    scope = resolve_same_view(_candidate(0, "A-101"), [])
    assert scope.members == ()


def test_every_answer_explains_itself() -> None:
    """An empty scope with no reason leaves a reviewer unable to tell "excluded" from "nothing found"."""
    package = [
        _candidate(0, "A-101"),
        _candidate(1, None),
        _candidate(2, "A-101", classified=False),
    ]
    for subject in [*package, _candidate(9, "A-999")]:
        scope = resolve_same_view(subject, package)
        assert scope.reason and len(scope.reason) > 25, f"thin reason: {scope.reason!r}"


def test_the_scope_is_frozen() -> None:
    from dataclasses import FrozenInstanceError

    scope = SameViewScope((0,), ScopeBasis.PAGE_INDEX, "because")
    with pytest.raises(FrozenInstanceError):
        scope.members = (0, 1, 2)  # type: ignore[misc]


def test_reading_a_sheet_touches_nothing_but_the_standard_library() -> None:
    """No rendering and no database, which is what keeps B6.3 implementable while B2 waits on drawings."""
    import ast
    from pathlib import Path

    import extraction.sheet as module

    tree = ast.parse(Path(module.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    for forbidden in ("verdict", "rules", "retrieval", "sqlalchemy", "app"):
        assert forbidden not in imported, f"extraction/sheet.py imports {forbidden}"
