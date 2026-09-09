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

__all__ = [
    "ComposerFinding",
    "ComposerOperand",
    "CompositionMode",
    "CompositionResult",
    "FindingsLanguageModel",
    "ModelComposition",
    "NarrativeBatch",
    "NarrativeGuardError",
    "ProposedNarrative",
    "compose_findings",
    "deterministic_summary",
]


_DIGIT_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_])[-+]?(?:\d+/\d+|\d+(?:,\d{3})*(?:\.\d+)?)(?![A-Za-z0-9_])"
)
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


class ProposedNarrative(BaseModel):
    """One model proposal.  Unknown fields reject instead of disappearing."""

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

    # JSON has arrays, never tuples.  The transient validated payload may use a list; it is converted
    # to the immutable ``ModelComposition`` tuple immediately after validation.
    findings: list[ProposedNarrative]


@dataclass(frozen=True, slots=True)
class ModelComposition:
    """Validated provider output plus the configuration identity that produced it."""

    narratives: tuple[ProposedNarrative, ...]
    model_id: str
    prompt_id: str
    template_id: str


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


class NarrativeGuardError(ValueError):
    """A model batch was well-formed JSON but did not preserve deterministic facts."""


def _source_label(source: str) -> str:
    folded = source.strip().upper()
    if folded == "ARCH":
        return "approved"
    if folded == "SHOP":
        return "vendor"
    return source or "unspecified source"


def deterministic_summary(finding: ComposerFinding) -> str:
    """A complete plain rendering used whenever the model cannot be trusted or reached."""
    parts = [
        f"{finding.check}: {finding.outcome}.",
        f"Checked {finding.check_name}.",
        f"Severity: {finding.severity}.",
        f"Why: {finding.reason}",
    ]
    if finding.operands:
        rendered = "; ".join(
            f"{operand.name} ({_source_label(operand.source)}, source {operand.source}) = "
            f"{operand.value}"
            + (
                f" on evidence page {operand.evidence_page}"
                if operand.evidence_page is not None
                else ""
            )
            for operand in finding.operands
        )
        parts.append(f"Values: {rendered}.")
    if finding.comparison is not None:
        parts.append(f"Comparison: {finding.comparison}.")
    if finding.difference is not None:
        parts.append(f"Difference: {finding.difference}.")
    if finding.tolerance is not None:
        parts.append(f"Tolerance: {finding.tolerance}.")
    if finding.arithmetic_unit is not None:
        parts.append(f"Arithmetic unit: {finding.arithmetic_unit}.")
    if finding.evidence_pages:
        parts.append(f"Evidence pages: {', '.join(finding.evidence_pages)}.")
    if finding.notes:
        parts.append(f"Notes: {' | '.join(finding.notes)}.")
    return " ".join(parts)


def _digit_numbers(text: str) -> frozenset[str]:
    return frozenset(match.group(0) for match in _DIGIT_NUMBER.finditer(text))


def _number_words(text: str) -> frozenset[str]:
    words = re.findall(r"[a-z]+", text.casefold().replace("-", " "))
    return frozenset(word for word in words if word in _NUMBER_WORDS)


def _guard_one(finding: ComposerFinding, text: str) -> None:
    required_prefix = f"{finding.check}: {finding.outcome}."
    if not text.startswith(required_prefix):
        raise NarrativeGuardError(
            f"{finding.key}: prose must start with the exact deterministic check and outcome"
        )

    required_fragments = [finding.check_name, finding.severity, finding.reason]
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
        required_fragments.extend((operand.name, operand.value, operand.source))
        if operand.source.strip().upper() == "ARCH":
            required_fragments.append("approved")
        elif operand.source.strip().upper() == "SHOP":
            required_fragments.append("vendor")
    missing_fragments = sorted(
        {fragment for fragment in required_fragments if fragment and fragment not in text}
    )
    if missing_fragments:
        raise NarrativeGuardError(
            f"{finding.key}: prose omitted deterministic fact(s) {missing_fragments}"
        )

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
    )
