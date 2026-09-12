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

__all__ = ["NOTHING_HAS_RUN", "ChatMode", "ChatReply", "answer_question"]


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


#: What the reviewer is told when nothing has been checked yet. Deliberately not phrased as an
#: answer to their question, because there is no run to answer it about.
NOTHING_HAS_RUN = (
    "No checks have been run on this package yet, so there is nothing to explain. "
    "Open Measure, fill in the values the rulebook asks for, and press Run checks."
)


def _intro(
    question: str,
    selected: Sequence[ComposerFinding],
    total: int,
    *,
    checks_have_run: bool = True,
) -> str:
    """A deterministic envelope around model prose, with no new facts to get wrong."""
    # **A package nobody has checked is not a run with no failures.** Both arrive here with an empty
    # sequence, and answering them the same way told a reviewer "No FAIL findings in this run"
    # about a package that had been uploaded four minutes earlier and never checked — which reads
    # as a clean bill of health for a drawing nothing has looked at. That is the most expensive
    # sentence this module could say.
    if not checks_have_run:
        return NOTHING_HAS_RUN

    # The question stays out of this deterministic envelope. It does now reach the provider, so that
    # an answer can be about what was asked — but it is never echoed into text this module composes,
    # because that text is shown whether or not a model ran.
    outcome = _selection_label(question)
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
    *,
    checks_have_run: bool = True,
) -> ChatReply:
    """Return guarded language over one run's facts, or the complete structured fallback.

    ``compose_findings`` supplies the hard 1:1 and fact-preservation checks.  In particular, a
    provider cannot add a PASS/FAIL phrase, alter a number, omit a supplied fact, or emit a finding
    key that was not in the deterministic query.  Any failure falls back to the plain summaries.

    ``checks_have_run`` is the caller's answer to a question this module cannot see: an empty
    ``findings`` means "the checks found nothing" *or* "nobody has run them", and the two must not
    be answered the same way. It defaults to ``True`` so that a caller holding real findings need
    not state the obvious; a caller with none has to have looked.
    """
    if not checks_have_run:
        return ChatReply(
            text=_intro(question, (), len(findings), checks_have_run=False),
            narratives=(),
            mode=ChatMode.STRUCTURED_FALLBACK,
            model_id=None,
            fallback_reason="no checks have been run on this package",
        )

    selected = _selection(question, findings)
    if not selected:
        return ChatReply(
            text=_intro(question, selected, len(findings)),
            narratives=(),
            mode=ChatMode.STRUCTURED_FALLBACK,
            model_id=None,
            fallback_reason="no finding matched the deterministic outcome filter",
        )
    # The reviewer's own words reach the provider from here. They are untrusted — see
    # `FindingsLanguageModel` for why the guards, not the prompt, are what makes that safe.
    result = compose_findings(selected, model, question=question)
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
