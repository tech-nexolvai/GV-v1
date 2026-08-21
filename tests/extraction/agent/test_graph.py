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
)
from extraction.agent.outcomes import abstain
from extraction.agent.tools import (
    AbstainArguments,
    AgentToolbox,
    OcrVerificationArguments,
    RefineCropArguments,
    ToolCall,
    ToolCallRecord,
    VlmReadingArguments,
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


def _vlm(call_id: str) -> ToolCall:
    return ToolCall(call_id, VlmReadingArguments("region-1", "crop-0"))


def test_candidate_is_one_of_exactly_two_terminal_states() -> None:
    """Input: one successful OCR call. Output: candidate terminal, never best-effort."""

    recorder = Recorder()
    graph = BoundedAgentGraph(limits=_limits(), toolbox=_toolbox(recorder, ocr=_candidate()))

    result = graph.run(_context(), (_ocr(),))

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

    result = graph.run(_context(), (action,))

    assert isinstance(result, AbstentionTerminal)
    assert result.abstention.reason == "readings remain ambiguous"
    assert result.abstention.requires_review is True


def test_empty_action_sequence_abstains_instead_of_returning_partial_data() -> None:
    """Input: no available action. Output: abstention because no candidate was produced."""

    recorder = Recorder()
    result = BoundedAgentGraph(limits=_limits(), toolbox=_toolbox(recorder)).run(_context(), ())

    assert isinstance(result, AbstentionTerminal)
    assert "ended without a candidate" in result.abstention.reason
    assert recorder.calls == []


def test_ocr_bound_blocks_third_retry_before_tool_invocation() -> None:
    """Input: three failed OCR retries. Output: abstention after exactly two calls."""

    recorder = Recorder()
    graph = BoundedAgentGraph(limits=_limits(), toolbox=_toolbox(recorder))

    result = graph.run(_context(), (_ocr("ocr-1"), _ocr("ocr-2"), _ocr("ocr-3")))

    assert isinstance(result, AbstentionTerminal)
    assert result.abstention.reason == "maximum OCR verification attempts reached"
    assert [call.call_id for call in recorder.calls] == ["ocr-1", "ocr-2"]


def test_vlm_bound_allows_primary_and_one_escalation_only() -> None:
    """Input: three failed VLM calls. Output: abstention before the third invocation."""

    recorder = Recorder()
    graph = BoundedAgentGraph(limits=_limits(), toolbox=_toolbox(recorder))

    result = graph.run(_context(), (_vlm("vlm-1"), _vlm("vlm-2"), _vlm("vlm-3")))

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
        ToolCall(f"refine-{number}", RefineCropArguments("region-1", f"crop-{number}"))
        for number in range(7)
    )

    result = BoundedAgentGraph(limits=_limits(), toolbox=toolbox).run(_context(), actions)

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
        context, (_ocr(),)
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
