"""Post-verdict language for findings, with deterministic facts still in charge.

The verdict engine has already finished before anything in this module runs.  A language model may
rewrite the recorded facts for a reviewer, but its reply is accepted only when it has exactly one
item for every finding, repeats the deterministic verdict, and preserves every numeric token.  A
failed call or a failed guard returns the deterministic summary instead; neither can delay or alter
the verdict.

This module deliberately contains no model SDK.  ``FindingsLanguageModel`` is the narrow transport
seam; the configured provider lives in ``workflow/findings_bedrock.py`` and tests supply a fake.
Nothing under ``verdict/`` imports either module.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from vocabulary.semantic_types import SemanticType

__all__ = [
    "ComposerFinding",
    "ComposerOperand",
    "CompositionMode",
    "CompositionResult",
    "FindingsLanguageModel",
    "ModelComposition",
    "NarrationFact",
    "NarrationOperand",
    "NarrativeBatch",
    "NarrativeGuardError",
    "ProposedExplanation",
    "ProposedNarrative",
    "bedrock_narrative_tool_schema",
    "bedrock_output_token_limit",
    "compose_findings",
    "deterministic_summary",
    "ground_explanations",
    "narration_overview_context",
]


# Presentation labels only. The engine outcome remains the immutable stored value; this closed map
# prevents database-style statuses such as ``NOT_FOUND`` from becoming the reviewer headline.
_OUTCOME_LABELS = {
    "PASS": "Looks right",
    "FAIL": "Needs correction",
    "REVIEW_REQUIRED": "Needs your decision",
    "NOT_FOUND": "Waiting on a value",
    "NO_APPLICABLE_RULE": "Not applicable",
}
_FIELD_LABELS = {
    SemanticType.WALL_CONFIG.value: "wall layout",
    "filler_symmetry": "whether the fillers should be symmetrical",
    "front_offset": "sink front offset",
    "countertop_depth": "countertop depth",
    "cabinet_side_thickness": "cabinet side thickness",
    "sink_interior_depth": "sink interior depth",
    "sink_interior_width": "sink interior width",
    "back_offset_minimum": "minimum back offset",
}


def reviewer_outcome(outcome: str) -> str:
    return _OUTCOME_LABELS.get(outcome, outcome.replace("_", " ").title())


def _field_label(value: str) -> str:
    return _FIELD_LABELS.get(value, value.replace("_", " "))


def reviewer_reason(reason: str, outcome: str) -> str:
    """Turn a known deterministic abstention into its equally deterministic reviewer action."""
    missing = re.search(r"(?:needs|required value) '([^']+)'", reason, flags=re.IGNORECASE)
    if missing:
        field_key = missing.group(1)
        field = _field_label(field_key)
        if outcome == "REVIEW_REQUIRED":
            if field_key == "filler_symmetry":
                return "Please confirm whether the fillers should be symmetrical so I can use the right version of this check."
            return f"Please confirm the {field} so I can use the right version of this check."
        return (
            f"I can’t check this yet because the {field} is missing. Please enter it to continue."
        )
    if "tolerance" in reason.casefold() and "supplied" in reason.casefold():
        return "I need the agreed tolerance before this can be marked right or wrong."
    if "derivation" in reason.casefold() or "final operation" in reason.casefold():
        quoted = re.search(r"required value '([^']+)'", reason, flags=re.IGNORECASE)
        if quoted:
            field = _field_label(quoted.group(1))
            return f"I can’t check this yet because the {field} is missing. Please enter it to continue."
        return "I can’t check this yet because a required drawing value is missing. Please enter it to continue."
    return reason


# Amazon Nova's Bedrock tool schema supports a deliberately small JSON Schema subset: at the top
# level it accepts ``type``, ``properties`` and ``required``.  Pydantic's normal JSON Schema adds
# ``$defs``, ``$ref``, titles and ``additionalProperties``; those are excellent for local validation
# but can make Nova produce a malformed tool-use sequence.  Keep the provider schema small, then
# validate the returned payload with ``NarrativeBatch`` below, which remains strict.
def bedrock_narrative_tool_schema() -> dict[str, object]:
    """Return Nova-compatible structured-output schema for a narration batch.

    A fresh dictionary prevents a transport or test from mutating the schema used by another
    request.  This schema constrains shape only; local Pydantic validation and the fidelity guard
    still enforce non-empty values, one-to-one findings and exact deterministic facts.
    """
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "finding_key": {"type": "string"},
                        "explanation": {"type": "string"},
                    },
                    "required": ["finding_key", "explanation"],
                },
            },
        },
        "required": ["summary", "findings"],
    }


def bedrock_output_token_limit(finding_count: int) -> int:
    """Reserve enough output room for the fact-preservation contract, within a hard spend bound.

    The guard requires one complete factual narration per finding.  A fixed 1024-token cap works
    for a one-row question but truncates a nine-finding response while Nova is emitting a forced
    tool input, which Bedrock reports as malformed tool use.  ``maxTokens`` is a ceiling rather than
    a charge; 4096 is the bounded ceiling and gives a typical whole run room to finish.
    """
    if finding_count < 1:
        raise ValueError("finding_count must be positive")
    return min(4096, max(1024, finding_count * 512))


def narration_overview_context(findings: Sequence[ComposerFinding]) -> dict[str, object]:
    """Give the model deterministic aggregate facts it must make useful in its overview."""
    counts: dict[str, int] = {}
    reviewer_decisions: list[dict[str, str]] = []
    unresolved_inputs: set[str] = set()
    for finding in findings:
        counts[finding.outcome] = counts.get(finding.outcome, 0) + 1
        if finding.outcome == "REVIEW_REQUIRED":
            reviewer_decisions.append({"check": finding.check_name, "reason": finding.reason})
        unresolved_inputs.update(
            _field_label(value)
            for value in re.findall(
                r"(?:needs|required value) '([^']+)'", finding.reason, flags=re.IGNORECASE
            )
        )
        unresolved_inputs.update(
            re.findall(r"because the (.+?) is missing", finding.reason, flags=re.IGNORECASE)
        )
    return {
        "selected_finding_count": len(findings),
        "outcome_counts": dict(sorted(counts.items())),
        "check_ids": [finding.check for finding in findings],
        "reviewer_decisions": reviewer_decisions,
        "unresolved_inputs": sorted(unresolved_inputs),
    }


_DIGIT_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_])[-+]?(?:\d+/\d+|\d+(?:,\d{3})*(?:\.\d+)?)(?![A-Za-z0-9_])"
)
_OUTCOME_PHRASES = frozenset({"PASS", "FAIL", "REVIEW REQUIRED", "NOT FOUND", "NO APPLICABLE RULE"})
_NUMBER_WORDS = frozenset(
    {
        "zero",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "thirteen",
        "fourteen",
        "fifteen",
        "sixteen",
        "seventeen",
        "eighteen",
        "nineteen",
        "twenty",
        "thirty",
        "forty",
        "fifty",
        "sixty",
        "seventy",
        "eighty",
        "ninety",
        "hundred",
        "thousand",
        "million",
    }
)
_NUMBER_WORD_DIGITS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
    "nineteen": "19",
    "twenty": "20",
    "thirty": "30",
    "forty": "40",
    "fifty": "50",
    "sixty": "60",
    "seventy": "70",
    "eighty": "80",
    "ninety": "90",
    "hundred": "100",
    "thousand": "1000",
    "million": "1000000",
}


@dataclass(frozen=True, slots=True)
class ComposerOperand:
    """One exact stored operand exposed to the narration layer."""

    name: str
    value: str
    source: str
    evidence_page: str | None = None

    def as_data(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "value": self.value,
            "source": self.source,
            "evidence_page": self.evidence_page,
        }


@dataclass(frozen=True, slots=True)
class ComposerFinding:
    """The reviewer-relevant subset of one immutable deterministic finding.

    Snapshot hashes, database ids and engine versions are intentionally absent.  They remain in the
    workbook's structured columns, while exposing them to the composer would force prose to repeat
    numeric hash fragments that do not explain the check.  Every number that *is* exposed is guarded.
    """

    key: str
    check: str
    check_name: str
    outcome: str
    severity: str
    reason: str
    comparison: str | None
    difference: str | None
    tolerance: str | None
    arithmetic_unit: str | None
    operands: tuple[ComposerOperand, ...]
    evidence_pages: tuple[str, ...]
    notes: tuple[str, ...]

    def as_data(self) -> dict[str, object]:
        return {
            "finding_key": self.key,
            "check": self.check,
            "check_name": self.check_name,
            "deterministic_outcome": self.outcome,
            "severity": self.severity,
            "reason": self.reason,
            "comparison": self.comparison,
            "difference": self.difference,
            "tolerance": self.tolerance,
            "arithmetic_unit": self.arithmetic_unit,
            "operands": [operand.as_data() for operand in self.operands],
            "evidence_pages": list(self.evidence_pages),
            "notes": list(self.notes),
        }

    def guarded_text(self) -> str:
        """Only facts whose numeric tokens must survive in the prose."""
        data = self.as_data()
        data.pop("finding_key")
        return json.dumps(data, sort_keys=True, separators=(",", ":"))


class NarrationOperand(BaseModel):
    """The validated operand shape exposed to the narration provider."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    value: str
    source: str
    evidence_page: str | None


