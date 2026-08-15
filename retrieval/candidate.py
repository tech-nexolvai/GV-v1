"""Advisory match proposals produced by retrieval lanes.

The type deliberately cannot express approval. Deterministic validation or a reviewer
must create the separate approved-match type in a later boundary; retrieval never does.

Source: ``DESIGN_EXTRACTION.md`` section 8, backend proposal sections 2 and 7.3,
and issue #173.
Verification: ``tests/retrieval/test_candidate.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum, auto
from uuid import UUID


class Lane(StrEnum):
    """The retrieval route that proposed a possible correspondence."""

    EXACT = auto()
    ALIAS = auto()
    METADATA = auto()
    GEOMETRY = auto()
    TRIGRAM = auto()
    LEXICAL = auto()
    DENSE = auto()
    FUSION = auto()


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    """An unapproved proposal that two items correspond.

    ``score`` is diagnostic metadata for ranking and review only. It grants no
    authority, and this type has no approval field or conversion into verdict input.
    """

    left_item_id: UUID
    right_item_id: UUID
    lane: Lane
    score: Decimal | None

    def __post_init__(self) -> None:
        """Reject permissive values that could blur the advisory boundary."""

        if not isinstance(self.left_item_id, UUID):
            raise TypeError("left_item_id must be a UUID")
        if not isinstance(self.right_item_id, UUID):
            raise TypeError("right_item_id must be a UUID")
        if not isinstance(self.lane, Lane):
            raise TypeError("lane must be a Lane")
        if self.score is not None and not isinstance(self.score, Decimal):
            raise TypeError("score must be a Decimal or None; float is not allowed")
