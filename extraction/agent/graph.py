"""Hard-bounded LangGraph executor for one ambiguous drawing region.

LangGraph orchestrates transitions; this module owns every safety bound. Exceeding a
step, verification or context limit produces an explicit abstention before another
tool is invoked. The only terminal values are a candidate with provenance or an
abstention requiring review.

**What happens next is asked, not scripted (#757).** Each step the graph hands a
`Planner` everything done so far — the readings each tool returned, the crop refinements
applied, the failures, the budget used — and the planner names the next tool call or
proposes one of the readings. `extraction/agent/policy.py` is the production planner: a
code-owned decision table over deterministic observations. The graph trusts nothing it is
told: every call is bounded exactly as before, and a proposal must be a reading a tool
returned in this run, so no planner can put a value in front of a reviewer that nothing
read.

Source: ``docs/DESIGN_AI.md`` section 3.2 and issue #244.
Verification: ``tests/extraction/agent/test_graph.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from evidence.candidate import ObservationCandidate
from extraction.agent.outcomes import AgentAbstention
from extraction.agent.tools import (
    AbstainArguments,
    AgentToolbox,
    OcrVerificationArguments,
    RefineCropArguments,
    ToolCall,
    ToolName,
    VlmReadingArguments,
    VlmRole,
)


def _require_text(value: object, *, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class GraphLimits:
    """Explicit graph and context bounds with no invented operational defaults."""

    max_steps: int
    max_ocr_retries: int
    max_primary_vlm_calls: int
    max_vlm_escalations: int
    max_nearby_text_items: int
    max_nearby_geometry_items: int

    def __post_init__(self) -> None:
        values = {
            "max_steps": self.max_steps,
            "max_ocr_retries": self.max_ocr_retries,
            "max_primary_vlm_calls": self.max_primary_vlm_calls,
            "max_vlm_escalations": self.max_vlm_escalations,
            "max_nearby_text_items": self.max_nearby_text_items,
            "max_nearby_geometry_items": self.max_nearby_geometry_items,
        }
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
        if not 6 <= self.max_steps <= 8:
            raise ValueError("max_steps must be between 6 and 8 inclusive")
        if not 0 <= self.max_ocr_retries <= 2:
            raise ValueError("max_ocr_retries must be between 0 and 2 inclusive")
        if self.max_primary_vlm_calls != 1:
            raise ValueError("max_primary_vlm_calls must be exactly 1")
        if not 0 <= self.max_vlm_escalations <= 1:
            raise ValueError("max_vlm_escalations must be 0 or 1")
        if self.max_nearby_text_items < 0 or self.max_nearby_geometry_items < 0:
            raise ValueError("nearby context bounds must be zero or greater")


@dataclass(frozen=True, slots=True)
class BoundedRegionContext:
    """A crop plus bounded nearby facts; full-package context is unrepresentable."""

    region_id: str
    crop_artifact_id: str
    nearby_text: tuple[str, ...]
    nearby_geometry_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.region_id, field="region_id")
        _require_text(self.crop_artifact_id, field="crop_artifact_id")
        if not isinstance(self.nearby_text, tuple) or any(
            not isinstance(item, str) for item in self.nearby_text
        ):
            raise TypeError("nearby_text must be a tuple of strings")
        if not isinstance(self.nearby_geometry_refs, tuple) or any(
            not isinstance(item, str) for item in self.nearby_geometry_refs
        ):
            raise TypeError("nearby_geometry_refs must be a tuple of strings")


@dataclass(frozen=True, slots=True)
class RefinedCrop:
    """Non-terminal result identifying the next bounded crop artifact."""

    crop_artifact_id: str

    def __post_init__(self) -> None:
        _require_text(self.crop_artifact_id, field="crop_artifact_id")


@dataclass(frozen=True, slots=True)
class RetryableToolFailure:
    """Non-terminal failure that permits the next already-bounded verification attempt."""

    reason: str

    def __post_init__(self) -> None:
        _require_text(self.reason, field="reason")


@dataclass(frozen=True, slots=True)
class CandidateTerminal:
    """Successful terminal containing one raw candidate with provenance."""

    candidate: ObservationCandidate

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, ObservationCandidate):
            raise TypeError("candidate must be an ObservationCandidate")


@dataclass(frozen=True, slots=True)
class AbstentionTerminal:
    """Fail-closed terminal routed to human review."""

    abstention: AgentAbstention

    def __post_init__(self) -> None:
        if not isinstance(self.abstention, AgentAbstention):
            raise TypeError("abstention must be an AgentAbstention")


type GraphTerminal = CandidateTerminal | AbstentionTerminal


@dataclass(frozen=True, slots=True)
class Look:
    """One reading a tool returned in this run, and which call returned it."""

    call_id: str
    tool: ToolName
    candidate: ObservationCandidate


@dataclass(frozen=True, slots=True)
class AgentProgress:
    """Everything done so far on one region — what a planner decides the next step from."""

    context: BoundedRegionContext
    steps: int
    ocr_calls: int
    primary_vlm_calls: int
    vlm_escalations: int
    current_crop_artifact_id: str
    looks: tuple[Look, ...]
    refinements: tuple[str, ...]
    """The refinement each `refine_crop` call named, in order (`RefineCropArguments.refinement`)."""

    failures: tuple[str, ...]
    attempted: tuple[str, ...]
    """Every call executed, as `tool` or `tool:detail` — `refine_crop:whole_run`,
    `request_vlm_reading:primary` — whatever it returned. A refinement that failed is attempted
    but not in `refinements`, and a planner must not ask for it again."""

    last_result: object | None
    """What the previous tool call returned, or `None` before the first."""


@dataclass(frozen=True, slots=True)
class Propose:
    """End the run with one of this run's readings. Not a tool: a model cannot choose it."""

    candidate: ObservationCandidate


