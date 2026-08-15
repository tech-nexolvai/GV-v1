"""Identifier normalisation and explicit alias tests for issue #127."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from retrieval.identifiers import (
    IdentifierAlias,
    IdentifierAliasConflictError,
    IdentifierAliasTable,
    IdentifierAliasTargetError,
    NormalizedIdentifier,
    normalize_identifier,
)


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("X-223", "X223"),
        ("x 223", "X223"),
        ("VAN-01-A", "VAN01A"),
        ("A_CAB_1", "ACAB1"),
        ("\t pl - 02 \n", "PL02"),
    ],
)
def test_case_whitespace_and_documented_separators_normalize_deterministically(
    raw: str, canonical: str
) -> None:
    """Input: printed variant. Outcome: expected form. Why: exact matching needs one spelling."""

    first = normalize_identifier(raw)
    second = normalize_identifier(raw)
    assert first == second == NormalizedIdentifier(raw=raw, canonical=canonical)


def test_printed_value_survives_for_display() -> None:
    """Input: spaced lowercase text. Outcome: raw retained. Why: reviewers see the drawing."""

    raw = "  x-223  "
    identifier = normalize_identifier(raw)
    assert identifier.raw == raw
    assert identifier.display == raw
    assert identifier.canonical == "X223"


def test_nearby_product_codes_remain_distinct() -> None:
    """Input: X-223 and X-233. Outcome: unequal. Why: similarity must not merge products."""

    assert normalize_identifier("X-223").canonical == "X223"
    assert normalize_identifier("X-233").canonical == "X233"
    assert normalize_identifier("X-223") != normalize_identifier("X-233")


@pytest.mark.parametrize("raw", ["", " - _ \t", "X/223", "X.223", "Ä-223"])
def test_empty_or_unsupported_identifiers_are_rejected(raw: str) -> None:
    """Input: absent or unsupported syntax. Outcome: rejection. Why: never erase meaning."""

    with pytest.raises(ValueError):
        normalize_identifier(raw)


def test_non_string_identifier_is_rejected() -> None:
    """Input: integer identifier. Outcome: TypeError. Why: formatting must be explicit text."""

    with pytest.raises(TypeError, match="identifier must be a string"):
        normalize_identifier(223)  # type: ignore[arg-type]


def test_explicit_alias_resolves_without_losing_printed_value() -> None:
    """Input: declared old code. Outcome: final code. Why: known variants are not fuzzy guesses."""

    table = IdentifierAliasTable(
        version="2026-08-16",
        aliases=(IdentifierAlias("OLD-X-223", "X223"),),
    )
    resolved = table.resolve("old x 223")
    assert resolved == NormalizedIdentifier(raw="old x 223", canonical="X223")


def test_unknown_alias_uses_ordinary_normalization() -> None:
    """Input: undeclared code. Outcome: normalized only. Why: table never infers similarity."""

    table = IdentifierAliasTable(version="v1", aliases=())
    assert table.resolve("X-233") == NormalizedIdentifier(raw="X-233", canonical="X233")


def test_normalized_alias_conflict_is_rejected() -> None:
    """Input: two spellings collapsing to one alias with two targets. Outcome: conflict."""

    with pytest.raises(IdentifierAliasConflictError, match="maps to both"):
        IdentifierAliasTable(
            version="v1",
            aliases=(
                IdentifierAlias("OLD-X", "X223"),
                IdentifierAlias("old x", "X233"),
            ),
        )


@pytest.mark.parametrize(
    "aliases",
    [
        (
            IdentifierAlias("OLD-X", "LEGACY-X"),
            IdentifierAlias("LEGACY-X", "X223"),
        ),
        (
            IdentifierAlias("OLD-X", "LEGACY-X"),
            IdentifierAlias("LEGACY-X", "OLD-X"),
        ),
        (IdentifierAlias("X-223", "X223"),),
    ],
)
def test_alias_chains_cycles_and_self_aliases_are_rejected(
    aliases: tuple[IdentifierAlias, ...],
) -> None:
    """Input: indirect mapping. Outcome: rejection. Why: resolution must be one-step."""

    with pytest.raises(IdentifierAliasTargetError, match="directly to final identifiers"):
        IdentifierAliasTable(version="v1", aliases=aliases)


def test_alias_table_requires_a_version_and_is_immutable() -> None:
    """Input: versioned table. Outcome: immutable. Why: aliases cannot change invisibly."""

    with pytest.raises(ValueError, match="version"):
        IdentifierAliasTable(version="", aliases=())

    table = IdentifierAliasTable(version="v1", aliases=())
    with pytest.raises(FrozenInstanceError):
        table.version = "v2"  # type: ignore[misc]


def test_alias_collection_must_be_an_explicit_typed_tuple() -> None:
    """Input: permissive alias collections. Outcome: rejection. Why: data stays reviewable."""

    with pytest.raises(TypeError, match="aliases must be a tuple"):
        IdentifierAliasTable(version="v1", aliases=[])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="only IdentifierAlias"):
        IdentifierAliasTable(version="v1", aliases=("OLD-X",))  # type: ignore[arg-type]
