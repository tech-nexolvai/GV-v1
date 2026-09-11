"""A reviewer chat may narrate stored findings, never decide a review.

The chat boundary is intentionally built on the same ``ComposerFinding`` contract as the report
composer.  It receives facts from one already-complete deterministic run, returns one guarded
narrative for each finding it elects to show, and has no arithmetic or verdict capability.  The
question only chooses an already-recorded subset by deterministic keywords; it is never treated as
an instruction to the verdict engine or to the model.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from workflow.findings_composer import (
    ComposerFinding,
    FindingsLanguageModel,
    ProposedNarrative,
    compose_findings,
)

__all__ = ["ChatMode", "ChatReply", "answer_question"]


class ChatMode(StrEnum):
    """Whether the optional language provider contributed to this reply."""

    LLM = "llm"
    STRUCTURED_FALLBACK = "structured_fallback"


@dataclass(frozen=True, slots=True)
class ChatReply:
    """A publishable response and its immutable finding backing."""

    text: str
    narratives: tuple[ProposedNarrative, ...]
    mode: ChatMode
    # Published only when guarded provider prose was accepted. It is configuration identity, not a
    # prompt, response, drawing, or finding value, so a reviewer can tell whether narration ran.
    model_id: str | None = None
    fallback_reason: str | None = None
    # The optional language overview is rendered above the exact audit cards.  Its numbers,
    # outcomes, and check identifiers have already been checked against the deterministic run.
    summary: str | None = None


def _selection(question: str, findings: Sequence[ComposerFinding]) -> tuple[ComposerFinding, ...]:
    """Select a recorded outcome class without asking a model to filter the record.

    A chat request must never make a failure disappear because a language model thought it was
    irrelevant.  These are intentionally small, explainable filters; all other questions receive
    the complete run.  The answer content is still composed from the selected facts by the bounded
    language layer below.
    """
    folded = question.casefold()
    if "fail" in folded:
        return tuple(finding for finding in findings if finding.outcome == "FAIL")
    if "pass" in folded:
        return tuple(finding for finding in findings if finding.outcome == "PASS")
    if any(token in folded for token in ("review", "abstain", "missing", "not found")):
        return tuple(
            finding
            for finding in findings
            if finding.outcome in {"REVIEW_REQUIRED", "NOT_FOUND", "NO_APPLICABLE_RULE"}
        )
    return tuple(findings)


def _selection_label(question: str) -> str:
    """A short outcome label that matches the filter used by _selection."""
    folded = question.casefold()
    if "fail" in folded:
        return "FAIL"
    if "pass" in folded:
        return "PASS"
    if any(token in folded for token in ("review", "abstain", "missing", "not found")):
        return "REVIEW_REQUIRED or NOT_FOUND"
    return "all"


def _intro(question: str, selected: Sequence[ComposerFinding], total: int) -> str:
    """A deterministic envelope around model prose, with no new facts to get wrong."""
    # The question stays out of the public response unless the facts answer it.
    outcome = _selection_label(question)
    # We keep `question` unused after parsing to avoid echoing raw user text in system output.
    if not selected:
        if outcome == "all":
            return f"This run has no findings ({total} total)."
        return f"No {outcome} findings in this run ({total} total)."
    del question
    return (
        f"Showing {len(selected)} of {total} recorded finding"
        f"{'s' if total != 1 else ''}. The explanation below is grounded in the deterministic run."
    )


def answer_question(
    question: str,
    findings: Sequence[ComposerFinding],
    model: FindingsLanguageModel | None,
) -> ChatReply:
    """Return guarded language over one run's facts, or the complete structured fallback.

    ``compose_findings`` supplies the hard 1:1 and fact-preservation checks.  In particular, a
    provider cannot add a PASS/FAIL phrase, alter a number, omit a supplied fact, or emit a finding
    key that was not in the deterministic query.  Any failure falls back to the plain summaries.
    """
    selected = _selection(question, findings)
    if not selected:
        return ChatReply(
            text=_intro(question, selected, len(findings)),
            narratives=(),
            mode=ChatMode.STRUCTURED_FALLBACK,
            model_id=None,
            fallback_reason="no finding matched the deterministic outcome filter",
        )
    result = compose_findings(selected, model)
    return ChatReply(
        text=_intro(question, selected, len(findings)),
        narratives=result.narratives,
        mode=(
            ChatMode.LLM
            if result.mode.value == ChatMode.LLM.value
            else ChatMode.STRUCTURED_FALLBACK
        ),
        model_id=(result.model_id if result.mode.value == ChatMode.LLM.value else None),
        fallback_reason=result.fallback_reason,
        summary=result.summary,
    )
