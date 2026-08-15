"""Deterministic architectural-to-shop identifier matching.

Exact normalized identifiers are considered before explicit aliases. Project,
package revision, category and document role are hard filters: a candidate that
crosses any of those boundaries is never emitted. Results remain advisory
``MatchCandidate`` values and cannot represent approval.

Source: ``docs/DESIGN_EXTRACTION.md`` section 8 and issue #128.
Verification: ``tests/retrieval/test_matching.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum, auto
from uuid import UUID

from retrieval.candidate import Lane, MatchCandidate
from retrieval.identifiers import IdentifierAliasTable, NormalizedIdentifier


class MatchDocumentRole(StrEnum):
    """The two document roles accepted by arch-to-shop matching."""

    ARCH = auto()
    SHOP = auto()


class MatchStatus(StrEnum):
    """The explicit result of attempting to match one drawing item."""

    MATCHED = auto()
    UNMATCHED = auto()
    AMBIGUOUS = auto()


@dataclass(frozen=True, slots=True)
class MatchableItem:
    """The minimum immutable item projection required by this matching lane."""

    item_id: UUID
    identifier: NormalizedIdentifier | None
    project_id: UUID
    package_revision_id: UUID
    category: str
    document_role: MatchDocumentRole

    def __post_init__(self) -> None:
        """Reject incomplete scope metadata before matching begins."""

        if not isinstance(self.item_id, UUID):
            raise TypeError("item_id must be a UUID")
        if self.identifier is not None and not isinstance(self.identifier, NormalizedIdentifier):
            raise TypeError("identifier must be a NormalizedIdentifier or None")
        if not isinstance(self.project_id, UUID):
            raise TypeError("project_id must be a UUID")
        if not isinstance(self.package_revision_id, UUID):
            raise TypeError("package_revision_id must be a UUID")
        if not isinstance(self.category, str) or not self.category.strip():
            raise ValueError("category must be a non-empty string")
        if not isinstance(self.document_role, MatchDocumentRole):
            raise TypeError("document_role must be a MatchDocumentRole")


@dataclass(frozen=True, slots=True)
class MatchResult:
    """An explicit match, refusal to choose, or unmatched drawing item."""

    subject_item_id: UUID
    status: MatchStatus
    candidates: tuple[MatchCandidate, ...]
    reason: str

    def __post_init__(self) -> None:
        """Keep result status and candidate cardinality consistent."""

        if not isinstance(self.subject_item_id, UUID):
            raise TypeError("subject_item_id must be a UUID")
        if not isinstance(self.status, MatchStatus):
            raise TypeError("status must be a MatchStatus")
        if not isinstance(self.candidates, tuple) or not all(
            isinstance(candidate, MatchCandidate) for candidate in self.candidates
        ):
            raise TypeError("candidates must be a tuple of MatchCandidate values")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be a non-empty string")

        expected = {
            MatchStatus.MATCHED: len(self.candidates) == 1,
            MatchStatus.UNMATCHED: not self.candidates,
            MatchStatus.AMBIGUOUS: len(self.candidates) >= 2,
        }
        if not expected[self.status]:
            raise ValueError(f"{self.status.value} has invalid candidate cardinality")


def _validate_roles(architectural: Sequence[MatchableItem], shop: Sequence[MatchableItem]) -> None:
    if any(item.document_role is not MatchDocumentRole.ARCH for item in architectural):
        raise ValueError("architectural items must have the ARCH document role")
    if any(item.document_role is not MatchDocumentRole.SHOP for item in shop):
        raise ValueError("shop items must have the SHOP document role")


def _same_scope(left: MatchableItem, right: MatchableItem) -> bool:
    return (
        left.project_id == right.project_id
        and left.package_revision_id == right.package_revision_id
        and left.category == right.category
    )


def _candidate(left: MatchableItem, right: MatchableItem, lane: Lane) -> MatchCandidate:
    return MatchCandidate(
        left_item_id=left.item_id,
        right_item_id=right.item_id,
        lane=lane,
        score=None,
    )


def _result_for(
    left: MatchableItem, candidates: tuple[MatchCandidate, ...], lane: Lane
) -> MatchResult:
    if not candidates:
        return MatchResult(
            subject_item_id=left.item_id,
            status=MatchStatus.UNMATCHED,
            candidates=(),
            reason="no scoped shop item has the same exact or aliased identifier",
        )
    if len(candidates) == 1:
        return MatchResult(
            subject_item_id=left.item_id,
            status=MatchStatus.MATCHED,
            candidates=candidates,
            reason=f"one deterministic {lane.value} identifier candidate was found",
        )
    return MatchResult(
        subject_item_id=left.item_id,
        status=MatchStatus.AMBIGUOUS,
        candidates=candidates,
        reason=f"multiple scoped shop items share the {lane.value} identifier; no item was chosen",
    )


def exact_match(
    architectural: Sequence[MatchableItem], shop: Sequence[MatchableItem]
) -> tuple[MatchResult, ...]:
    """Match normalized identifiers exactly after applying every hard scope filter."""

    return _match(architectural, shop, aliases=None)


def alias_match(
    architectural: Sequence[MatchableItem],
    shop: Sequence[MatchableItem],
    aliases: IdentifierAliasTable,
) -> tuple[MatchResult, ...]:
    """Match exact identifiers first, then use explicit aliases only as fallback.

    An exact candidate is pinned: aliases are not consulted when at least one exact
    candidate exists, even when that exact result is ambiguous.
    """

    if not isinstance(aliases, IdentifierAliasTable):
        raise TypeError("aliases must be an IdentifierAliasTable")
    return _match(architectural, shop, aliases=aliases)


def _match(
    architectural: Sequence[MatchableItem],
    shop: Sequence[MatchableItem],
    *,
    aliases: IdentifierAliasTable | None,
) -> tuple[MatchResult, ...]:
    _validate_roles(architectural, shop)
    results: list[MatchResult] = []
    proposed_shop_ids: set[UUID] = set()

    for left in architectural:
        if left.identifier is None:
            results.append(_result_for(left, (), Lane.EXACT))
            continue

        eligible = tuple(right for right in shop if _same_scope(left, right))
        exact = tuple(
            _candidate(left, right, Lane.EXACT)
            for right in eligible
            if right.identifier is not None
            and right.identifier.canonical == left.identifier.canonical
        )
        if exact:
            proposed_shop_ids.update(candidate.right_item_id for candidate in exact)
            results.append(_result_for(left, exact, Lane.EXACT))
            continue

        if aliases is None:
            results.append(_result_for(left, (), Lane.EXACT))
            continue

        left_canonical = aliases.resolve(left.identifier.raw).canonical
        aliased = tuple(
            _candidate(left, right, Lane.ALIAS)
            for right in eligible
            if right.identifier is not None
            and aliases.resolve(right.identifier.raw).canonical == left_canonical
        )
        proposed_shop_ids.update(candidate.right_item_id for candidate in aliased)
        results.append(_result_for(left, aliased, Lane.ALIAS))

    results.extend(
        MatchResult(
            subject_item_id=right.item_id,
            status=MatchStatus.UNMATCHED,
            candidates=(),
            reason="shop item was not proposed for any architectural item",
        )
        for right in shop
        if right.item_id not in proposed_shop_ids
    )
    return tuple(results)
