"""`PRODUCT_SPEC` means a document we can re-read — not an authoritative person (ADR-0015).

The failure this guards against is subtle and would never look like a bug. Somebody has the Kohler
sheet open, types the interior dimension into a form, and labels it `PRODUCT_SPEC` — because that
is, in a sense, where the number came from. The check then runs, passes, and its trace says the
expected value came from a product specification.

Nothing about that finding looks weaker than any other, and there is no document to go back to.
Provenance here is about what can be verified later, not about where a number originated.
"""

from __future__ import annotations

import pytest

from rules.semantic_types import (
    DOCUMENT_BACKED_SOURCES,
    DocumentName,
    OperandSource,
    UnknownVocabularyError,
    resolve,
)

# ---------------------------------------------------------------------------
# The member exists and is distinct
# ---------------------------------------------------------------------------


def test_product_spec_is_its_own_source() -> None:
    assert OperandSource.PRODUCT_SPEC.value == "PRODUCT_SPEC"


def test_product_spec_is_not_user_input() -> None:
    """The whole decision in one assertion. Collapsing these would let a number read out on a
    call carry the provenance of a hashed cut sheet."""
    assert OperandSource.PRODUCT_SPEC is not OperandSource.USER_INPUT


def test_every_source_is_accounted_for() -> None:
    """Pinned deliberately: adding a source is an architecture decision, and this test is the
    thing that makes someone notice they are making one."""
    assert {s.value for s in OperandSource} == {
        "ARCH",
        "SHOP",
        "LITERAL",
        "USER_INPUT",
        "PRODUCT_SPEC",
    }


# ---------------------------------------------------------------------------
# The `P_` prefix resolves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("A_Wall_2_Wall_Dim", OperandSource.ARCH),
        ("S_Wall_2_Wall_Dim", OperandSource.SHOP),
        ("F_Wall_2_Wall_Dim", OperandSource.USER_INPUT),
        ("P_Wall_2_Wall_Dim", OperandSource.PRODUCT_SPEC),
    ],
)
def test_prefix_maps_to_its_source(name: str, source: OperandSource) -> None:
    binding = resolve(name)
    assert isinstance(binding, DocumentName)
    assert binding.source is source


def test_an_unknown_prefix_still_raises_rather_than_defaulting() -> None:
    """Adding `P` must not turn the prefix table into something permissive."""
    with pytest.raises(UnknownVocabularyError):
        resolve("K_Wall_2_Wall_Dim")


# ---------------------------------------------------------------------------
# Document-backed vs not — the line the ADR draws
# ---------------------------------------------------------------------------


def test_product_spec_is_document_backed() -> None:
    assert OperandSource.PRODUCT_SPEC in DOCUMENT_BACKED_SOURCES


def test_drawings_are_document_backed() -> None:
    assert OperandSource.ARCH in DOCUMENT_BACKED_SOURCES
    assert OperandSource.SHOP in DOCUMENT_BACKED_SOURCES


@pytest.mark.parametrize("source", [OperandSource.USER_INPUT, OperandSource.LITERAL])
def test_a_value_with_no_document_behind_it_is_not_document_backed(
    source: OperandSource,
) -> None:
    """`USER_INPUT` is a person telling us a number; `LITERAL` is authored into the rule. Neither
    can be re-read against stored bytes, which is the only thing this set is about."""
    assert source not in DOCUMENT_BACKED_SOURCES


def test_the_document_backed_set_is_exactly_these_three() -> None:
    """Stated as an equality rather than three memberships, so a fourth source cannot be added to
    the set without this failing and someone having to justify it."""
    assert DOCUMENT_BACKED_SOURCES == frozenset(
        {OperandSource.ARCH, OperandSource.SHOP, OperandSource.PRODUCT_SPEC}
    )


# ---------------------------------------------------------------------------
# Cabinets stay a drawing concept
# ---------------------------------------------------------------------------


def test_a_cut_sheet_has_no_cabinet_index() -> None:
    """`P_CAB_1` is meaningless: a manufacturer's sheet describes a product, not the third cabinet
    in someone's kitchen. It must not resolve as a cabinet width."""
    with pytest.raises(UnknownVocabularyError):
        resolve("P_CAB_1")


@pytest.mark.parametrize("name", ["A_CAB_1", "S_CAB_7"])
def test_drawing_prefixes_still_resolve_cabinets(name: str) -> None:
    binding = resolve(name)
    assert isinstance(binding, DocumentName)
    assert binding.index is not None
