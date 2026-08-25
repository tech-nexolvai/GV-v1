"""The alias table (#167, B7.4).

The alias table is the half of matching that does not guess, so the tests that matter are the ones proving
it refuses rather than resolves: an ambiguous spelling, a mixed-version table, an alias with no author.

One of them is load-bearing in a way worth stating. `app/models/drawing.py` constrains
`(spelling, canonical_term, rulebook_version)` to be unique, which **permits** one spelling mapping to two
different terms in the same version. The database cannot catch that, so this module is the only place it
can be caught.

Source: `docs/DESIGN_EXTRACTION.md` §4.1 · Verification: this file
"""

from __future__ import annotations

import pytest

from extraction.model.aliases import (
    Alias,
    AliasConflict,
    AliasConflictError,
    AliasTable,
    build_table,
)
from vocabulary.semantic_types import SemanticType

V3 = "v3"


def _alias(
    spelling: str,
    canonical: SemanticType = SemanticType.CABINET_WIDTH,
    *,
    version: str = V3,
    added_by: str = "raj",
    rationale: str = "seen on three Ridgewood packages",
) -> Alias:
    return Alias(
        spelling=spelling,
        canonical=canonical,
        rulebook_version=version,
        added_by=added_by,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Every alias has an owner and a reason
# ---------------------------------------------------------------------------


def test_an_alias_records_who_added_it_and_why() -> None:
    """The first acceptance criterion. An alias is a small rule and needs an owner."""
    alias = _alias("CTOP")

    assert alias.added_by == "raj"
    assert alias.rationale == "seen on three Ridgewood packages"


@pytest.mark.parametrize("field", ["added_by", "rationale"])
def test_an_alias_without_an_owner_or_a_reason_is_refused(field: str) -> None:
    """Not optional, and not defaulted to a placeholder.

    An alias with no author is an anonymous rule change; one with no reason is a rule nobody can review —
    which defeats the point of writing it down. The persisted model states the same as two check
    constraints, and this is the same requirement one layer up.
    """
    for blank in ("", "   "):
        with pytest.raises(ValueError, match=field):
            _alias("CTOP", **{field: blank})  # type: ignore[arg-type]


def test_a_free_string_canonical_term_is_refused() -> None:
    """The target is the controlled vocabulary, not a string.

    A typo like `"countertop_widht"` would be stored, matched against nothing, and never noticed — the
    same argument `extraction/model/items.py` makes about `SemanticType`.
    """
    with pytest.raises(TypeError, match="SemanticType"):
        Alias(
            spelling="CTOP",
            canonical="cabinet_width",  # type: ignore[arg-type]
            rulebook_version=V3,
            added_by="raj",
            rationale="why",
        )


# ---------------------------------------------------------------------------
# Versioned, not edited in place
# ---------------------------------------------------------------------------


def test_identity_is_the_spelling_and_the_rulebook_version() -> None:
    """§4.1. The canonical term is deliberately *not* part of identity.

    If it were, one spelling mapping to two terms would be two perfectly valid aliases rather than the
    conflict it is — the ambiguity would become unrepresentable as a problem.
    """
    assert _alias("CTOP").identity == ("CTOP", V3)
    assert _alias("CTOP", SemanticType.MATERIAL).identity == _alias("CTOP").identity


def test_the_same_spelling_in_two_rulebook_versions_is_two_aliases() -> None:
    """A spelling may mean one thing under v3 and another under v4.

    That is the point of versioning: a past decision is replayed against the table as it stood, not as it
    stands.
    """
    old = _alias("CTOP", SemanticType.CABINET_WIDTH, version="v3")
    new = _alias("CTOP", SemanticType.COUNTERTOP_OVERALL_WIDTH, version="v4")

    assert old.identity != new.identity
    # And each builds a table of its own without conflicting with the other.
    assert build_table("v3", [old]).lookup("CTOP") is SemanticType.CABINET_WIDTH
    assert build_table("v4", [new]).lookup("CTOP") is SemanticType.COUNTERTOP_OVERALL_WIDTH


def test_a_table_mixing_rulebook_versions_is_refused() -> None:
    """Mixing versions in one table is how a past decision gets replayed against today's aliases."""
    with pytest.raises(ValueError, match="every entry must belong to rulebook version"):
        build_table("v3", [_alias("CTOP", version="v3"), _alias("CTOP2", version="v4")])


def test_the_table_and_its_entries_are_frozen() -> None:
    """Not edited in place — the second acceptance criterion.

    A table that could be mutated after a finding cited it would answer "what did this spelling mean?"
    with whatever was most recently convenient.
    """
    from dataclasses import FrozenInstanceError

    table = build_table(V3, [_alias("CTOP")])
    with pytest.raises(FrozenInstanceError):
        table.entries = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        table.entries[0].canonical = SemanticType.MATERIAL  # type: ignore[misc]


# ---------------------------------------------------------------------------
# An ambiguous spelling is refused — the database cannot catch this
# ---------------------------------------------------------------------------


def test_one_spelling_mapping_to_two_terms_is_refused() -> None:
    """**The criterion, and the gap the database leaves.**

    `app/models/drawing.py` constrains `(spelling, canonical_term, rulebook_version)` — so two rows with
    the same spelling and *different* terms both satisfy it. A lookup would return whichever came back
    first, which is a silent choice between two meanings.
    """
    with pytest.raises(AliasConflictError) as raised:
        build_table(
            V3,
            [
                _alias("CTOP", SemanticType.CABINET_WIDTH),
                _alias("CTOP", SemanticType.COUNTERTOP_OVERALL_WIDTH),
            ],
        )

    conflict = raised.value.conflicts[0]
    assert conflict.spelling == "CTOP"
    assert len(conflict.canonical_terms) == 2
    assert "whichever entry came first" in conflict.detail


def test_every_conflict_is_reported_not_just_the_first() -> None:
    """A curator fixing the table wants the whole list; finding them one build at a time misses the
    second."""
    with pytest.raises(AliasConflictError) as raised:
        build_table(
            V3,
            [
                _alias("CTOP", SemanticType.CABINET_WIDTH),
                _alias("CTOP", SemanticType.MATERIAL),
                _alias("BC", SemanticType.CABINET_WIDTH),
                _alias("BC", SemanticType.WALL_CONFIG),
            ],
        )

    assert {c.spelling for c in raised.value.conflicts} == {"BC", "CTOP"}


def test_the_constructor_repeats_the_check_rather_than_trusting_the_factory() -> None:
    """A guarantee that depends on going through the front door is not a guarantee.

    A caller can reach `AliasTable` directly, so the invariant lives in `__post_init__` and `build_table`
    is a convenience over it.
    """
    with pytest.raises(AliasConflictError):
        AliasTable(
            rulebook_version=V3,
            entries=(
                _alias("CTOP", SemanticType.CABINET_WIDTH),
                _alias("CTOP", SemanticType.MATERIAL),
            ),
        )


def test_many_spellings_for_one_term_is_not_a_conflict() -> None:
    """The normal case. `CTOP`, `C-TOP` and `COUNTER TOP` may all mean one term.

    Only one spelling meaning two *terms* is ambiguous. Refusing many-to-one would make the table useless
    for the thing it exists to do.
    """
    table = build_table(
        V3,
        [
            _alias("CTOP", SemanticType.COUNTERTOP_OVERALL_WIDTH),
            _alias("C-TOP", SemanticType.COUNTERTOP_OVERALL_WIDTH),
            _alias("COUNTER TOP", SemanticType.COUNTERTOP_OVERALL_WIDTH),
        ],
    )

    assert table.spellings_for(SemanticType.COUNTERTOP_OVERALL_WIDTH) == (
        "CTOP",
        "C-TOP",
        "COUNTER TOP",
    )


def test_the_same_alias_added_twice_is_kept_rather_than_collapsed() -> None:
    """Two curators recording the same alias with different reasons is a fact about the curation.

    Silently dropping one loses an author's rationale, and the rationale is the reviewable part.
    """
    table = build_table(
        V3,
        [
            _alias("CTOP", rationale="seen on Ridgewood"),
            _alias("CTOP", rationale="confirmed by Raj 2026-08"),
        ],
    )

    assert len(table.entries) == 2
    assert table.lookup("CTOP") is SemanticType.CABINET_WIDTH, "no ambiguity: one term, twice"


def test_a_conflict_of_one_term_cannot_be_constructed() -> None:
    with pytest.raises(ValueError, match="at least two canonical terms"):
        AliasConflict("CTOP", V3, (SemanticType.CABINET_WIDTH,), "only one")


# ---------------------------------------------------------------------------
# Lookup is exact, not fuzzy
# ---------------------------------------------------------------------------


def test_lookup_is_exact() -> None:
    table = build_table(V3, [_alias("CTOP", SemanticType.COUNTERTOP_OVERALL_WIDTH)])

    assert table.lookup("CTOP") is SemanticType.COUNTERTOP_OVERALL_WIDTH
    assert table.lookup("CTOPS") is None
    assert table.lookup("CTO") is None
    assert table.lookup("C TOP") is None


def test_lookup_does_not_fold_case() -> None:
    """**The strictness worth arguing about, so the reasoning is here.**

    Deciding that `CTOP` and `Ctop` are the same word is itself a normalisation rule — a small one, but a
    rule — and the point of a curated table is that such rules are written down with an owner rather than
    compiled in. A curator who wants both adds both, and then the table says so.

    Near-spellings are B9's, where a lane stays advisory and a reviewer still sees the original.
    """
    table = build_table(V3, [_alias("CTOP")])

    assert table.lookup("CTOP") is SemanticType.CABINET_WIDTH
    assert table.lookup("Ctop") is None
    assert table.lookup("ctop") is None


def test_an_unknown_spelling_returns_none_rather_than_the_nearest() -> None:
    """`None` means this table has no entry — not "no match anywhere".

    Returning a near miss would put a guessed term into the deterministic half of matching, which is the
    one place it must not go.
    """
    table = build_table(
        V3,
        [
            _alias("CTOP", SemanticType.COUNTERTOP_OVERALL_WIDTH),
            _alias("BC", SemanticType.CABINET_WIDTH),
        ],
    )

    assert table.lookup("CTP") is None, "no edit-distance match"
    assert table.lookup("B") is None, "no prefix match"


def test_an_empty_table_is_legitimate() -> None:
    """A rulebook version with no aliases yet is a real state, not an error."""
    table = build_table(V3, [])
    assert table.lookup("CTOP") is None
    assert table.entries == ()


def test_nothing_fuzzy_is_reachable_from_this_module() -> None:
    """Asserted against the imports, because "exact" is easier to claim than to keep.

    A `difflib` or `rapidfuzz` import here would move a guessing lane into the half that does not guess.
    """
    import ast
    from pathlib import Path

    import extraction.model.aliases as module

    tree = ast.parse(Path(module.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    for fuzzy in ("difflib", "rapidfuzz", "Levenshtein", "fuzzywuzzy", "re"):
        assert fuzzy not in imported, f"aliases.py imports {fuzzy}, which is a guessing lane"

    for forbidden in ("verdict", "rules", "retrieval", "app"):
        assert forbidden not in imported, f"aliases.py imports {forbidden}"
