"""Fixed capability surface for the bounded extraction agent.

Only four extraction actions are reachable. Rule selection, tolerance changes,
approval and verdict writing are absent rather than represented as denied handlers.
The toolbox has no registration API, and every attempted call is recorded with its
typed arguments before the handler runs.

Source: ``docs/DESIGN_AI.md`` section 3.3 and issue #245.
Verification: ``tests/extraction/agent/test_tools.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ToolName(StrEnum):
    """The complete set of capabilities available to the agent."""

    REFINE_CROP = "refine_crop"
    REQUEST_OCR_VERIFICATION = "request_ocr_verification"
    REQUEST_VLM_READING = "request_vlm_reading"
    ABSTAIN = "abstain"


PERMITTED_TOOLS: frozenset[ToolName] = frozenset(ToolName)


def _require_text(value: object, *, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class RefineCropArguments:
    """References for applying the system-owned bounded crop refinement policy."""

    region_id: str
    crop_artifact_id: str

    def __post_init__(self) -> None:
        _require_text(self.region_id, field="region_id")
        _require_text(self.crop_artifact_id, field="crop_artifact_id")


@dataclass(frozen=True, slots=True)
class OcrVerificationArguments:
    """References for requesting one independent OCR verification route."""

    region_id: str
    crop_artifact_id: str

    def __post_init__(self) -> None:
        _require_text(self.region_id, field="region_id")
        _require_text(self.crop_artifact_id, field="crop_artifact_id")


@dataclass(frozen=True, slots=True)
class VlmReadingArguments:
    """References for requesting one model reading of the bounded crop."""

    region_id: str
    crop_artifact_id: str

    def __post_init__(self) -> None:
        _require_text(self.region_id, field="region_id")
        _require_text(self.crop_artifact_id, field="crop_artifact_id")


@dataclass(frozen=True, slots=True)
class AbstainArguments:
    """The explicit reason the agent stopped without producing a candidate."""

    region_id: str
    reason: str

    def __post_init__(self) -> None:
        _require_text(self.region_id, field="region_id")
        _require_text(self.reason, field="reason")


type ToolArguments = (
    RefineCropArguments | OcrVerificationArguments | VlmReadingArguments | AbstainArguments
)


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One typed request selected from the fixed tool surface."""

    call_id: str
    arguments: ToolArguments

    def __post_init__(self) -> None:
        _require_text(self.call_id, field="call_id")
        if not isinstance(
            self.arguments,
            (RefineCropArguments, OcrVerificationArguments, VlmReadingArguments, AbstainArguments),
        ):
            raise TypeError("arguments must be one of the permitted tool argument types")

    @property
    def name(self) -> ToolName:
        """Derive the tool name from the argument type, never from model text."""

        if isinstance(self.arguments, RefineCropArguments):
            return ToolName.REFINE_CROP
        if isinstance(self.arguments, OcrVerificationArguments):
            return ToolName.REQUEST_OCR_VERIFICATION
        if isinstance(self.arguments, VlmReadingArguments):
            return ToolName.REQUEST_VLM_READING
        return ToolName.ABSTAIN


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """Replayable record written before a permitted handler executes."""

    call_id: str
    tool: ToolName
    arguments: ToolArguments


class ToolCallRecorder(Protocol):
    """Persistence boundary for replayable agent tool calls."""

    def record(self, call: ToolCallRecord) -> None:
        """Persist one immutable tool call record."""


class RefineCropHandler(Protocol):
    def __call__(self, arguments: RefineCropArguments) -> object:
        """Apply bounded crop refinement."""


class OcrVerificationHandler(Protocol):
    def __call__(self, arguments: OcrVerificationArguments) -> object:
        """Run one OCR verification route."""


class VlmReadingHandler(Protocol):
    def __call__(self, arguments: VlmReadingArguments) -> object:
        """Run one bounded model-reading route."""


class AbstainHandler(Protocol):
    def __call__(self, arguments: AbstainArguments) -> object:
        """Produce an explicit abstention result."""


class AgentToolbox:
    """Immutable-at-construction dispatch for exactly four permitted tools.

    Handlers are supplied through four named parameters rather than a mapping, so a
    caller cannot add a fifth capability. There is deliberately no ``register``,
    ``update`` or mutable handler collection.
    """

    __slots__ = (
        "_abstain",
        "_ocr_verification",
        "_recorder",
        "_refine_crop",
        "_sealed",
        "_vlm_reading",
    )

    _refine_crop: RefineCropHandler
    _ocr_verification: OcrVerificationHandler
    _vlm_reading: VlmReadingHandler
    _abstain: AbstainHandler
    _recorder: ToolCallRecorder
    _sealed: bool

    def __init__(
        self,
        *,
        refine_crop: RefineCropHandler,
        request_ocr_verification: OcrVerificationHandler,
        request_vlm_reading: VlmReadingHandler,
        abstain: AbstainHandler,
        recorder: ToolCallRecorder,
    ) -> None:
        object.__setattr__(self, "_refine_crop", refine_crop)
        object.__setattr__(self, "_ocr_verification", request_ocr_verification)
        object.__setattr__(self, "_vlm_reading", request_vlm_reading)
        object.__setattr__(self, "_abstain", abstain)
        object.__setattr__(self, "_recorder", recorder)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("AgentToolbox is fixed at construction")
        object.__setattr__(self, name, value)

    @property
    def permitted_tools(self) -> frozenset[ToolName]:
        """Return the immutable, code-owned capability set."""

        return PERMITTED_TOOLS

    def invoke(self, call: ToolCall) -> object:
        """Record and execute one typed call from the fixed capability set."""

        if not isinstance(call, ToolCall):
            raise TypeError("call must be a ToolCall")
        self._recorder.record(ToolCallRecord(call.call_id, call.name, call.arguments))
        if isinstance(call.arguments, RefineCropArguments):
            return self._refine_crop(call.arguments)
        if isinstance(call.arguments, OcrVerificationArguments):
            return self._ocr_verification(call.arguments)
        if isinstance(call.arguments, VlmReadingArguments):
            return self._vlm_reading(call.arguments)
        return self._abstain(call.arguments)