class NarrationFact(BaseModel):
    """One immutable finding payload a language-only adapter may receive.

    The provider gets the deterministic summary as ``required_text`` rather than reconstructing
    facts from individual fields.  Both Bedrock adapters serialize this one strict contract so chat
    and output narration cannot drift in what they consider grounded data.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    finding_key: str
    check: str
    check_name: str
    deterministic_outcome: str
    severity: str
    reason: str
    comparison: str | None
    difference: str | None
    tolerance: str | None
    arithmetic_unit: str | None
    operands: tuple[NarrationOperand, ...]
    evidence_pages: tuple[str, ...]
    notes: tuple[str, ...]
    required_text: str

    @classmethod
    def from_finding(cls, finding: ComposerFinding) -> NarrationFact:
        """Validate the exact provider facts for one stored deterministic finding."""
        return cls(
            finding_key=finding.key,
            check=finding.check,
            check_name=finding.check_name,
            deterministic_outcome=finding.outcome,
            severity=finding.severity,
            reason=finding.reason,
            comparison=finding.comparison,
            difference=finding.difference,
            tolerance=finding.tolerance,
            arithmetic_unit=finding.arithmetic_unit,
            operands=tuple(
                NarrationOperand(
                    name=operand.name,
                    value=operand.value,
                    source=operand.source,
                    evidence_page=operand.evidence_page,
                )
                for operand in finding.operands
            ),
            evidence_pages=finding.evidence_pages,
            notes=finding.notes,
            required_text=deterministic_summary(finding),
        )


class ProposedExplanation(BaseModel):
    """One provider proposal: the plain-language half, and nothing else.

    **The model is no longer asked to reproduce the facts.** It used to receive the complete
    deterministic summary as ``required_text`` and was told to copy it character-for-character
    before appending a sentence; the guard then checked that every fact had survived. Transcription
    is not a thing to ask a language model for, and Nova Lite proved it — it dropped the clause
    ``Recorded comparison: 101/4 in != 51/2 in`` from an otherwise sound explanation, so the guard
    rejected the whole batch and every reviewer saw the plain fallback instead of any narration at
    all.

    Now ``deterministic_summary`` is prepended in code, where the string already exists and cannot
    be mistyped, and the provider supplies only the sentences it is actually good at. Every
    protective check in ``_guard_one`` still runs on the composed text: an invented number, an
    invented verdict word, or a spelled-out number still fails the batch. What stopped being
    checkable is a transcription error that can no longer happen.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    finding_key: str = Field(min_length=1)
    explanation: str = Field(min_length=1, max_length=600)

    @field_validator("finding_key", "explanation")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class ProposedNarrative(BaseModel):
    """One composed narrative — deterministic facts plus the provider's explanation.

    Built by ``ground_explanations``; never accepted directly from a provider any more."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    finding_key: str = Field(min_length=1)
    text: str = Field(min_length=1)

    @field_validator("finding_key", "text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class NarrativeBatch(BaseModel):
    """The one forced-tool payload accepted from the model."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # A short reviewer-ready overview rendered above the immutable findings.  It is separately
    # guarded so every stated number, outcome, and check identifier is backed by this run.
    summary: str = Field(default="", max_length=600)
    # JSON has arrays, never tuples.  The transient validated payload may use a list; it is converted
    # to the immutable ``ModelComposition`` tuple immediately after validation.
    findings: list[ProposedExplanation]


