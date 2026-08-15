"""Deterministic identifier normalisation and explicit identifier aliases.

Printed values are retained exactly for review. Only ASCII whitespace, hyphens and
underscores are removed from the comparison form; unsupported punctuation is rejected
rather than silently erased. Identifier aliases map identifier to identifier and are
separate from the semantic-vocabulary aliases owned by B7.4.

Source: backend proposal section 7.3, system design section 9, and issue #127.
Verification: ``tests/retrieval/test_identifiers.py``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

SEPARATORS = re.compile(r"[\s_-]+")
CANONICAL_FORM = re.compile(r"^[A-Z0-9]+$")


class IdentifierAliasConflictError(ValueError):
    """Raised when one normalized spelling names different canonical identifiers."""


class IdentifierAliasTargetError(ValueError):
    """Raised when an alias points at another alias instead of a final identifier."""


def _canonicalize(raw: str) -> str:
    if not isinstance(raw, str):
        raise TypeError("identifier must be a string")
    canonical = SEPARATORS.sub("", raw).upper()
    if not canonical:
        raise ValueError("identifier must contain at least one letter or digit")
    if CANONICAL_FORM.fullmatch(canonical) is None:
        raise ValueError(
            "identifier may contain only ASCII letters, digits, whitespace, hyphens and underscores"
        )
    return canonical


@dataclass(frozen=True, slots=True)
class NormalizedIdentifier:
    """An exact printed identifier alongside its deterministic comparison form."""

    raw: str
    canonical: str

    def __post_init__(self) -> None:
        """Require a printable raw value and a valid canonical comparison form."""

        if not isinstance(self.raw, str):
            raise TypeError("raw must be a string")
        if not isinstance(self.canonical, str):
            raise TypeError("canonical must be a string")
        if CANONICAL_FORM.fullmatch(self.canonical) is None:
            raise ValueError("canonical must contain only uppercase ASCII letters and digits")

    @property
    def display(self) -> str:
        """Return exactly what the drawing printed, without reconstructing lost separators."""

        return self.raw


def normalize_identifier(raw: str) -> NormalizedIdentifier:
    """Return the retained printed value and its deterministic exact-match form."""

    return NormalizedIdentifier(raw=raw, canonical=_canonicalize(raw))


@dataclass(frozen=True, slots=True)
class IdentifierAlias:
    """One explicit spelling mapped directly to its final canonical identifier."""

    spelling: str
    canonical_identifier: str

    def __post_init__(self) -> None:
        """Validate both sides using the same auditable normalisation policy."""

        _canonicalize(self.spelling)
        _canonicalize(self.canonical_identifier)


@dataclass(frozen=True, slots=True)
class IdentifierAliasTable:
    """An immutable, versioned set of exact identifier aliases.

    Alias chains are rejected. Every mapping must name its final identifier directly,
    which keeps resolution one-step, deterministic and reviewable.
    """

    version: str
    aliases: tuple[IdentifierAlias, ...]
    _index: Mapping[str, str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Build an immutable lookup and reject ambiguity, chains and cycles."""

        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("alias table version must be a non-empty string")
        if not isinstance(self.aliases, tuple):
            raise TypeError("aliases must be a tuple of IdentifierAlias values")

        index: dict[str, str] = {}
        for alias in self.aliases:
            if not isinstance(alias, IdentifierAlias):
                raise TypeError("aliases must contain only IdentifierAlias values")
            spelling = _canonicalize(alias.spelling)
            target = _canonicalize(alias.canonical_identifier)
            existing = index.get(spelling)
            if existing is not None and existing != target:
                raise IdentifierAliasConflictError(
                    f"normalized alias {spelling!r} maps to both {existing!r} and {target!r}"
                )
            index[spelling] = target

        chained = sorted(spelling for spelling, target in index.items() if target in index)
        if chained:
            names = ", ".join(repr(name) for name in chained)
            raise IdentifierAliasTargetError(
                f"aliases must point directly to final identifiers; chained or cyclic: {names}"
            )
        object.__setattr__(self, "_index", MappingProxyType(index))

    def resolve(self, raw: str) -> NormalizedIdentifier:
        """Apply one explicit alias, or return the ordinary normalized identifier."""

        normalized = normalize_identifier(raw)
        target = self._index.get(normalized.canonical, normalized.canonical)
        return NormalizedIdentifier(raw=normalized.raw, canonical=target)
