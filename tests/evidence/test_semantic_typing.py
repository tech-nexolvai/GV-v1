"""The semantic-typing gate: exact proof can qualify; every guess remains review work."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from evidence.semantic_typing import (
    AgentTypeSuggestion,
    TypingDisposition,
    from_agent_suggestion,
    from_exact_tag,
)
from vocabulary.semantic_types import SemanticType

ALLOWED = frozenset({SemanticType.CT007, SemanticType.CABINET_WIDTH})


def test_exact_vector_tag_on_the_same_line_qualifies_without_mutating_a_candidate() -> None:
    decision = from_exact_tag(
        candidate_id=uuid4(),
        tag_candidate_id=uuid4(),
        tag_text="CT007",
        permitted_types=ALLOWED,
        shared_dimension_line=True,
        tag_is_vector_text=True,
    )

    assert decision.disposition is TypingDisposition.QUALIFIED
    assert decision.semantic_type is SemanticType.CT007
    assert decision.confidence == Decimal(1)


@pytest.mark.parametrize(
    ("tag_text", "permitted_types", "shared_dimension_line", "tag_is_vector_text"),
    [
        ("CT999", ALLOWED, True, True),
        ("CT007", frozenset({SemanticType.CABINET_WIDTH}), True, True),
        ("CT007", ALLOWED, False, True),
        ("CT007", ALLOWED, True, False),
    ],
)
def test_incomplete_mechanical_proof_abstains_to_reviewer(
    tag_text: str,
    permitted_types: frozenset[SemanticType],
    shared_dimension_line: bool,
    tag_is_vector_text: bool,
) -> None:
    decision = from_exact_tag(
        candidate_id=uuid4(),
        tag_candidate_id=uuid4(),
        tag_text=tag_text,
        permitted_types=permitted_types,
        shared_dimension_line=shared_dimension_line,
        tag_is_vector_text=tag_is_vector_text,
    )

    assert decision.disposition is TypingDisposition.REVIEW_REQUIRED


def test_agent_confidence_cannot_qualify_or_supply_a_verdict_operand() -> None:
    decision = from_agent_suggestion(
        AgentTypeSuggestion(
            candidate_id=uuid4(),
            semantic_type=SemanticType.CABINET_WIDTH,
            confidence=Decimal(1),
            reason="the crop appears to show a cabinet run",
        ),
        permitted_types=ALLOWED,
    )

    assert decision.disposition is TypingDisposition.REVIEW_REQUIRED
    assert decision.semantic_type is SemanticType.CABINET_WIDTH
    assert "reviewer confirmation" in decision.reason
