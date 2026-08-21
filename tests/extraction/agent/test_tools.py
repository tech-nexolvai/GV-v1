"""Fixed agent capability and replay-record tests for issue #245."""

from __future__ import annotations

import ast
import inspect
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import extraction.agent.tools as tools_module
from extraction.agent.tools import (
    PERMITTED_TOOLS,
    AbstainArguments,
    AgentToolbox,
    OcrVerificationArguments,
    RefineCropArguments,
    ToolCall,
    ToolCallRecord,
    ToolName,
    VlmReadingArguments,
)


class RecordingCalls:
    def __init__(self) -> None:
        self.items: list[ToolCallRecord] = []

    def record(self, call: ToolCallRecord) -> None:
        self.items.append(call)


class Handler:
    def __init__(self, result: object) -> None:
        self.result = result
        self.arguments: list[object] = []

    def __call__(self, arguments: object) -> object:
        self.arguments.append(arguments)
        return self.result


def _toolbox() -> tuple[AgentToolbox, RecordingCalls, dict[ToolName, Handler]]:
    recorder = RecordingCalls()
    handlers = {
        ToolName.REFINE_CROP: Handler("refined"),
        ToolName.REQUEST_OCR_VERIFICATION: Handler("ocr"),
        ToolName.REQUEST_VLM_READING: Handler("vlm"),
        ToolName.ABSTAIN: Handler("abstained"),
    }
    toolbox = AgentToolbox(
        refine_crop=handlers[ToolName.REFINE_CROP],
        request_ocr_verification=handlers[ToolName.REQUEST_OCR_VERIFICATION],
        request_vlm_reading=handlers[ToolName.REQUEST_VLM_READING],
        abstain=handlers[ToolName.ABSTAIN],
        recorder=recorder,
    )
    return toolbox, recorder, handlers


@pytest.mark.parametrize(
    ("arguments", "name", "result"),
    [
        (RefineCropArguments("region", "crop-1"), ToolName.REFINE_CROP, "refined"),
        (
            OcrVerificationArguments("region", "crop-1"),
            ToolName.REQUEST_OCR_VERIFICATION,
            "ocr",
        ),
        (VlmReadingArguments("region", "crop-1"), ToolName.REQUEST_VLM_READING, "vlm"),
        (AbstainArguments("region", "no reliable reading"), ToolName.ABSTAIN, "abstained"),
    ],
)
def test_each_permitted_call_is_recorded_and_dispatched(
    arguments: object, name: ToolName, result: object
) -> None:
    """Input: permitted typed call. Outcome: recorded execution. Why: every action is replayable."""

    toolbox, recorder, handlers = _toolbox()
    call = ToolCall("call-1", arguments)  # type: ignore[arg-type]

    actual = toolbox.invoke(call)

    assert actual == result
    assert recorder.items == [ToolCallRecord("call-1", name, call.arguments)]
    assert handlers[name].arguments == [arguments]


def test_call_is_recorded_before_a_handler_failure() -> None:
    """Input: failing handler. Outcome: retained call. Why: failed side effects must be replayable."""

    class FailingHandler:
        def __call__(self, arguments: RefineCropArguments) -> object:
            del arguments
            raise RuntimeError("crop service failed")

    toolbox, recorder, handlers = _toolbox()
    del handlers
    toolbox = AgentToolbox(
        refine_crop=FailingHandler(),
        request_ocr_verification=Handler("ocr"),
        request_vlm_reading=Handler("vlm"),
        abstain=Handler("abstained"),
        recorder=recorder,
    )
    arguments = RefineCropArguments("region", "crop-1")

    with pytest.raises(RuntimeError, match="crop service failed"):
        toolbox.invoke(ToolCall("failed-call", arguments))

    assert recorder.items == [ToolCallRecord("failed-call", ToolName.REFINE_CROP, arguments)]


def test_allow_list_is_exact_and_immutable() -> None:
    """Input: constructed toolbox. Outcome: four fixed names. Why: runtime cannot add authority."""

    toolbox, _, _ = _toolbox()

    assert toolbox.permitted_tools is PERMITTED_TOOLS
    assert toolbox.permitted_tools == frozenset(
        {
            ToolName.REFINE_CROP,
            ToolName.REQUEST_OCR_VERIFICATION,
            ToolName.REQUEST_VLM_READING,
            ToolName.ABSTAIN,
        }
    )
    with pytest.raises(AttributeError):
        toolbox.permitted_tools.add("approve_package")  # type: ignore[attr-defined]


def test_handlers_cannot_be_replaced_or_added_after_construction() -> None:
    """Input: runtime mutation. Outcome: rejection. Why: agent cannot widen its own surface."""

    toolbox, _, _ = _toolbox()

    with pytest.raises(AttributeError, match="fixed at construction"):
        toolbox._refine_crop = Handler("replacement")
    with pytest.raises(AttributeError, match="fixed at construction"):
        toolbox.approve_package = Handler("approved")
    assert not hasattr(toolbox, "register")
    assert not hasattr(toolbox, "add_tool")
    assert not hasattr(toolbox, "update")


def test_typed_arguments_are_immutable_and_reject_missing_identity() -> None:
    """Input: mutation/empty reference. Outcome: rejection. Why: replay arguments stay exact."""

    arguments = RefineCropArguments("region", "crop-1")
    with pytest.raises(FrozenInstanceError):
        arguments.region_id = "other"  # type: ignore[misc]
    with pytest.raises(ValueError, match="crop_artifact_id"):
        RefineCropArguments("region", "")


def test_untyped_call_cannot_enter_the_dispatcher() -> None:
    """Input: model-shaped dictionary. Outcome: type error. Why: raw output is not executable."""

    toolbox, recorder, _ = _toolbox()

    with pytest.raises(TypeError, match="ToolCall"):
        toolbox.invoke({"tool": "abstain"})  # type: ignore[arg-type]
    assert recorder.items == []


def test_agent_reachable_surface_contains_no_prohibited_capability() -> None:
    """Input: module and public toolbox surface. Outcome: no decision authority is reachable."""

    prohibited = {
        "select_rule",
        "modify_tolerance",
        "set_tolerance",
        "approve",
        "approve_package",
        "write_verdict",
        "pass_finding",
        "fail_finding",
    }
    module_names = set(vars(tools_module))
    public_calls = {
        name
        for name, member in inspect.getmembers(AgentToolbox)
        if not name.startswith("_") and callable(member)
    }

    assert prohibited.isdisjoint(module_names)
    assert prohibited.isdisjoint(public_calls)
    assert public_calls == {"invoke"}


def test_import_guard_keeps_rules_verdict_and_approval_out_of_tools_module() -> None:
    """Input: tools.py imports. Outcome: prohibited layers absent. Why: refusal checks are weaker."""

    source_path = Path(tools_module.__file__).resolve()
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_roots.add(node.module.split(".", maxsplit=1)[0])

    assert {"rules", "verdict", "app", "retrieval"}.isdisjoint(imported_roots)