@dataclass(frozen=True, slots=True)
class Halt:
    """End the run as an abstention without calling a tool — a planner with nothing left to try."""

    reason: str

    def __post_init__(self) -> None:
        _require_text(self.reason, field="reason")


type Planner = Callable[[AgentProgress], ToolCall | Propose | Halt]


def scripted(actions: tuple[ToolCall, ...]) -> Planner:
    """A planner that plays a fixed sequence, proposing the first reading any call returns.

    The shape the graph had before #757, kept for the tests that prove each bound with an exact
    sequence of calls. Production never uses it: `extraction/agent/policy.py` decides.
    """
    if not isinstance(actions, tuple) or any(not isinstance(a, ToolCall) for a in actions):
        raise TypeError("actions must be a tuple of ToolCall values")

    def plan(progress: AgentProgress) -> ToolCall | Propose | Halt:
        if progress.looks and progress.last_result is progress.looks[-1].candidate:
            return Propose(progress.looks[-1].candidate)
        taken = progress.steps
        if taken >= len(actions):
            return Halt("the bounded action sequence ended without a candidate")
        return actions[taken]

    return plan


def _attempt_label(call: ToolCall) -> str:
    """`tool` or `tool:detail`, the form `AgentProgress.attempted` records a call in."""
    arguments = call.arguments
    if isinstance(arguments, RefineCropArguments):
        return f"{call.name.value}:{arguments.refinement.value}"
    if isinstance(arguments, VlmReadingArguments):
        return f"{call.name.value}:{arguments.role.value}"
    return call.name.value


class _GraphState(TypedDict):
    context: BoundedRegionContext
    planner: Planner
    steps: int
    ocr_calls: int
    primary_vlm_calls: int
    vlm_escalations: int
    current_crop_artifact_id: str
    looks: tuple[Look, ...]
    refinements: tuple[str, ...]
    failures: tuple[str, ...]
    attempted: tuple[str, ...]
    last_result: object | None
    terminal: GraphTerminal | None


