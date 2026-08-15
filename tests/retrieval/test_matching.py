"""Tests for deterministic, scoped arch-to-shop identifier matching."""

from __future__ import annotations

from uuid import UUID

import pytest

from retrieval.candidate import Lane, MatchCandidate
from retrieval.identifiers import IdentifierAlias, IdentifierAliasTable, normalize_identifier
from retrieval.matching import (
    MatchableItem,
    MatchDocumentRole,
    MatchResult,
    MatchStatus,
    alias_match,
    exact_match,
)

PROJECT = UUID("00000000-0000-0000-0000-000000000001")
OTHER_PROJECT = UUID("00000000-0000-0000-0000-000000000002")
REVISION = UUID("00000000-0000-0000-0000-000000000010")
OTHER_REVISION = UUID("00000000-0000-0000-0000-000000000011")


def _item(
    number: int,
    identifier: str | None,
    role: MatchDocumentRole,
    *,
    project: UUID = PROJECT,
    revision: UUID = REVISION,
    category: str = "cabinet",
) -> MatchableItem:
    return MatchableItem(
        item_id=UUID(int=number),
        identifier=None if identifier is None else normalize_identifier(identifier),
        project_id=project,
        package_revision_id=revision,
        category=category,
        document_role=role,
    )


def test_exact_normalized_identifier_produces_an_advisory_candidate() -> None:
    """Input: PL-02 and pl_02. Outcome: exact candidate. Why: normalization is deterministic."""

    result = exact_match(
        [_item(1, "PL-02", MatchDocumentRole.ARCH)],
        [_item(2, "pl_02", MatchDocumentRole.SHOP)],
    )[0]

    assert result.status is MatchStatus.MATCHED
    assert result.candidates == (MatchCandidate(UUID(int=1), UUID(int=2), Lane.EXACT, None),)
    assert not hasattr(result.candidates[0], "approved")


def test_exact_candidate_is_pinned_before_an_alias_candidate() -> None:
    """Input: one exact and one alias. Outcome: exact only. Why: aliases never displace exact."""

    aliases = IdentifierAliasTable(
        version="v1",
        aliases=(IdentifierAlias("OLD-PL-02", "PL-02"),),
    )
    result = alias_match(
        [_item(1, "PL-02", MatchDocumentRole.ARCH)],
        [
            _item(2, "PL02", MatchDocumentRole.SHOP),
            _item(3, "OLD-PL-02", MatchDocumentRole.SHOP),
        ],
        aliases,
    )[0]

    assert result.status is MatchStatus.MATCHED
    assert tuple(candidate.right_item_id for candidate in result.candidates) == (UUID(int=2),)
    assert result.candidates[0].lane is Lane.EXACT


def test_explicit_alias_is_used_only_when_no_exact_candidate_exists() -> None:
    """Input: explicit old-to-new alias. Outcome: alias candidate. Why: the mapping is reviewed data."""

    aliases = IdentifierAliasTable(
        version="v1",
        aliases=(IdentifierAlias("OLD-7", "WD-03"),),
    )
    result = alias_match(
        [_item(1, "WD-03", MatchDocumentRole.ARCH)],
        [_item(2, "OLD-7", MatchDocumentRole.SHOP)],
        aliases,
    )[0]

    assert result.status is MatchStatus.MATCHED
    assert result.candidates[0].lane is Lane.ALIAS


def test_unmatched_item_is_reported_instead_of_dropped() -> None:
    """Input: an arch item with no shop peer. Outcome: UNMATCHED. Why: absence must stay visible."""

    result = exact_match([_item(1, "PL-02", MatchDocumentRole.ARCH)], [])[0]

    assert result.status is MatchStatus.UNMATCHED
    assert result.subject_item_id == UUID(int=1)
    assert result.candidates == ()


def test_unmatched_shop_item_is_also_reported_instead_of_dropped() -> None:
    """Input: a shop item with no arch peer. Outcome: UNMATCHED. Why: extras must stay visible."""

    results = exact_match([], [_item(2, "PL-02", MatchDocumentRole.SHOP)])

    assert len(results) == 1
    assert results[0].status is MatchStatus.UNMATCHED
    assert results[0].subject_item_id == UUID(int=2)
    assert results[0].candidates == ()


def test_one_to_many_identifier_is_ambiguous_and_never_chosen() -> None:
    """Input: two scoped shop peers. Outcome: AMBIGUOUS. Why: retrieval cannot choose evidence."""

    result = exact_match(
        [_item(1, "PL-02", MatchDocumentRole.ARCH)],
        [
            _item(2, "PL-02", MatchDocumentRole.SHOP),
            _item(3, "PL_02", MatchDocumentRole.SHOP),
        ],
    )[0]

    assert result.status is MatchStatus.AMBIGUOUS
    assert len(result.candidates) == 2
    assert "no item was chosen" in result.reason


@pytest.mark.parametrize(
    ("shop_kwargs", "why"),
    [
        ({"project": OTHER_PROJECT}, "another project must never leak into retrieval"),
        ({"revision": OTHER_REVISION}, "another package revision is not the pinned source"),
        ({"category": "countertop"}, "a shared identifier does not erase semantic category"),
    ],
)
def test_hard_scope_mismatch_cannot_become_a_candidate(
    shop_kwargs: dict[str, object], why: str
) -> None:
    """Input: same identifier outside one hard scope. Outcome: UNMATCHED. Why: see case label."""

    assert why
    left = _item(1, "PL-02", MatchDocumentRole.ARCH)
    right = _item(2, "PL-02", MatchDocumentRole.SHOP, **shop_kwargs)  # type: ignore[arg-type]

    assert exact_match([left], [right])[0].status is MatchStatus.UNMATCHED


def test_wrong_document_role_is_a_caller_error() -> None:
    """Input: SHOP item in arch input. Outcome: error. Why: role is a hard filter, not a hint."""

    with pytest.raises(ValueError, match="ARCH document role"):
        exact_match([_item(1, "PL-02", MatchDocumentRole.SHOP)], [])


@pytest.mark.parametrize(
    ("status", "candidates"),
    [
        (MatchStatus.MATCHED, ()),
        (
            MatchStatus.UNMATCHED,
            (MatchCandidate(UUID(int=1), UUID(int=2), Lane.EXACT, None),),
        ),
        (
            MatchStatus.AMBIGUOUS,
            (MatchCandidate(UUID(int=1), UUID(int=2), Lane.EXACT, None),),
        ),
    ],
)
def test_result_status_cannot_claim_the_wrong_cardinality(
    status: MatchStatus, candidates: tuple[MatchCandidate, ...]
) -> None:
    """Input: contradictory status/count. Outcome: error. Why: consumers need explicit truth."""

    with pytest.raises(ValueError, match="invalid candidate cardinality"):
        MatchResult(UUID(int=1), status, candidates, "test contradiction")
