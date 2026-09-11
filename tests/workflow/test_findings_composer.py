"""The language layer may change phrasing and nothing else."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from workflow.findings_composer import (
    ComposerFinding,
    ComposerOperand,
    CompositionMode,
    ModelComposition,
    ProposedExplanation,
    ProposedNarrative,
    compose_findings,
    deterministic_summary,
    ground_explanations,
    narration_overview_context,
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


class _ExplainingModel:
    """A provider that returns only its sentences, the way a real adapter now does."""

    def __init__(self, explanation: str, *, key: str = "finding-a") -> None:
        self._explanation = explanation
        self._key = key

    def compose(self, findings: Sequence[ComposerFinding]) -> ModelComposition:
        return ModelComposition(
            narratives=ground_explanations(
                findings,
                (ProposedExplanation(finding_key=self._key, explanation=self._explanation),),
            ),
            model_id="configured-model",
            prompt_id="findings-composer-v1",
            template_id="deterministic-findings-v1",
            summary=None,
        )


class _StaticModel:
    def __init__(
        self, narratives: Sequence[ProposedNarrative], *, summary: str | None = None
    ) -> None:
        self._narratives = tuple(narratives)
        self._summary = summary

    def compose(self, findings: Sequence[ComposerFinding]) -> ModelComposition:
        del findings
        return ModelComposition(
            narratives=self._narratives,
            model_id="configured-model",
            prompt_id="findings-composer-v1",
            template_id="deterministic-findings-v1",
            summary=self._summary,
        )


class _BrokenModel:
    def compose(self, findings: Sequence[ComposerFinding]) -> ModelComposition:
        del findings
        raise TimeoutError("provider unavailable")


def _faithful(finding: ComposerFinding) -> ProposedNarrative:
    return ProposedNarrative(
        finding_key=finding.key,
        text=deterministic_summary(finding),
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
    assert all(item.text.startswith("Countertop depth —") for item in result.narratives)


def test_a_guarded_ai_overview_is_available_above_the_audit_record() -> None:
    finding = _finding()
    summary = "1 FAIL needs reviewer attention: approved 25 in versus " "vendor 25 1/2 in."

    result = compose_findings((finding,), _StaticModel((_faithful(finding),), summary=summary))

    assert result.mode is CompositionMode.LLM
    assert result.summary == summary


def test_an_ai_overview_cannot_introduce_an_unbacked_number_or_outcome() -> None:
    finding = _finding()

    result = compose_findings(
        (finding,),
        _StaticModel((_faithful(finding),), summary="999 PASS checks need no review."),
    )

    assert result.mode is CompositionMode.FALLBACK
    assert "AI overview introduced numeric claim(s) ['999']" in str(result.fallback_reason)


def test_an_ai_overview_cannot_introduce_an_unbacked_check_or_number_word() -> None:
    finding = _finding()

    result = compose_findings(
        (finding,),
        _StaticModel(
            (_faithful(finding),),
            summary="Three FAIL checks need attention, including CT-WIDTH-001.",
        ),
    )

    assert result.mode is CompositionMode.FALLBACK
    assert "AI overview introduced number word(s) ['three']" in str(result.fallback_reason)


def test_an_ai_overview_may_use_the_reviewer_facing_spaced_outcome() -> None:
    finding = _finding(outcome="NOT_FOUND")
    summary = "1 NOT FOUND check needs the missing recorded measurement."

    result = compose_findings((finding,), _StaticModel((_faithful(finding),), summary=summary))

    assert result.mode is CompositionMode.LLM
    assert result.summary == summary


def test_an_ai_overview_may_spell_an_exact_aggregate_count() -> None:
    finding = _finding()
    summary = "One FAIL needs attention."

    result = compose_findings((finding,), _StaticModel((_faithful(finding),), summary=summary))

    assert result.mode is CompositionMode.LLM
    assert result.summary == summary


def test_an_ai_overview_cannot_claim_an_individual_rule_outcome() -> None:
    finding = _finding()

    result = compose_findings(
        (finding,),
        _StaticModel((_faithful(finding),), summary="1 FAIL: CT-DEPTH-001 needs attention."),
    )

    assert result.mode is CompositionMode.FALLBACK
    assert "must not name individual rule IDs" in str(result.fallback_reason)


def test_overview_context_exposes_concrete_reviewer_decisions_and_missing_inputs() -> None:
    reviewer_choice = replace(
        _finding(outcome="REVIEW_REQUIRED"),
        reason="This check needs 'wall_config' before it can run.",
    )
    missing_input = replace(
        _finding(key="finding-b", outcome="NOT_FOUND"),
        reason="The final operation could not resolve required value 'front_offset'.",
    )

    context = narration_overview_context((reviewer_choice, missing_input))

    assert context["reviewer_decisions"] == [
        {"check": "Countertop depth", "reason": "This check needs 'wall_config' before it can run."}
    ]
    assert context["unresolved_inputs"] == ["sink front offset", "wall layout"]


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


def test_an_omitted_numeric_token_reaches_the_numeric_guard() -> None:
    finding = _finding()
    proposed = _faithful(finding)
    changed = ProposedNarrative(
        finding_key=finding.key,
        text=proposed.text.replace(" Evidence page: 3.", "").replace(
            "Evidence pages: 3.", "Evidence pages were recorded."
        ),
    )

    result = compose_findings((finding,), _StaticModel((changed,)))

    assert result.mode is CompositionMode.FALLBACK
    assert "omitted numeric token" in str(result.fallback_reason)


def test_a_duplicate_composed_finding_key_rejects_the_batch() -> None:
    finding = _finding()
    proposed = _faithful(finding)

    result = compose_findings((finding,), _StaticModel((proposed, proposed)))

    assert result.mode is CompositionMode.FALLBACK
    assert "duplicate composed finding" in str(result.fallback_reason)


def test_a_non_numeric_reason_cannot_be_dropped() -> None:
    finding = _finding()
    proposed = _faithful(finding)
    changed = ProposedNarrative(
        finding_key=finding.key,
        text=proposed.text.replace(finding.reason, "Check the drawing."),
    )

    result = compose_findings((finding,), _StaticModel((changed,)))

    assert result.mode is CompositionMode.FALLBACK
    assert "omitted deterministic fact" in str(result.fallback_reason)


def test_a_rejudged_verdict_in_model_prose_falls_back() -> None:
    finding = _finding()
    proposed = _faithful(finding)
    changed = ProposedNarrative(
        finding_key=finding.key,
        text=proposed.text.replace("Needs correction", "Looks right", 1),
    )

    result = compose_findings((finding,), _StaticModel((changed,)))

    assert result.mode is CompositionMode.FALLBACK
    assert "reviewer-facing check name and deterministic outcome" in str(result.fallback_reason)


def test_known_engine_missing_value_is_presented_without_internal_key() -> None:
    from workflow.findings_composer import reviewer_reason

    text = reviewer_reason(
        "derivation 'depth_less_near_clearance' could not resolve required value 'sink_interior_depth'.",
        "NOT_FOUND",
    )

    assert (
        text
        == "I can’t check this yet because the sink interior depth is missing. Please enter it to continue."
    )
    assert "depth_less_near_clearance" not in text
    assert "sink_interior_depth" not in text

    finding = replace(
        _finding(outcome="NOT_FOUND"),
        reason=(
            "derivation 'depth_less_near_clearance' could not resolve required value "
            "'sink_interior_depth'."
        ),
    )
    fallback = deterministic_summary(finding)
    assert text in fallback
    assert "depth_less_near_clearance" not in fallback
    assert "sink_interior_depth" not in fallback


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


# ---------------------------------------------------------------------------
# Grounding: the facts are prepended in code, never transcribed by the provider
# ---------------------------------------------------------------------------


def test_an_explanation_that_states_no_facts_still_carries_all_of_them() -> None:
    """**The fix.** Input: a sentence naming nothing. Outcome: a narrative the guard accepts.

    This is what the whole change is for. The provider used to be handed the complete deterministic
    summary as `required_text` and told to copy it character-for-character before adding a sentence;
    the guard then checked that every fact had survived the copy. Nova Lite dropped one clause —
    `Recorded comparison: 101/4 in != 51/2 in` — from an otherwise sound explanation, so the batch
    was rejected and every reviewer saw the plain fallback instead of narration.

    Transcription is not a thing to ask a language model for. `deterministic_summary` is prepended
    where the string already exists, so a provider cannot omit a fact it was never holding.
    """
    finding = _finding()

    grounded = ground_explanations(
        (finding,),
        (
            ProposedExplanation(
                finding_key=finding.key,
                explanation="The vendor drawing is shallower than the approved design allows.",
            ),
        ),
    )

    assert len(grounded) == 1
    text = grounded[0].text
    assert text.startswith(deterministic_summary(finding))
    assert text.endswith("The vendor drawing is shallower than the approved design allows.")
    # Every guarded fragment is present because the summary was prepended, not retyped.
    for fragment in ("Countertop depth", "25 1/2 in vs 25 in", "1/2 in", "1/16 in"):
        assert fragment in text


def test_the_grounded_narrative_passes_the_guard_end_to_end() -> None:
    """Outcome: `LLM` mode, not the structured fallback.

    The assertion that would have failed before this change, on the same provider output.
    """
    finding = _finding()
    model = _ExplainingModel("A quarter inch short; check the vendor drawing.")

    result = compose_findings((finding,), model)

    assert result.mode is CompositionMode.LLM
    assert "A quarter inch short; check the vendor drawing." in result.narratives[0].text


def test_an_explanation_that_invents_a_number_is_still_refused() -> None:
    """**The protection that must survive.** Input: a number nothing recorded. Outcome: fallback.

    Prepending the facts removed the provider's ability to *omit* one. It must not have removed the
    check on what the provider *adds* — the guard's real job. A model that decides the gap is 3/4 in
    when the run recorded 1/2 in is exactly the failure this project exists to prevent, and it still
    takes the whole batch down to deterministic prose.
    """
    result = compose_findings((_finding(),), _ExplainingModel("The gap is 3/4 in, not what it says."))

    assert result.mode is CompositionMode.FALLBACK
    assert "numeric token" in (result.fallback_reason or "")


def test_an_explanation_that_invents_a_verdict_is_still_refused() -> None:
    """Input: prose calling a FAIL a pass. Outcome: fallback, because verdicts are not the model's.

    `AGENTS.md` §2 puts every verdict in deterministic Python. A provider that can narrate one into
    existence has taken the decision, whatever the engine recorded.
    """
    result = compose_findings((_finding(),), _ExplainingModel("On balance this should PASS."))

    assert result.mode is CompositionMode.FALLBACK
    assert "verdict" in (result.fallback_reason or "")


def test_an_explanation_for_a_finding_this_run_did_not_select_is_refused() -> None:
    """**Input: a key from no finding. Outcome: refused as unbacked, never silently dropped.**

    `ground_explanations` carries an unknown key through rather than discarding it, precisely so
    `_guard_batch` can report it. Dropping it there would turn a provider hallucinating a finding
    into a batch that looked 1:1 and was short one narrative.
    """
    result = compose_findings((_finding(),), _ExplainingModel("Fine.", key="finding-that-does-not-exist"))

    assert result.mode is CompositionMode.FALLBACK
    assert "not 1:1" in (result.fallback_reason or "")


def test_no_adapter_lets_a_provider_supply_the_finding_text() -> None:
    """**The standing guard.** Outcome: no Bedrock adapter builds `narratives` from raw provider text.

    The defect this change fixes came back the moment somebody wired a provider's string straight
    into `ModelComposition.narratives`. Every adapter must route through `ground_explanations`, so
    the facts are the engine's in every path rather than in the one that was remembered.
    """
    adapters = (
        Path(__file__).resolve().parents[2] / "app" / "review" / "chat_bedrock.py",
        Path(__file__).resolve().parents[2] / "workflow" / "findings_bedrock.py",
    )

    for adapter in adapters:
        source = adapter.read_text(encoding="utf-8")
        assert "ground_explanations(findings, batch.findings)" in source, adapter.name
        assert "narratives=tuple(batch.findings)" not in source, adapter.name