@dataclass(frozen=True, slots=True)
class ModelComposition:
    """Validated provider output plus the configuration identity that produced it."""

    narratives: tuple[ProposedNarrative, ...]
    model_id: str
    prompt_id: str
    template_id: str
    summary: str | None = None


class FindingsLanguageModel(Protocol):
    """The only model capability reachable from output generation."""

    def compose(self, findings: Sequence[ComposerFinding]) -> ModelComposition:
        """Propose one prose rendering for each supplied deterministic finding."""


class CompositionMode(StrEnum):
    LLM = "llm"
    FALLBACK = "structured_fallback"


@dataclass(frozen=True, slots=True)
class CompositionResult:
    """Prose safe to publish and enough metadata for the persisted stage result."""

    narratives: tuple[ProposedNarrative, ...]
    mode: CompositionMode
    model_id: str | None = None
    prompt_id: str | None = None
    template_id: str | None = None
    fallback_reason: str | None = None
    summary: str | None = None


class NarrativeGuardError(ValueError):
    """A model batch was well-formed JSON but did not preserve deterministic facts."""


def _source_label(source: str) -> str:
    folded = source.strip().upper()
    if folded == "ARCH":
        return "approved"
    if folded == "SHOP":
        return "vendor"
    return source or "unspecified source"


