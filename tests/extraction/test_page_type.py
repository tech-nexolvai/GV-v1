"""Classifying a page, and refusing to (#161, B6.2).

The refusals carry the weight here. `docs/DESIGN_EXTRACTION.md` §3.2: *"a countertop width found on a
cabinet elevation is a plausible number attached to the wrong drawing, and no tolerance check catches
it."* Nothing downstream recovers from a page classified wrongly, so the tests that matter are the ones
proving it declines rather than guesses.

Two properties get their own sections because they are easy to lose later: confidence never influences an
answer, and the same input always gives the same answer.

Source: `docs/DESIGN_EXTRACTION.md` §3.2 · Verification: this file
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from extraction.manifest import PageRecord
from extraction.page_type import (
    TYPE_WORDS,
    Classification,
    PageText,
    Signal,
    classify,
    is_same_view_eligible,
)
from vocabulary.page_types import PageType


def _page(index: int = 0) -> PageRecord:
    return PageRecord(
        index=index,
        content_hash=hashlib.sha256(f"page-{index}".encode()).hexdigest(),
        width_pt=Decimal(842),
        height_pt=Decimal(595),
        rotation=0,
        has_vector_text=True,
        render_failed=False,
    )


# ---------------------------------------------------------------------------
# Unknown is a real answer
# ---------------------------------------------------------------------------


def test_a_page_naming_no_type_is_unclassified() -> None:
    """The first acceptance criterion. Not rounded to the likeliest member."""
    result = classify(_page(), PageText(title_block=("GRANITI VICENTIA", "JOB 4471", "SCALE 1:20")))

    assert result.page_type is None
    assert not result.is_classified
    assert result.signal is None, "there is no evidence for a conclusion that was not reached"
    assert "still extracted" in result.reason


def test_there_is_no_unknown_member_to_be_assigned() -> None:
    """Absence is `None`, never a value.

    An `UNKNOWN` member would be compared, filtered and displayed like any real type — and a page listed
    as type "unknown" in a report looks classified.
    """
    assert "UNKNOWN" not in {member.name for member in PageType}
    assert not any(member.value == "unknown" for member in PageType)


def test_an_empty_page_is_unclassified_rather_than_an_error() -> None:
    """A page whose text could not be read is a normal input, not a bug."""
    assert classify(_page(), PageText()).page_type is None


def test_two_type_words_in_the_title_block_is_unknown() -> None:
    """**The refusal most likely to be argued with.**

    `PLAN AND ELEVATION` is a real sheet, and this declines it. Assigning either half would hand a check
    scoped to that type a sheet that is only partly it — and the check would compute a confident number
    from the wrong half.
    """
    result = classify(_page(), PageText(title_block=("FLOOR PLAN AND ELEVATION",)))

    assert result.page_type is None
    assert "more than one page type" in result.reason
    assert "elevation" in result.reason and "plan" in result.reason


def test_a_word_that_merely_contains_a_type_name_does_not_classify() -> None:
    """Word boundaries, not substrings.

    `PLANNING` is not `PLAN` and `SECTIONAL` is not `SECTION`. A substring search would classify a sheet
    by a word that happens to contain a type name, which is a wrong answer arrived at confidently.
    """
    for misleading in ("PLANNING APPLICATION", "SECTIONAL OVERHEAD DOOR", "DETAILED SCOPE"):
        result = classify(_page(), PageText(title_block=(misleading,)))
        assert result.page_type is None, f"{misleading!r} was classified"


# ---------------------------------------------------------------------------
# The title block decides; tags speak only when it is silent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("FLOOR PLAN", PageType.PLAN),
        ("KITCHEN ELEVATION", PageType.ELEVATION),
        ("SECTION THROUGH ISLAND", PageType.SECTION),
        ("DETAIL — SINK CUTOUT", PageType.DETAIL),
        ("COUNTERTOP SCHEDULE", PageType.SCHEDULE),
        ("TITLE SHEET", PageType.TITLE),
        ("COVER SHEET", PageType.TITLE),
    ],
)
def test_the_title_block_classifies_the_page(line: str, expected: PageType) -> None:
    """Every word in the vocabulary, so a missing mapping fails rather than silently never matching."""
    result = classify(_page(), PageText(title_block=(line,)))

    assert result.page_type is expected
    assert result.signal is Signal.TITLE_TEXT
    assert result.evidence == line, "the deciding text is recorded, not just the conclusion"


def test_every_type_word_maps_to_a_real_page_type() -> None:
    """Guard for the table: a typo in a key would make that word simply never match."""
    for word, page_type in TYPE_WORDS.items():
        assert isinstance(page_type, PageType)
        assert classify(_page(), PageText(title_block=(word,))).page_type is page_type


def test_a_section_callout_on_a_plan_sheet_is_still_a_plan() -> None:
    """**The precedence rule, tested on the ordinary case rather than a contrived one.**

    Plan sheets carry `SECTION A-A` markers all the time. A title block describes *this* sheet; a view
    tag points at another. Treating the two as contradictory would make almost every real page unknown —
    and a classifier that answers "unknown" for everything is one somebody switches off.
    """
    result = classify(
        _page(), PageText(title_block=("FLOOR PLAN",), view_tags=("SECTION A-A", "DETAIL 3"))
    )

    assert result.page_type is PageType.PLAN
    assert result.signal is Signal.TITLE_TEXT


def test_a_view_tag_classifies_only_when_the_title_block_is_silent() -> None:
    result = classify(_page(), PageText(title_block=("JOB 4471",), view_tags=("SECTION B-B",)))

    assert result.page_type is PageType.SECTION
    assert result.signal is Signal.VIEW_TAG
    assert "title block names no page type" in result.reason


def test_disagreeing_view_tags_with_a_silent_title_block_are_unknown() -> None:
    """Several tags naming different types say nothing about the sheet they are printed on."""
    result = classify(
        _page(), PageText(title_block=("JOB 4471",), view_tags=("SECTION A-A", "ELEVATION 2"))
    )

    assert result.page_type is None
    assert "view tags disagree" in result.reason


def test_the_geometry_signal_is_reserved_and_unused() -> None:
    """The narrowing of scope, asserted so it cannot be forgotten or quietly filled in.

    A geometry threshold has to come from real drawings (#274). §9: *"a fixture invented today encodes
    today's guess as ground truth."* The member exists so adding the signal does not change this shape;
    nothing produces it, and that is the honest state.
    """
    assert Signal.GEOMETRY in set(Signal)

    every_answer = [
        classify(_page(), PageText(title_block=("FLOOR PLAN",))),
        classify(_page(), PageText(view_tags=("SECTION A-A",))),
        classify(_page(), PageText()),
        classify(_page(), PageText(title_block=("PLAN AND ELEVATION",))),
    ]
    assert all(answer.signal is not Signal.GEOMETRY for answer in every_answer)


# ---------------------------------------------------------------------------
# Confidence is diagnostic, never authority
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("confidence", [None, Decimal("0.01"), Decimal("0.5"), Decimal("1.0")])
def test_confidence_never_changes_the_answer(confidence: Decimal | None) -> None:
    """The fourth acceptance criterion — backend §6.3's model trust policy.

    A model may say how sure it is; that number has no authority. Asserted across the whole range,
    because the failure mode is a threshold appearing later that reads "if confidence > x".
    """
    for text_kwargs in (
        {"title_block": ("FLOOR PLAN",)},
        {"title_block": ("PLAN AND ELEVATION",)},
        {"view_tags": ("SECTION A-A",)},
        {},
    ):
        without = classify(_page(), PageText(**text_kwargs))  # type: ignore[arg-type]
        with_confidence = classify(
            _page(), PageText(**text_kwargs, confidence=confidence)  # type: ignore[arg-type]
        )
        assert with_confidence.page_type == without.page_type
        assert with_confidence.signal == without.signal


def test_confidence_is_carried_through_for_diagnosis() -> None:
    """Never consulted is not the same as discarded — it is recorded so a bad reader can be found."""
    result = classify(_page(), PageText(title_block=("FLOOR PLAN",), confidence=Decimal("0.42")))
    assert result.confidence == Decimal("0.42")


def test_a_low_confidence_classification_is_still_classified() -> None:
    """There is no threshold below which a read becomes unknown, because that would be authority."""
    result = classify(_page(), PageText(title_block=("FLOOR PLAN",), confidence=Decimal("0.001")))
    assert result.page_type is PageType.PLAN


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_the_same_page_always_yields_the_same_type() -> None:
    """The third acceptance criterion, over every branch rather than one input."""
    inputs = [
        PageText(title_block=("FLOOR PLAN",)),
        PageText(title_block=("PLAN AND ELEVATION",)),
        PageText(title_block=("JOB 4471",), view_tags=("SECTION A-A",)),
        PageText(),
    ]
    for text in inputs:
        answers = {classify(_page(), text) for _ in range(20)}
        assert len(answers) == 1, f"{text} produced more than one answer"


def test_classification_does_not_depend_on_the_page_index() -> None:
    """Position in the package is not evidence about what a sheet is."""
    text = PageText(title_block=("FLOOR PLAN",))
    assert classify(_page(0), text).page_type == classify(_page(17), text).page_type


def test_nothing_random_or_time_based_is_reachable() -> None:
    """Determinism, asserted against the imports rather than by running it twice.

    Twenty identical answers do not prove there is no clock in the code — they prove it did not tick.
    """
    import ast
    from pathlib import Path

    import extraction.page_type as module

    tree = ast.parse(Path(module.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    for forbidden in ("random", "time", "datetime", "secrets", "uuid"):
        assert forbidden not in imported, f"page_type imports {forbidden}"


# ---------------------------------------------------------------------------
# What unknown actually costs
# ---------------------------------------------------------------------------


def test_an_unknown_page_is_excluded_only_from_same_view() -> None:
    """The fifth acceptance criterion. An unclassified page is still extracted.

    Dropping it from extraction would lose evidence over a classification failure — the page is still a
    drawing, and a reviewer may well need what is on it.
    """
    unknown = classify(_page(), PageText(title_block=("JOB 4471",)))
    known = classify(_page(), PageText(title_block=("FLOOR PLAN",)))

    assert not is_same_view_eligible(unknown)
    assert is_same_view_eligible(known)
    assert "only excluded from same_view" in unknown.reason


def test_every_answer_explains_itself() -> None:
    """Whether or not it classified. A refusal with no reason is a reviewer with no next step."""
    for text in (
        PageText(title_block=("FLOOR PLAN",)),
        PageText(title_block=("PLAN AND ELEVATION",)),
        PageText(title_block=("JOB 4471",), view_tags=("SECTION A-A", "ELEVATION 2")),
        PageText(),
    ):
        result = classify(_page(), text)
        assert result.reason and len(result.reason) > 20, f"thin reason for {text}"


def test_the_classification_is_frozen() -> None:
    """Read once, reported later. An editable classification answers "what is this page?" with
    whatever was most recently convenient."""
    from dataclasses import FrozenInstanceError

    result = classify(_page(), PageText(title_block=("FLOOR PLAN",)))
    with pytest.raises(FrozenInstanceError):
        result.page_type = PageType.SECTION  # type: ignore[misc]


def test_a_bare_string_is_refused_rather_than_read_character_by_character() -> None:
    """A string is iterable, so passing one would treat each character as a line."""
    with pytest.raises(TypeError, match="not a single string"):
        PageText(title_block="FLOOR PLAN")  # type: ignore[arg-type]


def test_a_classification_can_be_built_directly_for_a_known_page() -> None:
    """The type is constructible by a caller that already knows — used by fixtures and by #162."""
    made = Classification(PageType.PLAN, Signal.TITLE_TEXT, "FLOOR PLAN", "known")
    assert made.is_classified and made.confidence is None
