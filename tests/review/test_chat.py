"""The chat wrapper stays behind the deterministic narration guard."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path

from app.review.chat import ChatMode, answer_question
from workflow.findings_composer import (
    ComposerFinding,
    ComposerOperand,
    ModelComposition,
    ProposedNarrative,
    deterministic_summary,
)


def _finding(*, key: str = "finding-fail", outcome: str = "FAIL") -> ComposerFinding:
    return ComposerFinding(
        key=key,
        check="CT-DEPTH-001",
        check_name="Countertop depth",
        outcome=outcome,
        severity="FLAG",
        reason="The vendor depth differs from the approved depth by 1/2 in.",
        comparison="25 1/2 in vs 25 in",
        difference="1/2 in",
        tolerance=None,
        arithmetic_unit="in",
        operands=(
            ComposerOperand("approved_depth", "25 in", "ARCH", "13"),
            ComposerOperand("vendor_depth", "25 1/2 in", "SHOP", "13"),
        ),
        evidence_pages=("13",),
        notes=(),
    )


class _Model:
    def __init__(self, narratives: Sequence[ProposedNarrative]) -> None:
        self._narratives = tuple(narratives)

    def compose(self, findings: Sequence[ComposerFinding]) -> ModelComposition:
        del findings
        return ModelComposition(
            narratives=self._narratives,
            model_id="configured-model",
            prompt_id="reviewer-chat-v1",
            template_id="grounded-deterministic-findings-v1",
        )


def _faithful(finding: ComposerFinding) -> ProposedNarrative:
    return ProposedNarrative(
        finding_key=finding.key,
        text=deterministic_summary(finding),
    )


def test_chat_only_returns_narratives_backed_by_the_selected_deterministic_run() -> None:
    failed = _finding()
    passed = _finding(key="finding-pass", outcome="PASS")

    result = answer_question("What failed and why?", (failed, passed), _Model((_faithful(failed),)))

    assert result.mode is ChatMode.LLM
    assert result.model_id == "configured-model"
    assert [item.finding_key for item in result.narratives] == ["finding-fail"]
    assert "1 of 2 recorded findings" in result.text


def test_chat_rejects_a_provider_that_asserts_a_verdict_not_in_the_finding() -> None:
    finding = _finding()
    invented = ProposedNarrative(
        finding_key=finding.key,
        text=deterministic_summary(finding).replace("CT-DEPTH-001: FAIL.", "CT-DEPTH-001: PASS."),
    )

    result = answer_question("Why did this fail?", (finding,), _Model((invented,)))

    assert result.mode is ChatMode.STRUCTURED_FALLBACK
    assert result.model_id is None
    assert result.narratives[0].text == deterministic_summary(finding)
    assert "deterministic check and outcome" in str(result.fallback_reason)


def test_unavailable_provider_degrades_to_plain_structured_findings() -> None:
    finding = _finding()

    result = answer_question("Which sheet?", (finding,), None)

    assert result.mode is ChatMode.STRUCTURED_FALLBACK
    assert result.narratives[0].text == deterministic_summary(finding)
    assert result.narratives[0].finding_key == finding.key


def test_chat_language_modules_cannot_reach_rules_verdicts_or_arithmetic() -> None:
    """The chat is a post-verdict language surface, not a second decision engine."""
    root = Path(__file__).resolve().parents[2]
    forbidden = {"rules", "units", "verdict"}
    for relative in ("app/review/chat.py", "app/review/chat_bedrock.py"):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert imported.isdisjoint(forbidden), f"{relative} reaches {imported & forbidden}"
