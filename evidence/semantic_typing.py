"""Fail-closed decisions about what a numeric reading means.

This module deliberately produces a *typing decision*, not a mutation of an extraction candidate.
An exact tag can be the second piece of evidence needed to qualify a reading, while an LLM may only
offer a reviewer-visible suggestion.  Neither route knows a rule, a tolerance, or a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from vocabulary.semantic_types import SemanticType

__all__ = [
    "AgentTypeSuggestion",
    "SemanticTypingDecision",
    "TypingDisposition",
    "TypingMethod",
    "from_agent_suggestion",
    "from_exact_tag",
]


class TypingMethod(StrEnum):
    """Who or what supplied a possible semantic label."""

    MECHANICAL_TAG = "MECHANICAL_TAG"
    AGENT = "AGENT"
    REVIEWER = "REVIEWER"


class TypingDisposition(StrEnum):
    """Whether the label may qualify a reading, or needs a reviewer."""

    QUALIFIED = "QUALIFIED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass(frozen=True, slots=True)
class SemanticTypingDecision:
    """One auditable type outcome for one extracted reading.

    ``QUALIFIED`` is intentionally possible only for an exact mechanical tag.  A model confidence
    is a useful sorting hint for a reviewer but cannot be independent evidence of the label it made.
    """

    candidate_id: UUID
    semantic_type: SemanticType | None
    method: TypingMethod
    disposition: TypingDisposition
    reason: str
    tag_candidate_id: UUID | None = None
    confidence: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, UUID):
            raise TypeError("candidate_id must be a UUID")
        if self.semantic_type is not None and not isinstance(self.semantic_type, SemanticType):
            raise TypeError("semantic_type must be a SemanticType or None")
        if not isinstance(self.method, TypingMethod):
            raise TypeError("method must be a TypingMethod")
        if not isinstance(self.disposition, TypingDisposition):
            raise TypeError("disposition must be a TypingDisposition")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("a semantic typing decision needs a reason")
        if self.tag_candidate_id is not None and not isinstance(self.tag_candidate_id, UUID):
            raise TypeError("tag_candidate_id must be a UUID or None")
        if self.confidence is not None:
            if isinstance(self.confidence, float) or not isinstance(self.confidence, Decimal):
                raise TypeError("confidence must be an exact Decimal or None")
            if not self.confidence.is_finite() or not Decimal(0) <= self.confidence <= Decimal(1):
                raise ValueError("confidence must be between zero and one")
        if self.disposition is TypingDisposition.QUALIFIED and not (
            self.method is TypingMethod.MECHANICAL_TAG
            and self.semantic_type is not None
            and self.tag_candidate_id is not None
            and self.confidence == Decimal(1)
        ):
            raise ValueError(
                "only an exact mechanical tag with a tag candidate may qualify a semantic type"
            )


@dataclass(frozen=True, slots=True)
class AgentTypeSuggestion:
    """Validated, bounded output from an ambiguity-only typing agent.

    The agent is allowed to choose only from a caller-supplied semantic vocabulary.  Its output does
    not qualify a reading even at confidence one; it stays an explicit request for reviewer action.
    """

    candidate_id: UUID
    semantic_type: SemanticType | None
    confidence: Decimal | None
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, UUID):
            raise TypeError("candidate_id must be a UUID")
        if self.semantic_type is not None and not isinstance(self.semantic_type, SemanticType):
            raise TypeError("semantic_type must be a SemanticType or None")
        if self.confidence is not None:
            if isinstance(self.confidence, float) or not isinstance(self.confidence, Decimal):
                raise TypeError("confidence must be an exact Decimal or None")
            if not self.confidence.is_finite() or not Decimal(0) <= self.confidence <= Decimal(1):
                raise ValueError("confidence must be between zero and one")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("an agent suggestion needs a reason")


def from_exact_tag(
    *,
    candidate_id: UUID,
    tag_candidate_id: UUID,
    tag_text: str,
    permitted_types: frozenset[SemanticType],
    shared_dimension_line: bool,
    tag_is_vector_text: bool,
) -> SemanticTypingDecision:
    """Qualify an exact vocabulary tag only after all drawing evidence is present.

    No synonym or case-folding occurs here.  An unreadable, provisional, OCR-only, or geometrically
    unbound tag is not close enough to decide what an operand means.
    """

    if not isinstance(tag_text, str):
        raise TypeError("tag_text must be a string")
    if not isinstance(permitted_types, frozenset) or any(
        not isinstance(item, SemanticType) for item in permitted_types
    ):
        raise TypeError("permitted_types must be a frozenset of SemanticType values")
    if not isinstance(shared_dimension_line, bool) or not isinstance(tag_is_vector_text, bool):
        raise TypeError("tag proof flags must be booleans")

    try:
        semantic_type = SemanticType(tag_text.strip())
    except ValueError:
        return SemanticTypingDecision(
            candidate_id=candidate_id,
            semantic_type=None,
            method=TypingMethod.MECHANICAL_TAG,
            disposition=TypingDisposition.REVIEW_REQUIRED,
            reason="the drawing tag is not an exact semantic vocabulary value",
            tag_candidate_id=tag_candidate_id,
        )
    if semantic_type not in permitted_types:
        return SemanticTypingDecision(
            candidate_id=candidate_id,
            semantic_type=semantic_type,
            method=TypingMethod.MECHANICAL_TAG,
            disposition=TypingDisposition.REVIEW_REQUIRED,
            reason="the exact tag is not approved for this drawing layout",
            tag_candidate_id=tag_candidate_id,
        )
    if not tag_is_vector_text:
        return SemanticTypingDecision(
            candidate_id=candidate_id,
            semantic_type=semantic_type,
            method=TypingMethod.MECHANICAL_TAG,
            disposition=TypingDisposition.REVIEW_REQUIRED,
            reason="the tag was not directly extracted from vector text",
            tag_candidate_id=tag_candidate_id,
        )
    if not shared_dimension_line:
        return SemanticTypingDecision(
            candidate_id=candidate_id,
            semantic_type=semantic_type,
            method=TypingMethod.MECHANICAL_TAG,
            disposition=TypingDisposition.REVIEW_REQUIRED,
            reason="the tag and reading are not both bound to one resolved dimension line",
            tag_candidate_id=tag_candidate_id,
        )
    return SemanticTypingDecision(
        candidate_id=candidate_id,
        semantic_type=semantic_type,
        method=TypingMethod.MECHANICAL_TAG,
        disposition=TypingDisposition.QUALIFIED,
        reason="exact approved vector tag and reading share one resolved dimension line",
        tag_candidate_id=tag_candidate_id,
        confidence=Decimal(1),
    )


def from_agent_suggestion(
    suggestion: AgentTypeSuggestion,
    *,
    permitted_types: frozenset[SemanticType],
) -> SemanticTypingDecision:
    """Return an agent label as reviewer work, never qualified evidence."""

    if not isinstance(suggestion, AgentTypeSuggestion):
        raise TypeError("suggestion must be an AgentTypeSuggestion")
    if not isinstance(permitted_types, frozenset) or any(
        not isinstance(item, SemanticType) for item in permitted_types
    ):
        raise TypeError("permitted_types must be a frozenset of SemanticType values")
    if suggestion.semantic_type is not None and suggestion.semantic_type not in permitted_types:
        return SemanticTypingDecision(
            candidate_id=suggestion.candidate_id,
            semantic_type=None,
            method=TypingMethod.AGENT,
            disposition=TypingDisposition.REVIEW_REQUIRED,
            reason="the agent suggested a type outside this layout's approved vocabulary",
            confidence=suggestion.confidence,
        )
    return SemanticTypingDecision(
        candidate_id=suggestion.candidate_id,
        semantic_type=suggestion.semantic_type,
        method=TypingMethod.AGENT,
        disposition=TypingDisposition.REVIEW_REQUIRED,
        reason="agent suggestions require reviewer confirmation before a verdict operand exists",
        confidence=suggestion.confidence,
    )
