"""The chat wrapper stays behind the deterministic narration guard."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path

from app.review.chat import NOTHING_HAS_RUN, ChatMode, answer_question
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

    def compose(
        self, findings: Sequence[ComposerFinding], *, question: str | None = None
    ) -> ModelComposition:
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
        text=deterministic_summary(finding).replace("Needs correction", "Looks right", 1),
    )

    result = answer_question("Why did this fail?", (finding,), _Model((invented,)))

    assert result.mode is ChatMode.STRUCTURED_FALLBACK
    assert result.model_id is None
    assert result.narratives[0].text == deterministic_summary(finding)
    assert "reviewer-facing check name and deterministic outcome" in str(result.fallback_reason)


def test_unavailable_provider_degrades_to_plain_structured_findings() -> None:
    finding = _finding()

    result = answer_question("Which sheet?", (finding,), None)

    assert result.mode is ChatMode.STRUCTURED_FALLBACK
    assert result.narratives[0].text == deterministic_summary(finding)
    assert result.narratives[0].finding_key == finding.key


def test_empty_filter_returns_a_normal_state_message() -> None:
    passed = _finding(key="finding-pass", outcome="PASS")

    result = answer_question("Show me FAIL findings", (passed,), None)

    assert result.mode is ChatMode.STRUCTURED_FALLBACK
    assert result.fallback_reason == "no finding matched the deterministic outcome filter"
    assert result.text == "No FAIL findings in this run (1 total)."


def test_pass_filter_includes_pass_only_text_when_present() -> None:
    failed = _finding(key="finding-fail", outcome="FAIL")
    passed = _finding(key="finding-pass", outcome="PASS")

    result = answer_question("Which passes?", (failed, passed), _Model((_faithful(passed),)))

    assert result.text == (
        "Showing 1 of 2 recorded findings. The explanation below is grounded in the deterministic run."
    )


def test_chat_keeps_the_intro_free_of_raw_engine_reasons() -> None:
    review_required = _finding(key="finding-review", outcome="REVIEW_REQUIRED")
    missing = _finding(key="finding-missing", outcome="NOT_FOUND")

    result = answer_question(
        "Show all findings",
        (review_required, missing),
        _Model((_faithful(review_required), _faithful(missing))),
    )

    assert "Reviewer decisions required" not in result.text
    assert "CT-DEPTH-001" not in result.text
    assert "Showing 2 of 2 recorded findings" in result.text


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


# ---------------------------------------------------------------------------
# A package nobody has checked
# ---------------------------------------------------------------------------


def test_a_package_nobody_has_checked_is_not_a_run_with_no_failures() -> None:
    """**Input: no findings, and no check has run. Outcome: it says so.**

    The two empty cases arrive here identically and mean opposite things. Asked "Why did this
    fail?" about a package uploaded four minutes earlier and never checked, this answered *"No FAIL
    findings in this run (0 total)"* — which a reviewer reads as a clean bill of health for a
    drawing nothing has looked at. That is the most expensive sentence this module can say, and it
    was the default one.
    """
    result = answer_question("Why did this fail?", (), None, checks_have_run=False)

    assert result.text == NOTHING_HAS_RUN
    assert "No FAIL findings" not in result.text
    assert result.narratives == ()
    assert result.fallback_reason == "no checks have been run on this package"


def test_a_run_that_found_nothing_still_says_the_run_happened() -> None:
    """Outcome: the existing sentence, unchanged.

    The other half of the distinction. A check that ran and found nothing is a real result and must
    keep reading like one — the fix must not make every empty answer sound like an unfinished job.
    """
    result = answer_question("Show me FAIL findings", (), None)

    assert result.text == "No FAIL findings in this run (0 total)."


def test_nothing_having_run_outranks_the_outcome_filter() -> None:
    """Outcome: the same sentence whichever outcome was asked about.

    "Show me PASS findings" on an unchecked package must not answer "no PASS findings" either. The
    filter chooses among recorded findings; where there is no run, there is nothing to choose from
    and the question does not have an answer yet.
    """
    for question in ("Show all findings", "Which passes?", "Show FAIL findings"):
        assert answer_question(question, (), None, checks_have_run=False).text == NOTHING_HAS_RUN
