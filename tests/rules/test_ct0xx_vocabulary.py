"""The client's codes are the canonical vocabulary (ADR-0017, #44).

The point of adopting them is that each is defined **positionally on a diagram** rather than in
prose. "Countertop width" is unambiguous until somebody asks whether it includes the end panel —
which is `Q3` (#11), still open, precisely because the phrase does not settle it. A rule authored
against the wrong reading computes the wrong quantity correctly, produces a confident PASS, and
nothing downstream catches it because the arithmetic is sound.

So these tests care about two things: that every code the client defined is present, and that no
code he did **not** define has been invented.
"""

from __future__ import annotations

import pytest

from rules.semantic_types import (
    CLIENT_CODES,
    VOCABULARY_STATUS,
    ClientCode,
    SemanticType,
    UnknownVocabularyError,
    resolve_semantic,
)

#: Every code on `CT_image10`. Pinned as a literal rather than derived from `CLIENT_CODES`, so
#: deleting an entry there fails here instead of quietly shrinking the vocabulary.
DIAGRAM_CODES: tuple[str, ...] = tuple(f"CT{n:03d}" for n in range(1, 14))


# ---------------------------------------------------------------------------
# Completeness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", DIAGRAM_CODES)
def test_every_code_on_the_diagram_is_represented(code: str) -> None:
    assert code in CLIENT_CODES
    assert SemanticType(code)


@pytest.mark.parametrize("code", ["CT011", "CT012", "CT013"])
def test_the_three_codes_defined_only_by_the_diagram_are_present(code: str) -> None:
    """These appear in formula `F38` but never as rows in the variable table.

    ADR-0017 originally excluded them on exactly that basis and was wrong — the diagram defines
    them as clearly as the rest. Reading a text summary instead of the image is the recurring
    failure with this client's material, and this test is the standing correction.
    """
    described = CLIENT_CODES[code]
    assert described.anchor == "CT_image10"
    # Not a length threshold — "sink hole width" is three words and entirely sufficient. What we
    # are excluding is a description that restates the code and says nothing.
    assert described.description.lower() != code.lower()
    assert (
        len(described.description.split()) >= 2
    ), f"{code} restates its code instead of defining it"


def test_the_named_variables_are_represented_too() -> None:
    """`B.S_THK`, `C.T_OH` and `CAB_SIDE_THK` sit alongside the codes in his formulas — CT010 is
    built from several of them, so a vocabulary without them cannot express his own arithmetic."""
    for name in ("B.S_THK", "C.T_OH", "CAB_SIDE_THK"):
        assert name in CLIENT_CODES


def test_every_code_carries_a_description_and_an_anchor() -> None:
    """The anchor matters as much as the description: it is what a reviewer opens to check a rule
    against the thing it is supposed to measure."""
    for code in CLIENT_CODES.values():
        assert isinstance(code, ClientCode)
        assert code.description.strip()
        assert code.anchor.strip()


# ---------------------------------------------------------------------------
# Nothing invented
# ---------------------------------------------------------------------------


def test_no_code_beyond_the_client_s_own_has_been_invented() -> None:
    """A fabricated `CT014` would be indistinguishable from his vocabulary at a glance, and that is
    how a guess acquires authority — the same shape as the ±1/8" placeholder that reached
    RULE_ENGINE_SPEC §4 and started reading as fact."""
    ct_codes = {c for c in CLIENT_CODES if c.startswith("CT")}
    assert ct_codes == set(DIAGRAM_CODES)


def test_an_unknown_name_raises_rather_than_resolving_to_the_nearest() -> None:
    with pytest.raises(UnknownVocabularyError, match="must not be invented"):
        resolve_semantic("CT014")


# ---------------------------------------------------------------------------
# Lookup by either name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "descriptive"),
    [
        ("CT001", "wall_to_wall_dimension"),
        ("CT007", "sink_offset_front"),
        ("CT008", "sink_cutout_depth"),
        ("CT009", "sink_offset_back"),
        ("CT010", "countertop_depth"),
        ("CT012", "sink_cutout_width"),
    ],
)
def test_lookup_works_by_either_name(code: str, descriptive: str) -> None:
    assert resolve_semantic(code) is resolve_semantic(descriptive)


def test_a_descriptive_alias_is_the_same_object_as_its_code() -> None:
    """A true Python enum alias, not a parallel member — otherwise two names for one thing would
    compare unequal and a rule written with one would not match evidence tagged with the other."""
    assert SemanticType.SINK_CUTOUT_WIDTH is SemanticType.CT012
    assert SemanticType.WALL_TO_WALL_DIMENSION is SemanticType.CT001
    assert SemanticType.COUNTERTOP_DEPTH.value == "CT010"


def test_cabinet_width_is_deliberately_not_aliased_to_a_code() -> None:
    """The client has three *positional* cabinet codes — CT003 left, CT004 sink, CT005 right — for
    a three-cabinet layout. Our type is generic and carries the position in an index. Aliasing one
    to the other would silently claim every cabinet is the left one."""
    assert SemanticType.CABINET_WIDTH is not SemanticType.CT003
    assert SemanticType.CABINET_WIDTH.value == "cabinet_width"


def test_filler_width_is_likewise_not_aliased() -> None:
    """CT002 is the left filler and CT006 the right. A generic `filler_width` cannot be either."""
    assert SemanticType.FILLER_WIDTH is not SemanticType.CT002
    assert SemanticType.FILLER_WIDTH is not SemanticType.CT006


# ---------------------------------------------------------------------------
# Readability and provisional status
# ---------------------------------------------------------------------------


def test_every_code_has_a_plain_english_label() -> None:
    """A rule file reading `CT007 - CT008` is unreadable without the diagram, and the people
    debugging a failed check are usually us."""
    assert SemanticType.CT012.label() == "sink hole width"
    assert "clearance" in SemanticType.CT011.label()
    assert SemanticType.CT009.label() == "sink back offset"


def test_the_vocabulary_is_marked_provisional() -> None:
    """Acceptance criterion from #44. Q20 confirms he has not renamed the codes; until then the
    structure ships marked, not asserted as final."""
    assert "PROVISIONAL" in VOCABULARY_STATUS
    assert "Q20" in VOCABULARY_STATUS


def test_the_sink_geometry_reads_consistently_with_his_own_formula() -> None:
    """His table gives `CT010 = C.T_OH + CT007 + CT008 + CT009 + B.S_THK` — depth equals overhang
    plus front offset plus hole plus back offset plus backsplash. The labels have to describe that
    same chain, or a rule author reading them will build a different one.
    """
    assert SemanticType.CT010.label() == "countertop depth"
    assert SemanticType.CT007.label() == "sink front offset"
    assert SemanticType.CT008.label() == "sink hole depth"
    assert SemanticType.CT009.label() == "sink back offset"
    assert CLIENT_CODES["C.T_OH"].description == "countertop overhang"
    assert CLIENT_CODES["B.S_THK"].description == "backsplash thickness"
