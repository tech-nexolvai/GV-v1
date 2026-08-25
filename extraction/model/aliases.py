"""Which spellings mean the same thing — the deterministic half of matching (#167, B7.4).

Architectural and shop drawings rarely spell the same thing the same way. The alias table is the half of
matching that does not guess: a curated mapping from a spelling as printed to a canonical vocabulary term,
with an owner and a reason for every entry.

**An alias is a small rule, and is versioned like one.** `docs/DESIGN_EXTRACTION.md` §4.1 gives its identity
as `(spelling, rulebook_version)`. So a table is built for one version and frozen; a spelling means one
thing under rulebook `v3` and may mean something else under `v4`, and a past decision has to be replayable
against the table as it stood rather than as it stands. Nothing here edits an entry.

**This is where an ambiguous spelling is caught, because the database cannot catch it.**
`app/models/drawing.py` constrains `(spelling, canonical_term, rulebook_version)` to be unique — which
permits one spelling mapping to *two different terms* in the same version. That is not a hypothetical
gap: it is exactly what the fourth acceptance criterion warns about, and a lookup that could return two
canonical terms is a lookup that will silently return whichever row came back first. `build_table` refuses
to construct such a table and names every conflict.

**Lookup is exact, and that includes case.** `AGENTS.md` keeps the guessing lanes advisory; B9 handles OCR
variants and near-spellings, where a reviewer still sees the original. Deciding here that `CTOP` and `Ctop`
are the same word is itself a normalisation rule — a small one, but a rule — and the whole point of a
curated table is that such rules are written down with an owner rather than compiled in. A curator who
wants both spellings adds both, and then the table says so.

**The domain type is not the row.** `app/models/drawing.py` has the persisted `Alias`; this is a frozen
value object, and `extraction/` may not import `app/` (§2). The same split already exists between
`PageRecord` and the `pages` table. Saying so because #164 shipped a genuine duplicate — the test for that
one asserts the *drawing model* holds no two types of one name, and this is deliberately a different
package's concern.

Source: backend proposal §10.1 `aliases` · Design: `docs/DESIGN_EXTRACTION.md` §4.1 ·
Verification: `tests/extraction/model/test_aliases.py`
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from vocabulary.semantic_types import SemanticType

__all__ = [
    "Alias",
    "AliasConflict",
    "AliasConflictError",
    "AliasTable",
    "build_table",
]


@dataclass(frozen=True, slots=True)
class Alias:
    """One curated spelling, and who decided it means what.

    `added_by` and `rationale` are required, not optional. An alias with no author is an anonymous rule
    change, and one with no reason is a rule nobody can review — which defeats the point of writing it
    down. The persisted model states the same requirement as two `CheckConstraint`s.
    """

    spelling: str
    canonical: SemanticType
    rulebook_version: str
    added_by: str
    rationale: str

    def __post_init__(self) -> None:
        if not isinstance(self.canonical, SemanticType):
            raise TypeError(
                "canonical must be a SemanticType. A free string would let a typo be stored, matched "
                "against nothing, and never noticed."
            )
        for name, value in (
            ("spelling", self.spelling),
            ("rulebook_version", self.rulebook_version),
            ("added_by", self.added_by),
            ("rationale", self.rationale),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"{name} must be present and non-empty. "
                    + (
                        "An alias with no author is an anonymous rule change; one with no reason is a "
                        "rule nobody can review."
                        if name in {"added_by", "rationale"}
                        else "An alias is identified by its spelling and rulebook version."
                    )
                )

    @property
    def identity(self) -> tuple[str, str]:
        """`(spelling, rulebook_version)` — §4.1.

        The canonical term is deliberately *not* part of identity. If it were, one spelling mapping to two
        terms would be two perfectly valid aliases rather than the conflict it is.
        """
        return (self.spelling, self.rulebook_version)


@dataclass(frozen=True, slots=True)
class AliasConflict:
    """One spelling that maps to more than one canonical term in the same rulebook version."""

    spelling: str
    rulebook_version: str
    canonical_terms: tuple[SemanticType, ...]
    detail: str

    def __post_init__(self) -> None:
        if len(self.canonical_terms) < 2:
            raise ValueError("a conflict needs at least two canonical terms; one is just an alias")


class AliasConflictError(ValueError):
    """Raised instead of building a table whose lookups would be ambiguous.

    Carries every conflict rather than the first: a curator fixing the table wants the whole list, and
    finding them one build at a time is how the second one gets missed.
    """

    def __init__(self, conflicts: tuple[AliasConflict, ...]) -> None:
        self.conflicts = conflicts
        super().__init__(
            f"{len(conflicts)} spelling(s) map to more than one canonical term. "
            f"First: {conflicts[0].detail}"
        )


@dataclass(frozen=True, slots=True)
class AliasTable:
    """The curated aliases for one rulebook version. Immutable, and unambiguous by construction.

    Built only through `build_table`, which is what enforces the no-ambiguity guarantee. The constructor
    itself repeats the check rather than trusting the factory: a caller can reach the class directly, and a
    guarantee that depends on going through the front door is not a guarantee.
    """

    rulebook_version: str
    entries: tuple[Alias, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.rulebook_version, str) or not self.rulebook_version.strip():
            raise ValueError("a table belongs to one named rulebook version")
        wrong_version = {entry.rulebook_version for entry in self.entries} - {self.rulebook_version}
        if wrong_version:
            raise ValueError(
                f"every entry must belong to rulebook version {self.rulebook_version!r}; found "
                f"{sorted(wrong_version)}. Mixing versions in one table is how a past decision gets "
                "replayed against today's aliases."
            )
        conflicts = _conflicts(self.entries)
        if conflicts:
            raise AliasConflictError(conflicts)

    def lookup(self, spelling: str) -> SemanticType | None:
        """The canonical term for `spelling`, or `None`. **Exact match, including case.**

        `None` means this table has no entry — not "no match anywhere". A near-spelling is B9's to consider
        and stays advisory; returning a guess here would put a guessed term into the deterministic half of
        matching, which is the one place it must not go.
        """
        for entry in self.entries:
            if entry.spelling == spelling:
                return entry.canonical
        return None

    def spellings_for(self, canonical: SemanticType) -> tuple[str, ...]:
        """Every spelling that maps to `canonical`, in the order curated.

        Many-to-one is the normal case and not a conflict: `CTOP`, `C-TOP` and `COUNTER TOP` may all mean
        the same term. Only one spelling meaning two *terms* is ambiguous.
        """
        return tuple(entry.spelling for entry in self.entries if entry.canonical is canonical)


def _conflicts(entries: tuple[Alias, ...]) -> tuple[AliasConflict, ...]:
    """Every spelling mapping to more than one canonical term, in a stable order."""
    by_spelling: dict[tuple[str, str], list[SemanticType]] = defaultdict(list)
    for entry in entries:
        terms = by_spelling[(entry.spelling, entry.rulebook_version)]
        if entry.canonical not in terms:
            terms.append(entry.canonical)

    return tuple(
        AliasConflict(
            spelling=spelling,
            rulebook_version=version,
            canonical_terms=tuple(terms),
            detail=(
                f"{spelling!r} maps to {len(terms)} canonical terms under rulebook {version}: "
                f"{', '.join(term.value for term in terms)}. A lookup would return whichever entry came "
                "first, so the table is refused rather than silently preferring one."
            ),
        )
        for (spelling, version), terms in sorted(by_spelling.items())
        if len(terms) > 1
    )


def build_table(rulebook_version: str, entries: list[Alias]) -> AliasTable:
    """Build the table for one rulebook version, or raise naming every ambiguous spelling.

    Duplicate entries — the same spelling and the same term, added twice — are kept rather than collapsed.
    Two curators recording the same alias with different rationales is a fact about the curation, and
    silently dropping one loses an author's reason.
    """
    return AliasTable(rulebook_version=rulebook_version, entries=tuple(entries))