def _human_label(value: str) -> str:
    """Make an immutable machine key readable without changing the fact it identifies."""
    return value.replace("_", " ")


def deterministic_summary(finding: ComposerFinding) -> str:
    """A complete, reviewer-readable fallback used whenever the model cannot be reached.

    This is assembled only from persisted engine facts.  In particular, it never derives a number
    from the comparison string: every displayed value comes straight from ``ComposerFinding``.
    """
    parts = [
        f"{finding.check_name} — {reviewer_outcome(finding.outcome)} ({finding.check}).",
        reviewer_reason(finding.reason, finding.outcome),
    ]
    if finding.operands:
        rendered = " ".join(
            f"{_human_label(operand.name).capitalize()} from {_source_label(operand.source)}: {operand.value}."
            + (
                f" Evidence page: {operand.evidence_page}."
                if operand.evidence_page is not None
                else ""
            )
            for operand in finding.operands
        )
        parts.append(rendered)
    if finding.comparison is not None:
        parts.append(f"Recorded comparison: {finding.comparison}.")
    if finding.difference is not None:
        parts.append(f"Difference: {finding.difference}.")
    if finding.tolerance is not None:
        parts.append(f"Tolerance: {finding.tolerance}.")
    if finding.arithmetic_unit is not None:
        parts.append(f"Unit: {finding.arithmetic_unit}.")
    if finding.evidence_pages:
        parts.append(f"Evidence pages: {', '.join(finding.evidence_pages)}.")
    if finding.notes:
        parts.append(f"Notes: {' | '.join(finding.notes)}.")
    if finding.outcome == "PASS":
        parts.append("Next: no action is needed.")
    elif finding.outcome == "FAIL":
        parts.append("Next: review the vendor drawing against the approved design.")
    elif finding.outcome == "REVIEW_REQUIRED":
        parts.append("Next: make the requested review decision.")
    elif finding.outcome == "NOT_FOUND":
        parts.append("Next: provide the missing value, then run the check again.")
    return " ".join(parts)


def ground_explanations(
    findings: Sequence[ComposerFinding], proposals: Sequence[ProposedExplanation]
) -> tuple[ProposedNarrative, ...]:
    """Put each provider explanation behind the deterministic summary of its own finding.

    This is the step that makes fact preservation structural instead of hopeful. The facts come
    from ``deterministic_summary``, which reads them out of the persisted ``ComposerFinding``; the
    provider contributes only what follows. A model can no longer omit a fact, because it was never
    holding one.

    A proposal naming a finding this run did not select is dropped here rather than concatenated
    onto a mismatched summary — ``_guard_batch`` is what reports it, as an unbacked key, and it must
    see the same set of keys the provider actually sent. A finding with no proposal is likewise left
    out so the same check can report it as missing, rather than silently arriving with a
    deterministic summary and no explanation, which would look like a narrated answer and be a
    fallback wearing its clothes.
    """
    by_key = {finding.key: finding for finding in findings}
    grounded: list[ProposedNarrative] = []
    for proposal in proposals:
        finding = by_key.get(proposal.finding_key)
        if finding is None:
            # Kept, so `_guard_batch` reports it as unbacked and the whole batch falls back.
            grounded.append(
                ProposedNarrative(finding_key=proposal.finding_key, text=proposal.explanation)
            )
            continue
        grounded.append(
            ProposedNarrative(
                finding_key=proposal.finding_key,
                text=f"{deterministic_summary(finding)} {proposal.explanation.strip()}".strip(),
            )
        )
    return tuple(grounded)


def _digit_numbers(text: str) -> frozenset[str]:
    return frozenset(match.group(0) for match in _DIGIT_NUMBER.finditer(text))


