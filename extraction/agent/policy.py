"""The reading agent's decision table: which step comes next, chosen by code from observations (#757).

`extraction/agent/graph.py` runs one step at a time and bounds every step. This module decides
what each step is. It replaces the fixed script the stage used to hand the graph — OCR, OCR, VLM,
abstain, whatever each returned — with a table that reads what the checks in
`extraction/agent/observations.py` found and picks the next move from it.

**The table, in order; the first row that applies decides.**

| The observations say… | Next step |
|---|---|
| the label is cut off at the crop's edge, not yet widened | `refine_crop` → **whole run** |
| it was widened and is still cut off | abstain: the label does not fit a bounded crop |
| the label reads sideways, not yet turned | `refine_crop` → **upright** |
| no reading yet (or the last was set aside) | the primary vision reader; then **sharper** and the escalation reader; then abstain |
| readings disagree | **sharper**, then the escalation reader; still disagreeing → abstain with every reading |
| one exact value, and nothing disagrees | propose it |

**What it never does.** It never resolves a numeric disagreement: two readings that differ end in an
abstention listing both, and a reviewer decides — the rule `DESIGN_AI.md` §3.2 states and the
graph cannot enforce for it. It never proposes a reading no tool returned; the graph refuses that
anyway. And a stacked fraction is proposed like any other reading, because the rule that sends
every stacked fraction to a person (#726) is enforced downstream, where the proposal is still a
candidate to confirm.

**The budget is the graph's.** Before naming a step the table checks it fits what is left, so the
abstention a budget forces says what was tried rather than only that a limit was reached.

Source: issue #757 · Verification: `tests/extraction/agent/test_policy.py`
"""

from __future__ import annotations

from extraction.agent.graph import AgentProgress, GraphLimits, Planner, Propose
from extraction.agent.observations import Fact, RegionFacts, describe, observe, valid_looks
from extraction.agent.tools import (
    AbstainArguments,
    RefineCropArguments,
    Refinement,
    ToolCall,
    VlmReadingArguments,
    VlmRole,
)

__all__ = ["policy_planner"]


def policy_planner(facts: RegionFacts, limits: GraphLimits) -> Planner:
    """The planner for one region: the decision table over these facts, within these limits."""
    if not isinstance(facts, RegionFacts):
        raise TypeError("facts must be RegionFacts")
    if not isinstance(limits, GraphLimits):
        raise TypeError("limits must be GraphLimits")

    def plan(progress: AgentProgress) -> ToolCall | Propose:
        region = progress.context.region_id
        crop = progress.current_crop_artifact_id
        observed = observe(facts, progress)
        step = progress.steps + 1

        def abstain(reason: str) -> ToolCall:
            return ToolCall(
                f"{region}:{step}:abstain",
                AbstainArguments(region, f"{reason} — {describe(facts, progress)}"),
            )

        def refine(refinement: Refinement) -> ToolCall:
            return ToolCall(
                f"{region}:{step}:refine-{refinement.value}",
                RefineCropArguments(region, crop, refinement),
            )

        def read(role: VlmRole) -> ToolCall:
            return ToolCall(
                f"{region}:{step}:vlm-{role.value}", VlmReadingArguments(region, crop, role)
            )

        primary_left = progress.primary_vlm_calls < limits.max_primary_vlm_calls
        escalation_left = not primary_left and progress.vlm_escalations < limits.max_vlm_escalations
        # One step is always kept for the abstention itself, so a budget that runs out still ends
        # with a reason rather than with the graph's bare limit.
        steps_left = limits.max_steps - progress.steps - 1

        def tried(refinement: Refinement) -> bool:
            return f"refine_crop:{refinement.value}" in progress.attempted

        if Fact.LABEL_CUT_AT_EDGE in observed:
            if tried(Refinement.WHOLE_RUN) or steps_left < 1:
                return abstain("the label is cut off and does not fit a bounded crop")
            return refine(Refinement.WHOLE_RUN)

        # Sideways and not yet tried: turn it. A turn that failed is not retried; the crop is
        # read as it is, which is what every reader did before #757.
        if Fact.SIDEWAYS in observed and not tried(Refinement.UPRIGHT) and steps_left >= 1:
            return refine(Refinement.UPRIGHT)

        if Fact.NO_READING in observed:
            if primary_left and steps_left >= 1:
                return read(VlmRole.PRIMARY)
            if escalation_left and not tried(Refinement.SHARPER) and steps_left >= 2:
                return refine(Refinement.SHARPER)
            if escalation_left and steps_left >= 1:
                return read(VlmRole.ESCALATION)
            return abstain("no reader gave one exact value")

        if Fact.READINGS_DISAGREE in observed:
            if escalation_left and not tried(Refinement.SHARPER) and steps_left >= 2:
                return refine(Refinement.SHARPER)
            if escalation_left and steps_left >= 1:
                return read(VlmRole.ESCALATION)
            # **Never a pick.** Every reading goes to the reviewer; none is chosen here.
            return abstain("the readings disagree, so a reviewer must read it")

        readings = valid_looks(progress)
        if not readings:
            return abstain("no reader gave one exact value")
        return Propose(readings[-1].candidate)

    return plan
