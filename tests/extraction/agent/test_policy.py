"""The reading agent's decision table, row by row (#757).

Verification for: `extraction/agent/policy.py` and `extraction/agent/observations.py`, run through the
real bounded graph with stand-in tools.

Each test drives one row: what the observations say, and the step the table takes next. The one
that matters most is `test_two_disagreeing_readings_end_with_a_reviewer_never_a_pick`: a numeric
disagreement is never resolved by the agent, however the readings arrive.

No model is called; the tools return authored readings.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction

from evidence.candidate import ObservationCandidate
from evidence.coordinates import ImagePoint
from extraction.agent.graph import (
    AbstentionTerminal,
    BoundedAgentGraph,
    BoundedRegionContext,
    CandidateTerminal,
    GraphLimits,
    RefinedCrop,
    RetryableToolFailure,
)
from extraction.agent.observations import Fact, RegionFacts, observe
from extraction.agent.outcomes import abstain
from extraction.agent.policy import policy_planner
from extraction.agent.tools import (
    AgentToolbox,
    RefineCropArguments,
    ToolCallRecord,
    VlmReadingArguments,
)
from units.measurement import Measurement, Unit

LIMITS = GraphLimits(
    max_steps=6,
    max_ocr_retries=2,
    max_primary_vlm_calls=1,
    max_vlm_escalations=1,
    max_nearby_text_items=0,
    max_nearby_geometry_items=0,
)
CONTEXT = BoundedRegionContext(
    region_id="r", crop_artifact_id="crop-0", nearby_text=(), nearby_geometry_refs=()
)


def _facts(**changes: object) -> RegionFacts:
    values: dict[str, object] = {
        "cut_at_edge": False,
        "rotation_degrees": 0,
        "stacked_fraction": False,
        "shape_reading": None,
        "other_route_values": (),
    }
    values.update(changes)
    return RegionFacts(**values)  # type: ignore[arg-type]


def _reading(text: str, reader: str = "vision") -> ObservationCandidate:
    return ObservationCandidate(
        candidate_id=f"{reader}-{text}",
        extractor=reader,
        extractor_version="1",
        raw_text=text,
        parsed_value=None,
        unit_guess=Unit.INCH,
        semantic_guess=None,
        page=0,
        polygon=(ImagePoint(0, 0), ImagePoint(1, 1)),
        confidence=None,
        ambiguity_flags=(),
    )


def _inches(value: Fraction | int) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, f"{value} in")


@dataclass
class _Tools:
    """Stand-in tools: each refinement succeeds unless listed as failing; readings in order."""

    readings: list[object] = field(default_factory=list)
    failing: frozenset[str] = frozenset()
    calls: list[ToolCallRecord] = field(default_factory=list)
    crops: int = 0

    def record(self, call: ToolCallRecord) -> None:
        self.calls.append(call)

    def refine(self, arguments: RefineCropArguments) -> object:
        if arguments.refinement.value in self.failing:
            return RetryableToolFailure(f"{arguments.refinement.value} could not be done")
        self.crops += 1
        return RefinedCrop(f"crop-{self.crops}")

    def read(self, arguments: VlmReadingArguments) -> object:
        del arguments
        return self.readings.pop(0) if self.readings else RetryableToolFailure("no answer")

    def box(self) -> AgentToolbox:
        return AgentToolbox(
            refine_crop=self.refine,
            request_ocr_verification=lambda _arguments: RetryableToolFailure("not used"),
            request_vlm_reading=self.read,
            abstain=abstain,
            recorder=self,
        )


def _run(tools: _Tools, facts: RegionFacts) -> object:
    return BoundedAgentGraph(limits=LIMITS, toolbox=tools.box()).run(
        CONTEXT, policy_planner(facts, LIMITS)
    )


def _steps(tools: _Tools) -> list[str]:
    return [record.call_id.split(":", 2)[-1] for record in tools.calls]


# ---------------------------------------------------------------------------
# One row each
# ---------------------------------------------------------------------------


def test_nothing_read_yet_asks_the_primary_reader_and_proposes_one_exact_value() -> None:
    tools = _Tools(readings=[_reading('12"')])

    result = _run(tools, _facts())

    assert _steps(tools) == ["vlm-primary"]
    assert isinstance(result, CandidateTerminal) and result.candidate.raw_text == '12"'


def test_a_label_cut_at_the_edge_is_widened_to_its_whole_run_first() -> None:
    """**Acceptance criterion.** Outcome: `refine_crop → whole run`, then the read."""
    tools = _Tools(readings=[_reading('192"')])

    result = _run(tools, _facts(cut_at_edge=True))

    assert _steps(tools) == ["refine-whole_run", "vlm-primary"]
    assert isinstance(result, CandidateTerminal)


def test_a_label_that_cannot_be_widened_abstains_rather_than_read_in_part() -> None:
    """**Acceptance criterion.** Outcome: one widening tried; still cut off, so a reviewer reads it."""
    tools = _Tools(readings=[_reading('92"')], failing=frozenset({"whole_run"}))

    result = _run(tools, _facts(cut_at_edge=True))

    assert _steps(tools) == ["refine-whole_run", "abstain"]
    assert isinstance(result, AbstentionTerminal)
    assert "cut off" in result.abstention.reason


def test_a_sideways_label_is_turned_upright_before_it_is_read() -> None:
    tools = _Tools(readings=[_reading('26 3/4"')])

    result = _run(tools, _facts(rotation_degrees=90))

    assert _steps(tools) == ["refine-upright", "vlm-primary"]
    assert isinstance(result, CandidateTerminal)


def test_a_turn_that_fails_is_not_retried_and_the_label_is_read_as_it_is() -> None:
    tools = _Tools(readings=[_reading('26 3/4"')], failing=frozenset({"upright"}))

    result = _run(tools, _facts(rotation_degrees=270))

    assert _steps(tools) == ["refine-upright", "vlm-primary"]
    assert isinstance(result, CandidateTerminal)


def test_a_reading_that_is_not_one_value_is_set_aside_then_sharper_then_escalation() -> None:
    """Outcome: the unparsable reading is dropped, a sharper crop is taken, the escalation reads."""
    tools = _Tools(readings=[_reading("see detail"), _reading('3/4"', "escalation")])

    result = _run(tools, _facts())

    assert _steps(tools) == ["vlm-primary", "refine-sharper", "vlm-escalation"]
    assert isinstance(result, CandidateTerminal) and result.candidate.raw_text == '3/4"'


def test_a_reading_whose_mm_and_inch_halves_disagree_is_set_aside() -> None:
    """Outcome: `254 [11]` states two different lengths; it is dropped like an unparsable read."""
    tools = _Tools(readings=[_reading("254 [11]"), _reading("254 [10]", "escalation")])

    result = _run(tools, _facts())

    assert _steps(tools) == ["vlm-primary", "refine-sharper", "vlm-escalation"]
    assert isinstance(result, CandidateTerminal) and result.candidate.raw_text == "254 [10]"


def test_agreement_with_another_route_is_proposed_without_escalating() -> None:
    tools = _Tools(readings=[_reading('12"')])

    result = _run(tools, _facts(other_route_values=(_inches(12),)))

    assert _steps(tools) == ["vlm-primary"]
    assert isinstance(result, CandidateTerminal)


def test_two_disagreeing_readings_end_with_a_reviewer_never_a_pick() -> None:
    """**The rule the graph cannot enforce for the table** (DESIGN_AI §3.2). Outcome: a sharper look,
    one escalation, and — still disagreeing — an abstention that lists both readings."""
    tools = _Tools(readings=[_reading('3/4"'), _reading('3 3/4"', "escalation")])

    result = _run(tools, _facts(other_route_values=(_inches(Fraction(15, 4)),)))

    assert _steps(tools) == ["vlm-primary", "refine-sharper", "vlm-escalation", "abstain"]
    assert isinstance(result, AbstentionTerminal)
    assert "disagree" in result.abstention.reason
    assert "'3/4\"'" in result.abstention.reason and "'3 3/4\"'" in result.abstention.reason


def test_two_against_one_is_still_a_disagreement_not_a_vote() -> None:
    """**No majority rule.** The primary reads `1 3/4"`, the shape reader and the escalation both
    `3/4"`. Outcome: an abstention — counting votes would be choosing between readings, which is a
    reviewer's decision, not the agent's."""
    tools = _Tools(readings=[_reading('1 3/4"'), _reading('3/4"', "escalation")])

    result = _run(tools, _facts(shape_reading=_inches(Fraction(3, 4))))

    assert _steps(tools) == ["vlm-primary", "refine-sharper", "vlm-escalation", "abstain"]
    assert isinstance(result, AbstentionTerminal)
    assert "disagree" in result.abstention.reason


def test_a_stacked_fraction_goes_to_a_reviewer_without_a_paid_call() -> None:
    """**Every stacked fraction reaches a person (#726).** Outcome: an abstention that says so, and no
    reader asked — the validator refuses any reading of such a crop (#735), so a call would be paid
    for and discarded. First in the table: it holds even for a label that is also cut off."""
    tools = _Tools(readings=[_reading('3/4"')])

    result = _run(tools, _facts(stacked_fraction=True, cut_at_edge=True))

    assert _steps(tools) == ["abstain"]
    assert isinstance(result, AbstentionTerminal)
    assert "stacked fraction" in result.abstention.reason


def _progress(**changes: object) -> object:
    from extraction.agent.graph import AgentProgress

    values: dict[str, object] = {
        "context": CONTEXT,
        "steps": 0,
        "ocr_calls": 0,
        "primary_vlm_calls": 0,
        "vlm_escalations": 0,
        "current_crop_artifact_id": "crop-0",
        "looks": (),
        "refinements": (),
        "failures": (),
        "attempted": (),
        "last_result": None,
    }
    values.update(changes)
    return AgentProgress(**values)  # type: ignore[arg-type]


def test_the_budget_ends_with_what_was_tried_not_only_that_a_limit_was_reached() -> None:
    """**Acceptance criterion: budget reached → abstain.** Outcome: with one step left and nothing
    read, the table abstains — keeping that step for the reason — instead of asking a reader the
    graph would then refuse, and the reason names what was tried."""
    from extraction.agent.tools import AbstainArguments, Refinement, ToolCall

    planner = policy_planner(_facts(), LIMITS)
    progress = _progress(
        steps=LIMITS.max_steps - 1,
        refinements=(Refinement.SHARPER.value,),
        attempted=("refine_crop:sharper",),
    )

    decision = planner(progress)  # type: ignore[arg-type]

    assert isinstance(decision, ToolCall) and isinstance(decision.arguments, AbstainArguments)
    assert "no reader gave one exact value" in decision.arguments.reason
    assert "sharper" in decision.arguments.reason


def test_every_step_the_table_takes_fits_the_graphs_budget() -> None:
    """Outcome: the longest path — widen, turn, read, sharpen, escalate — ends in the table's own
    abstention within the six steps, never in the graph's bare limit."""
    tools = _Tools(readings=[_reading("?"), _reading("??", "escalation")])

    result = _run(tools, _facts(cut_at_edge=True, rotation_degrees=90))

    assert _steps(tools) == [
        "refine-whole_run",
        "refine-upright",
        "vlm-primary",
        "refine-sharper",
        "vlm-escalation",
        "abstain",
    ]
    assert isinstance(result, AbstentionTerminal)
    assert "maximum" not in result.abstention.reason
    assert "widened" in result.abstention.reason and "upright" in result.abstention.reason


def test_the_table_decides_from_observations_only() -> None:
    """Outcome: the same facts and the same progress always give the same step — no model, no
    clock, no randomness in between."""
    planner: Callable[..., object] = policy_planner(_facts(rotation_degrees=90), LIMITS)
    progress = _progress()

    assert planner(progress) == planner(progress)
    assert observe(_facts(rotation_degrees=90), progress) == frozenset(  # type: ignore[arg-type]
        {Fact.SIDEWAYS, Fact.NO_READING}
    )