def _number_words(text: str) -> frozenset[str]:
    words = re.findall(r"[a-z]+", text.casefold().replace("-", " "))
    return frozenset(word for word in words if word in _NUMBER_WORDS)


def _outcomes(text: str) -> frozenset[str]:
    folded = re.sub(r"[_-]+", " ", text.upper())
    outcomes = {
        outcome
        for outcome in _OUTCOME_PHRASES
        if re.search(rf"(?<![A-Z]){re.escape(outcome)}(?![A-Z])", folded)
    }
    # Reviewer-facing labels are verdict claims too; a model must not turn a FAIL into "Looks
    # right" merely by avoiding the database enum spelling.
    for outcome, label in _OUTCOME_LABELS.items():
        if re.search(rf"(?<![A-Z]){re.escape(label.upper())}(?![A-Z])", folded):
            outcomes.add(outcome.replace("_", " "))
    return frozenset(outcomes)


def _guard_one(finding: ComposerFinding, text: str) -> None:
    required_prefix = (
        f"{finding.check_name} — {reviewer_outcome(finding.outcome)} ({finding.check})."
    )
    if not text.startswith(required_prefix):
        raise NarrativeGuardError(
            f"{finding.key}: prose must start with the reviewer-facing check name and deterministic outcome"
        )

    # The model receives the reviewer-facing translation of known engine reasons.  Requiring the
    # raw reason here would force it to expose an internal derivation or field key, defeating the
    # presentation boundary while adding no factual protection.
    required_fragments = [
        finding.check_name,
        reviewer_outcome(finding.outcome),
        reviewer_reason(finding.reason, finding.outcome),
    ]
    required_fragments.extend(
        value
        for value in (
            finding.comparison,
            finding.difference,
            finding.tolerance,
            finding.arithmetic_unit,
        )
        if value is not None
    )
    required_fragments.extend(finding.notes)
    for operand in finding.operands:
        required_fragments.extend(
            (_human_label(operand.name), operand.value, _source_label(operand.source))
        )
    # Names are presentation text, so title casing must not turn an otherwise faithful narrative
    # into a fallback (``Countertop depth`` and ``countertop depth`` name the same supplied check).
    # Numeric and verdict preservation remain exact checks below.
    folded_text = text.casefold()
    missing_fragments = sorted(
        {
            fragment
            for fragment in required_fragments
            if fragment and fragment.casefold() not in folded_text
        }
    )
    if missing_fragments:
        raise NarrativeGuardError(
            f"{finding.key}: prose omitted deterministic fact(s) {missing_fragments}"
        )

    invented_outcomes = sorted(_outcomes(text) - _outcomes(finding.guarded_text()))
    if invented_outcomes:
        raise NarrativeGuardError(f"{finding.key}: prose introduced verdict(s) {invented_outcomes}")

    fact_text = finding.guarded_text()
    permitted_digits = _digit_numbers(fact_text)
    prose_digits = _digit_numbers(text)
    invented_digits = sorted(prose_digits - permitted_digits)
    missing_digits = sorted(permitted_digits - prose_digits)
    if invented_digits:
        raise NarrativeGuardError(
            f"{finding.key}: prose introduced numeric token(s) {invented_digits}"
        )
    if missing_digits:
        raise NarrativeGuardError(f"{finding.key}: prose omitted numeric token(s) {missing_digits}")

    # Models sometimes spell a new number to evade a digit-only comparison ("four" for ``3``).
    # Such prose is not accepted unless that exact word already occurred in the deterministic facts.
    invented_words = sorted(_number_words(text) - _number_words(fact_text))
    if invented_words:
        raise NarrativeGuardError(
            f"{finding.key}: prose introduced number word(s) {invented_words}"
        )


def _guard_batch(
    findings: Sequence[ComposerFinding], composition: ModelComposition
) -> tuple[ProposedNarrative, ...]:
    expected = {finding.key: finding for finding in findings}
    proposed: dict[str, ProposedNarrative] = {}
    for narrative in composition.narratives:
        if narrative.finding_key in proposed:
            raise NarrativeGuardError(f"duplicate composed finding {narrative.finding_key!r}")
        proposed[narrative.finding_key] = narrative

    missing = sorted(expected.keys() - proposed.keys())
    unbacked = sorted(proposed.keys() - expected.keys())
    if missing or unbacked:
        raise NarrativeGuardError(
            f"composition is not 1:1; missing={missing!r}, unbacked={unbacked!r}"
        )

    for key, narrative in proposed.items():
        _guard_one(expected[key], narrative.text)
    return tuple(proposed[finding.key] for finding in findings)


