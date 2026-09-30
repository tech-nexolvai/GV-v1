"""Tests for the hard-bounded LangGraph extraction flow.

Each test names its input, expected terminal state and safety reason so the graph's
behavior can be reviewed without reading its implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import get_args

import pytest

from evidence.candidate import ObservationCandidate
from evidence.coordinates import ImagePoint
from extraction.agent.graph import (
    AbstentionTerminal,
    BoundedAgentGraph,
    BoundedRegionContext,
    CandidateTerminal,
    GraphLimits,
    GraphTerminal,
    RefinedCrop,
    RetryableToolFailure,
    scripted,
)
from extraction.agent.outcomes import abstain
from extraction.agent.tools import (
    AbstainArguments,
    AgentToolbox,
    OcrVerificationArguments,
    RefineCropArguments,
    Refinement,
    ToolCall,
    ToolCallRecord,
    VlmReadingArguments,
    VlmRole,
)


@dataclass
class Recorder:
    """In-memory call recorder used to prove blocked calls never execute."""

    calls: list[ToolCallRecord] = field(default_factory=list)

    def record(self, call: ToolCallRecord) -> None:
        self.calls.append(call)


def _limits(**changes: int) -> GraphLimits:
    values = {
        "max_steps": 6,
        "max_ocr_retries": 2,
        "max_primary_vlm_calls": 1,
        "max_vlm_escalations": 1,
        "max_nearby_text_items": 2,
        "max_nearby_geometry_items": 2,
    }
    values.update(changes)
    return GraphLimits(**values)


def _context(**changes: object) -> BoundedRegionContext:
    values: dict[str, object] = {
        "region_id": "region-1",
        "crop_artifact_id": "crop-0",
        "nearby_text": ("984",),
        "nearby_geometry_refs": ("line-7",),
    }
    values.update(changes)
    return BoundedRegionContext(**values)  # type: ignore[arg-type]


def _candidate() -> ObservationCandidate:
    return ObservationCandidate(
        candidate_id="candidate-1",
        extractor="nova",
        extractor_version="1",
        raw_text="984",
        parsed_value=None,
        unit_guess=None,
        semantic_guess=None,
        page=0,
        polygon=(ImagePoint(10, 20), ImagePoint(30, 40)),
        confidence=Decimal("0.90"),
        ambiguity_flags=(),
    )


def _toolbox(
    recorder: Recorder,
    *,
    refine: object = RetryableToolFailure("unused"),
    ocr: object = RetryableToolFailure("unused"),
    vlm: object = RetryableToolFailure("unused"),
) -> AgentToolbox:
    return AgentToolbox(
        refine_crop=lambda _arguments: refine,
        request_ocr_verification=lambda _arguments: ocr,
        request_vlm_reading=lambda _arguments: vlm,
        abstain=abstain,
        recorder=recorder,
    )


def _ocr(call_id: str = "ocr-1") -> ToolCall:
    return ToolCall(call_id, OcrVerificationArguments("region-1", "crop-0"))


def _vlm(call_id: str, role: VlmRole = VlmRole.PRIMARY) -> ToolCall:
    return ToolCall(call_id, VlmReadingArguments("region-1", "crop-0", role))


def test_candidate_is_one_of_exactly_two_terminal_states() -> None:
    """Input: one successful OCR call. Output: candidate terminal, never best-effort."""

    recorder = Recorder()
    graph = BoundedAgentGraph(limits=_limits(), toolbox=_toolbox(recorder, ocr=_candidate()))

    result = graph.run(_context(), scripted((_ocr(),)))

    assert isinstance(result, CandidateTerminal)
    assert result.candidate == _candidate()
    assert set(get_args(GraphTerminal.__value__)) == {  # type: ignore[attr-defined]
        CandidateTerminal,
        AbstentionTerminal,
    }
    assert len(recorder.calls) == 1


def test_explicit_abstain_is_the_only_unsuccessful_terminal() -> None:
    """Input: allow-listed abstain action. Output: readable review-required abstention."""

    recorder = Recorder()
    graph = BoundedAgentGraph(limits=_limits(), toolbox=_toolbox(recorder))
    action = ToolCall("stop-1", AbstainArguments("region-1", "readings remain ambiguous"))

    result = graph.run(_context(), scripted((action,)))

    assert isinstance(result, AbstentionTerminal)
    assert result.abstention.reason == "readings remain ambiguous"
    assert result.abstention.requires_review is True


def test_empty_action_sequence_abstains_instead_of_returning_partial_data() -> None:
    """Input: no available action. Output: abstention because no candidate was produced."""

    recorder = Recorder()
    result = BoundedAgentGraph(limits=_limits(), toolbox=_toolbox(recorder)).run(
        _context(), scripted(())
    )

    assert isinstance(result, AbstentionTerminal)
    assert "ended without a candidate" in result.abstention.reason
    assert recorder.calls == []


def test_ocr_bound_blocks_third_retry_before_tool_invocation() -> None:
    """Input: three failed OCR retries. Output: abstention after exactly two calls."""

    recorder = Recorder()
    graph = BoundedAgentGraph(limits=_limits(), toolbox=_toolbox(recorder))

    result = graph.run(_context(), scripted((_ocr("ocr-1"), _ocr("ocr-2"), _ocr("ocr-3"))))

    assert isinstance(result, AbstentionTerminal)
    assert result.abstention.reason == "maximum OCR verification attempts reached"
    assert [call.call_id for call in recorder.calls] == ["ocr-1", "ocr-2"]


def test_vlm_bound_allows_primary_and_one_escalation_only() -> None:
    """Input: three failed VLM calls. Output: abstention before the third invocation."""

    recorder = Recorder()
    graph = BoundedAgentGraph(limits=_limits(), toolbox=_toolbox(recorder))

    result = graph.run(
        _context(),
        scripted(
            (
                _vlm("vlm-1"),
                _vlm("vlm-2", VlmRole.ESCALATION),
                _vlm("vlm-3", VlmRole.ESCALATION),
            )
        ),
    )

    assert isinstance(result, AbstentionTerminal)
    assert result.abstention.reason == "maximum VLM calls reached"
    assert [call.call_id for call in recorder.calls] == ["vlm-1", "vlm-2"]


def test_step_bound_stops_before_a_seventh_tool_call() -> None:
    """Input: seven valid crop refinements. Output: six calls then bounded abstention."""

    recorder = Recorder()

    def refine(arguments: RefineCropArguments) -> RefinedCrop:
        number = int(arguments.crop_artifact_id.removeprefix("crop-"))
        return RefinedCrop(f"crop-{number + 1}")

    toolbox = AgentToolbox(
        refine_crop=refine,
        request_ocr_verification=lambda _arguments: RetryableToolFailure("unused"),
        request_vlm_reading=lambda _arguments: RetryableToolFailure("unused"),
        abstain=abstain,
        recorder=recorder,
    )
    actions = tuple(
        ToolCall(
            f"refine-{number}",
            RefineCropArguments("region-1", f"crop-{number}", Refinement.SHARPER),
        )
        for number in range(7)
    )

    result = BoundedAgentGraph(limits=_limits(), toolbox=toolbox).run(_context(), scripted(actions))

    assert isinstance(result, AbstentionTerminal)
    assert result.abstention.reason == "maximum graph steps reached"
    assert len(recorder.calls) == 6


@pytest.mark.parametrize(
    ("context", "reason"),
    [
        (_context(nearby_text=("one", "two", "three")), "nearby text"),
        (
            _context(nearby_geometry_refs=("one", "two", "three")),
            "nearby geometry",
        ),
    ],
)
def test_oversized_context_abstains_before_any_tool(
    context: BoundedRegionContext, reason: str
) -> None:
    """Input: context beyond an explicit bound. Output: no tool call and abstention."""

    recorder = Recorder()
    result = BoundedAgentGraph(limits=_limits(), toolbox=_toolbox(recorder, ocr=_candidate())).run(
        context, scripted((_ocr(),))
    )

    assert isinstance(result, AbstentionTerminal)
    assert reason in result.abstention.reason
    assert recorder.calls == []


def test_full_package_context_cannot_be_constructed() -> None:
    """Input: forbidden full-package field. Output: constructor rejection by shape."""

    with pytest.raises(TypeError, match="full_package"):
        BoundedRegionContext(  # type: ignore[call-arg]
            region_id="region-1",
            crop_artifact_id="crop-0",
            nearby_text=(),
            nearby_geometry_refs=(),
            full_package="all-pages",
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"max_steps": 5}, "between 6 and 8"),
        ({"max_steps": 9}, "between 6 and 8"),
        ({"max_ocr_retries": 3}, "between 0 and 2"),
        ({"max_primary_vlm_calls": 2}, "exactly 1"),
        ({"max_vlm_escalations": 2}, "must be 0 or 1"),
    ],
)
def test_unsafe_limits_are_rejected_at_construction(changes: dict[str, int], message: str) -> None:
    """Input: a bound outside the approved envelope. Output: loud construction error."""

    with pytest.raises(ValueError, match=message):
        _limits(**changes)


# ---------------------------------------------------------------------------
# #757 — the graph asks a planner, and trusts nothing it is told
# ---------------------------------------------------------------------------


def test_a_planner_cannot_propose_a_reading_no_tool_returned() -> None:
    """**Outcome: abstention.** A proposal must be one of this run's readings, by identity — a
    planner cannot put a value in front of a reviewer that nothing read."""
    from extraction.agent.graph import Propose

    recorder = Recorder()
    fabricated = _candidate()

    result = BoundedAgentGraph(limits=_limits(), toolbox=_toolbox(recorder)).run(
        _context(), lambda _progress: Propose(fabricated)
    )

    assert isinstance(result, AbstentionTerminal)
    assert "no tool returned" in result.abstention.reason


def test_a_planner_that_fails_closes_toward_abstention() -> None:
    recorder = Recorder()

    def broken(_progress: object) -> ToolCall:
        raise RuntimeError("planner bug")

    result = BoundedAgentGraph(limits=_limits(), toolbox=_toolbox(recorder)).run(_context(), broken)

    assert isinstance(result, AbstentionTerminal)
    assert "planner failed" in result.abstention.reason
    assert recorder.calls == []


def test_an_escalation_before_the_primary_reading_is_refused_before_it_runs() -> None:
    """Outcome: the roles are enforced — one primary, then at most one escalation (DESIGN_AI §3.2)."""
    recorder = Recorder()
    graph = BoundedAgentGraph(limits=_limits(), toolbox=_toolbox(recorder, vlm=_candidate()))

    result = graph.run(_context(), scripted((_vlm("vlm-1", VlmRole.ESCALATION),)))

    assert isinstance(result, AbstentionTerminal)
    assert recorder.calls == []


def test_a_second_primary_reading_is_refused_before_it_runs() -> None:
    recorder = Recorder()
    graph = BoundedAgentGraph(
        limits=_limits(), toolbox=_toolbox(recorder, vlm=RetryableToolFailure("no answer"))
    )

    result = graph.run(_context(), scripted((_vlm("vlm-1"), _vlm("vlm-2"))))

    assert isinstance(result, AbstentionTerminal)
    assert [call.call_id for call in recorder.calls] == ["vlm-1"]


def test_the_planner_sees_every_reading_refinement_and_failure_so_far() -> None:
    """Outcome: after a refinement and a failed read, the progress the planner is handed holds both."""
    from extraction.agent.graph import AgentProgress, Halt

    recorder = Recorder()
    seen: list[AgentProgress] = []
    graph = BoundedAgentGraph(
        limits=_limits(),
        toolbox=_toolbox(
            recorder, refine=RefinedCrop("crop-1"), vlm=RetryableToolFailure("no answer")
        ),
    )
    calls = iter(
        (
            ToolCall("refine", RefineCropArguments("region-1", "crop-0", Refinement.UPRIGHT)),
            ToolCall("vlm", VlmReadingArguments("region-1", "crop-1", VlmRole.PRIMARY)),
        )
    )

    def watch(progress: AgentProgress) -> ToolCall | Halt:
        seen.append(progress)
        return next(calls, None) or Halt("done")

    graph.run(_context(), watch)

    final = seen[-1]
    assert final.refinements == ("upright",)
    assert final.failures == ("no answer",)
    assert final.attempted == ("refine_crop:upright", "request_vlm_reading:primary")
    assert final.current_crop_artifact_id == "crop-1"
