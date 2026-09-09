"""The language layer may change phrasing and nothing else."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path

from workflow.findings_composer import (
    ComposerFinding,
    ComposerOperand,
    CompositionMode,
    ModelComposition,
    ProposedNarrative,
    compose_findings,
    deterministic_summary,
)


def _finding(*, key: str = "finding-a", outcome: str = "FAIL") -> ComposerFinding:
    return ComposerFinding(
        key=key,
        check="CT-DEPTH-001",
        check_name="Countertop depth",
        outcome=outcome,
        severity="FLAG",
        reason="The vendor depth differs from the approved depth by 1/2 in.",
        comparison="25 1/2 in vs 25 in",
        difference="1/2 in",
        tolerance="1/16 in",
        arithmetic_unit="in",
        operands=(
            ComposerOperand("approved_depth", "25 in", "ARCH", "3"),
            ComposerOperand("vendor_depth", "25 1/2 in", "SHOP", "3"),
        ),
        evidence_pages=("3",),
        notes=(),
    )


class _StaticModel:
    def __init__(self, narratives: Sequence[ProposedNarrative]) -> None:
        self._narratives = tuple(narratives)

    def compose(self, findings: Sequence[ComposerFinding]) -> ModelComposition:
        del findings
        return ModelComposition(
            narratives=self._narratives,
            model_id="configured-model",
            prompt_id="findings-composer-v1",
            template_id="deterministic-findings-v1",
        )


class _BrokenModel:
    def compose(self, findings: Sequence[ComposerFinding]) -> ModelComposition:
        del findings
        raise TimeoutError("provider unavailable")


def _faithful(finding: ComposerFinding) -> ProposedNarrative:
    deterministic = deterministic_summary(finding)
    prefix = f"{finding.check}: {finding.outcome}."
    return ProposedNarrative(
        finding_key=finding.key,
        text=deterministic.replace(prefix, f"{prefix} Reviewer summary:", 1),
    )


def test_a_faithful_model_rewrite_is_accepted_in_deterministic_order() -> None:
    first = _finding(key="finding-a")
    second = _finding(key="finding-b", outcome="PASS")
    # Provider order is deliberately reversed. Published order remains engine order.
    model = _StaticModel((_faithful(second), _faithful(first)))

    result = compose_findings((first, second), model)

    assert result.mode is CompositionMode.LLM
    assert result.model_id == "configured-model"
    assert [item.finding_key for item in result.narratives] == ["finding-a", "finding-b"]
    assert all("Reviewer summary:" in item.text for item in result.narratives)


def test_an_unbacked_or_missing_finding_rejects_the_whole_model_batch() -> None:
    first = _finding(key="finding-a")
    unbacked = _finding(key="not-a-real-finding")

    result = compose_findings((first,), _StaticModel((_faithful(unbacked),)))

    assert result.mode is CompositionMode.FALLBACK
    assert "not 1:1" in str(result.fallback_reason)
    assert [item.finding_key for item in result.narratives] == ["finding-a"]
    assert result.narratives[0].text == deterministic_summary(first)


def test_a_new_number_in_model_prose_falls_back() -> None:
    finding = _finding()
    proposed = _faithful(finding)
    changed = ProposedNarrative(
        finding_key=finding.key,
        text=f"{proposed.text} Review again on page 99.",
    )

    result = compose_findings((finding,), _StaticModel((changed,)))

    assert result.mode is CompositionMode.FALLBACK
    assert "introduced numeric" in str(result.fallback_reason)


def test_an_omitted_number_in_model_prose_falls_back() -> None:
    finding = _finding()
    proposed = _faithful(finding)
    changed = ProposedNarrative(
        finding_key=finding.key,
        text=proposed.text.replace("Tolerance: 1/16 in.", "Tolerance was recorded."),
    )

    result = compose_findings((finding,), _StaticModel((changed,)))

    assert result.mode is CompositionMode.FALLBACK
    assert "omitted deterministic fact" in str(result.fallback_reason)


def test_a_non_numeric_reason_cannot_be_dropped() -> None:
    finding = _finding()
    proposed = _faithful(finding)
    changed = ProposedNarrative(
        finding_key=finding.key,
        text=proposed.text.replace(f"Why: {finding.reason}", "Why: check the drawing."),
    )

    result = compose_findings((finding,), _StaticModel((changed,)))

    assert result.mode is CompositionMode.FALLBACK
    assert "omitted deterministic fact" in str(result.fallback_reason)


def test_a_rejudged_verdict_in_model_prose_falls_back() -> None:
    finding = _finding()
    proposed = _faithful(finding)
    changed = ProposedNarrative(
        finding_key=finding.key,
        text=proposed.text.replace("CT-DEPTH-001: FAIL.", "CT-DEPTH-001: PASS.", 1),
    )

    result = compose_findings((finding,), _StaticModel((changed,)))

    assert result.mode is CompositionMode.FALLBACK
    assert "exact deterministic check and outcome" in str(result.fallback_reason)


def test_a_second_conflicting_verdict_cannot_hide_behind_the_correct_prefix() -> None:
    finding = _finding()
    proposed = _faithful(finding)
    changed = ProposedNarrative(
        finding_key=finding.key,
        text=f"{proposed.text} The reviewer should PASS it.",
    )

    result = compose_findings((finding,), _StaticModel((changed,)))

    assert result.mode is CompositionMode.FALLBACK
    assert "introduced verdict" in str(result.fallback_reason)


def test_a_spelled_out_invented_number_cannot_evade_the_guard() -> None:
    finding = _finding()
    proposed = _faithful(finding)
    changed = ProposedNarrative(
        finding_key=finding.key,
        text=f"{proposed.text} Four readings were considered.",
    )

    result = compose_findings((finding,), _StaticModel((changed,)))

    assert result.mode is CompositionMode.FALLBACK
    assert "number word" in str(result.fallback_reason)


def test_an_unavailable_model_never_blocks_the_structured_finding() -> None:
    finding = _finding()

    result = compose_findings((finding,), _BrokenModel())

    assert result.mode is CompositionMode.FALLBACK
    assert result.narratives[0].text == deterministic_summary(finding)
    assert "TimeoutError" in str(result.fallback_reason)


def test_no_configured_model_uses_the_same_complete_fallback() -> None:
    finding = _finding()

    result = compose_findings((finding,), None)

    assert result.mode is CompositionMode.FALLBACK
    assert result.narratives[0].text == deterministic_summary(finding)
    assert "no findings language model" in str(result.fallback_reason)


def test_the_language_modules_cannot_reach_rules_verdicts_or_arithmetic() -> None:
    """The model has a prose proposal type, not a decision capability.

    This checks the module boundary as source rather than importing the whole ``workflow`` package,
    whose stage bridge legitimately calls the deterministic engine before narration begins.
    """
    repository = Path(__file__).resolve().parents[2]
    forbidden = {"rules", "units", "verdict"}
    for relative in ("workflow/findings_composer.py", "workflow/findings_bedrock.py"):
        tree = ast.parse((repository / relative).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert imported.isdisjoint(forbidden), f"{relative} reaches {imported & forbidden}"

    schema = ProposedNarrative.model_json_schema()["properties"]
    assert set(schema) == {"finding_key", "text"}
    assert "outcome" not in schema, "the model must not have a verdict field it can change"