class BoundedAgentGraph:
    """Compiled LangGraph whose nodes enforce all project-owned safety limits."""

    def __init__(self, *, limits: GraphLimits, toolbox: AgentToolbox) -> None:
        if not isinstance(limits, GraphLimits):
            raise TypeError("limits must be GraphLimits")
        if not isinstance(toolbox, AgentToolbox):
            raise TypeError("toolbox must be AgentToolbox")
        self._limits = limits
        self._toolbox = toolbox

        builder = StateGraph(_GraphState)
        builder.add_node("advance", self._advance)
        builder.add_edge(START, "advance")
        builder.add_conditional_edges(
            "advance",
            self._route,
            {"continue": "advance", "end": END},
        )
        self._compiled: Any = builder.compile()

    @property
    def limits(self) -> GraphLimits:
        """The bounds this graph enforces — what a planner must plan within."""
        return self._limits

    def run(self, context: BoundedRegionContext, planner: Planner) -> GraphTerminal:
        """Ask the planner step by step, bound every call, and return one of the two terminals."""

        if not isinstance(context, BoundedRegionContext):
            raise TypeError("context must be BoundedRegionContext")
        if not callable(planner):
            raise TypeError("planner must be callable; a fixed sequence goes through `scripted`")
        initial: _GraphState = {
            "context": context,
            "planner": planner,
            "steps": 0,
            "ocr_calls": 0,
            "primary_vlm_calls": 0,
            "vlm_escalations": 0,
            "current_crop_artifact_id": context.crop_artifact_id,
            "looks": (),
            "refinements": (),
            "failures": (),
            "attempted": (),
            "last_result": None,
            "terminal": None,
        }
        completed = self._compiled.invoke(
            initial,
            {"recursion_limit": self._limits.max_steps + 4},
        )
        terminal = completed.get("terminal")
        if not isinstance(terminal, (CandidateTerminal, AbstentionTerminal)):
            raise TypeError("bounded graph ended without one of its two terminal states")
        return terminal

    @staticmethod
    def _route(state: _GraphState) -> Literal["continue", "end"]:
        return "end" if state["terminal"] is not None else "continue"

    def _abstain(self, state: _GraphState, reason: str) -> _GraphState:
        return {
            **state,
            "terminal": AbstentionTerminal(
                AgentAbstention(region_id=state["context"].region_id, reason=reason)
            ),
        }

    def _context_violation(self, context: BoundedRegionContext) -> str | None:
        if len(context.nearby_text) > self._limits.max_nearby_text_items:
            return "nearby text exceeds the configured bounded context"
        if len(context.nearby_geometry_refs) > self._limits.max_nearby_geometry_items:
            return "nearby geometry exceeds the configured bounded context"
        return None

    def _bound_violation(self, state: _GraphState, call: ToolCall) -> str | None:
        if state["steps"] >= self._limits.max_steps:
            return "maximum graph steps reached"
        if (
            isinstance(call.arguments, OcrVerificationArguments)
            and state["ocr_calls"] >= self._limits.max_ocr_retries
        ):
            return "maximum OCR verification attempts reached"
        if isinstance(call.arguments, VlmReadingArguments):
            permitted_vlm_calls = (
                self._limits.max_primary_vlm_calls + self._limits.max_vlm_escalations
            )
            completed_vlm_calls = state["primary_vlm_calls"] + state["vlm_escalations"]
            if completed_vlm_calls >= permitted_vlm_calls:
                return "maximum VLM calls reached"
            # **The roles, enforced.** One primary read; an escalation only after it (§3.2).
            if (
                call.arguments.role is VlmRole.PRIMARY
                and state["primary_vlm_calls"] >= self._limits.max_primary_vlm_calls
            ):
                return "the primary vision reader has already been asked"
            if call.arguments.role is VlmRole.ESCALATION and (
                state["primary_vlm_calls"] < self._limits.max_primary_vlm_calls
                or state["vlm_escalations"] >= self._limits.max_vlm_escalations
            ):
                return "an escalation needs a primary reading first, and there is only one"
        return None

    @staticmethod
    def _references_current_region(state: _GraphState, call: ToolCall) -> bool:
        arguments = call.arguments
        if arguments.region_id != state["context"].region_id:
            return False
        if isinstance(arguments, AbstainArguments):
            return True
        return arguments.crop_artifact_id == state["current_crop_artifact_id"]

    def _increment(self, state: _GraphState, call: ToolCall) -> _GraphState:
        updated: _GraphState = {
            **state,
            "steps": state["steps"] + 1,
            "attempted": (*state["attempted"], _attempt_label(call)),
        }
        if isinstance(call.arguments, OcrVerificationArguments):
            updated["ocr_calls"] += 1
        elif isinstance(call.arguments, VlmReadingArguments):
            if call.arguments.role is VlmRole.PRIMARY:
                updated["primary_vlm_calls"] += 1
            else:
                updated["vlm_escalations"] += 1
        return updated

    @staticmethod
    def _progress(state: _GraphState) -> AgentProgress:
        return AgentProgress(
            context=state["context"],
            steps=state["steps"],
            ocr_calls=state["ocr_calls"],
            primary_vlm_calls=state["primary_vlm_calls"],
            vlm_escalations=state["vlm_escalations"],
            current_crop_artifact_id=state["current_crop_artifact_id"],
            looks=state["looks"],
            refinements=state["refinements"],
            failures=state["failures"],
            attempted=state["attempted"],
            last_result=state["last_result"],
        )

    def _advance(self, state: _GraphState) -> _GraphState:
        context_error = self._context_violation(state["context"])
        if context_error is not None:
            return self._abstain(state, context_error)

        try:
            decision = state["planner"](self._progress(state))
        # A planner that fails closes toward abstention, like a tool that fails: an exception
        # leaking out would be a third terminal behavior.
        except Exception as error:  # noqa: BLE001
            return self._abstain(state, f"the planner failed: {type(error).__name__}")

        if isinstance(decision, Halt):
            return self._abstain(state, decision.reason)
        if isinstance(decision, Propose):
            # **Only a reading this run produced.** Identity, not equality: a planner cannot
            # construct a candidate that happens to look like one a tool returned.
            if not any(look.candidate is decision.candidate for look in state["looks"]):
                return self._abstain(state, "the planner proposed a reading no tool returned")
            return {**state, "terminal": CandidateTerminal(decision.candidate)}
        if not isinstance(decision, ToolCall):
            return self._abstain(state, "the planner named neither a tool call nor a proposal")

        call = decision
        bound_error = self._bound_violation(state, call)
        if bound_error is not None:
            return self._abstain(state, bound_error)
        if not self._references_current_region(state, call):
            return self._abstain(state, "tool call does not reference the current bounded region")

        updated = self._increment(state, call)
        try:
            result = self._toolbox.invoke(call)
        # Any external handler failure must close toward abstention; allowing an
        # unexpected exception to leak would create a third terminal behavior.
        except Exception as error:  # noqa: BLE001
            return self._abstain(updated, f"tool execution failed: {type(error).__name__}")

        updated["last_result"] = result
        if isinstance(result, ObservationCandidate):
            return {
                **updated,
                "looks": (*updated["looks"], Look(call.call_id, call.name, result)),
            }
        if isinstance(result, AgentAbstention):
            return {**updated, "terminal": AbstentionTerminal(result)}
        if isinstance(result, RefinedCrop):
            refinement = (
                call.arguments.refinement
                if isinstance(call.arguments, RefineCropArguments)
                else "unknown"
            )
            return {
                **updated,
                "current_crop_artifact_id": result.crop_artifact_id,
                "refinements": (*updated["refinements"], str(refinement)),
            }
        if isinstance(result, RetryableToolFailure):
            return {**updated, "failures": (*updated["failures"], result.reason)}
        return self._abstain(updated, "tool returned an unsupported result type")