def _guard_overview(findings: Sequence[ComposerFinding], summary: str | None) -> str | None:
    """Accept an overview only when every number, outcome and check id is run-backed.

    The overview may now name the reviewer-relevant blockers and exact values, because that is the
    useful part of narration.  The guard still rejects any new number, outcome or check identifier;
    cards below remain the full one-to-one deterministic audit record.
    """
    if summary is None or not summary.strip():
        return None
    text = summary.strip()
    if len(text) > 600:
        raise NarrativeGuardError("AI overview is longer than 600 characters")
    context = narration_overview_context(findings)
    permitted_digits = set().union(
        *(_digit_numbers(finding.guarded_text()) for finding in findings),
        _digit_numbers(str(context["selected_finding_count"])),
        *(_digit_numbers(str(count)) for count in context["outcome_counts"].values()),
    )
    invented_digits = sorted(_digit_numbers(text) - permitted_digits)
    if invented_digits:
        raise NarrativeGuardError(f"AI overview introduced numeric claim(s) {invented_digits}")
    aggregate_numbers = {
        str(context["selected_finding_count"]),
        *(str(count) for count in context["outcome_counts"].values()),
    }
    # Nova occasionally spells the aggregate counts despite the prompt's digit-only request.
    # Accept that benign surface variation only when the word maps to an exact supplied aggregate;
    # a page number or dimension must not let it claim a different number of findings.
    permitted_number_words = set().union(
        *(_number_words(finding.guarded_text()) for finding in findings),
        {word for word, number in _NUMBER_WORD_DIGITS.items() if number in aggregate_numbers},
    )
    invented_number_words = sorted(_number_words(text) - permitted_number_words)
    if invented_number_words:
        raise NarrativeGuardError(f"AI overview introduced number word(s) {invented_number_words}")
    # Outcomes are stored with underscores (``NOT_FOUND``) but displayed with spaces.  Compare
    # through the same normalizer used for prose rather than rejecting the reviewer-facing form.
    permitted_outcomes = set().union(*(_outcomes(finding.outcome) for finding in findings))
    invented_outcomes = sorted(_outcomes(text) - permitted_outcomes)
    if invented_outcomes:
        raise NarrativeGuardError(f"AI overview introduced outcome claim(s) {invented_outcomes}")
    mentioned_check_ids = set(re.findall(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b", text))
    if mentioned_check_ids:
        # Individual claim/outcome pairs belong on the one-to-one cards, not an aggregate summary.
        # Refusing raw IDs here avoids accepting a swapped outcome relation in prose.
        raise NarrativeGuardError("AI overview must not name individual rule IDs")
    return text


def _fallback(findings: Sequence[ComposerFinding], *, reason: str) -> CompositionResult:
    return CompositionResult(
        narratives=tuple(
            ProposedNarrative(finding_key=finding.key, text=deterministic_summary(finding))
            for finding in findings
        ),
        mode=CompositionMode.FALLBACK,
        fallback_reason=reason,
    )


def compose_findings(
    findings: Sequence[ComposerFinding], model: FindingsLanguageModel | None
) -> CompositionResult:
    """Accept a faithful 1:1 model rewrite or fail closed to deterministic prose.

    The whole batch falls back together.  Mixing accepted model prose with fallback rows after a
    missing or invalid item would make the exported report look complete while hiding that the model
    dropped a finding.
    """
    keys = [finding.key for finding in findings]
    if len(keys) != len(set(keys)):
        raise ValueError("deterministic finding keys must be unique")
    if model is None:
        return _fallback(findings, reason="no findings language model is configured")
    try:
        proposed = model.compose(findings)
        narratives = _guard_batch(findings, proposed)
        summary = _guard_overview(findings, proposed.summary)
    # A provider can fail through its SDK, transport, protocol parser or local schema.  The contract
    # is deliberately broader than any one SDK's exception tree: *every* such failure must preserve
    # the already-recorded verdict and yield the structured fallback.
    except Exception as error:  # noqa: BLE001 - fail closed across the provider boundary
        reason = f"{type(error).__name__}: {error}"[:500]
        return _fallback(findings, reason=reason)
    return CompositionResult(
        narratives=narratives,
        mode=CompositionMode.LLM,
        model_id=proposed.model_id,
        prompt_id=proposed.prompt_id,
        template_id=proposed.template_id,
        summary=summary,
    )
