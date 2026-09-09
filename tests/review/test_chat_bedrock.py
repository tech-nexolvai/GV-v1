"""The reviewer-chat transport is configured and tool-only."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from app.review.chat_bedrock import TOOL_NAME, BedrockReviewerChat, _Config
from workflow.findings_composer import ComposerFinding


class _Client:
    def __init__(self, response: Mapping[str, Any]) -> None:
        self._response = response
        self.requests: list[dict[str, object]] = []

    def converse(self, **kwargs: object) -> Mapping[str, Any]:
        self.requests.append(kwargs)
        return self._response


def _finding() -> ComposerFinding:
    return ComposerFinding(
        key="finding-a",
        check="CT-DEPTH-001",
        check_name="Countertop depth",
        outcome="FAIL",
        severity="FLAG",
        reason="The values differ.",
        comparison="25 1/2 in vs 25 in",
        difference=None,
        tolerance=None,
        arithmetic_unit="in",
        operands=(),
        evidence_pages=("13",),
        notes=(),
    )


def _response() -> dict[str, object]:
    return {
        "output": {
            "message": {
                "content": [
                    {
                        "toolUse": {
                            "name": TOOL_NAME,
                            "input": {
                                "findings": [
                                    {
                                        "finding_key": "finding-a",
                                        "text": (
                                            "CT-DEPTH-001: FAIL. Checked Countertop depth. "
                                            "Severity: FLAG. Why: The values differ. "
                                            "Comparison: 25 1/2 in vs 25 in. "
                                            "Arithmetic unit: in. Evidence pages: 13."
                                        ),
                                    }
                                ]
                            },
                        }
                    }
                ]
            }
        }
    }


def test_transport_uses_the_configured_provider_and_forces_its_only_tool() -> None:
    client = _Client(_response())
    composer = BedrockReviewerChat(_Config("configured-model", "us-east-1", 1, 2), client=client)

    result = composer.compose((_finding(),))

    assert result.model_id == "configured-model"
    request = client.requests[0]
    assert request["modelId"] == "configured-model"
    tool_config = request["toolConfig"]
    assert isinstance(tool_config, dict)
    assert tool_config["toolChoice"] == {"tool": {"name": TOOL_NAME}}


def test_transport_refuses_model_free_text_alongside_a_tool_call() -> None:
    response = _response()
    message = response["output"]["message"]  # type: ignore[index]
    content = message["content"]  # type: ignore[index]
    content.append({"text": "free-form answer"})  # type: ignore[union-attr]
    composer = BedrockReviewerChat(
        _Config("configured-model", "us-east-1", 1, 2), _Client(response)
    )

    with pytest.raises(RuntimeError, match="no free text"):
        composer.compose((_finding(),))
