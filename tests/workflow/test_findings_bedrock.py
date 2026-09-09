"""The findings composer uses the configured Bedrock identity and one forced tool."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from extraction.models.nova import NovaConfig
from workflow.findings_bedrock import TOOL_NAME, BedrockFindingsComposer
from workflow.findings_composer import ComposerFinding


class _Client:
    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []

    def converse(self, **kwargs: object) -> Mapping[str, Any]:
        self.requests.append(kwargs)
        return self.response


def _config() -> NovaConfig:
    return NovaConfig(
        model_id="operator-configured-model",
        prompt_id="prompt-v1",
        template_id="template-v1",
        connect_timeout_seconds=1,
        read_timeout_seconds=2,
        max_attempts=1,
        region_name="us-east-1",
    )


def _finding() -> ComposerFinding:
    return ComposerFinding(
        key="finding-a",
        check="CAB-FILLER-001",
        check_name="Filler width",
        outcome="REVIEW_REQUIRED",
        severity="FLAG",
        reason="The required reading was not found.",
        comparison=None,
        difference=None,
        tolerance=None,
        arithmetic_unit=None,
        operands=(),
        evidence_pages=(),
        notes=(),
    )


def test_the_configured_model_and_forced_tool_are_used_without_free_text() -> None:
    response = {
        "stopReason": "tool_use",
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
                                            "CAB-FILLER-001: REVIEW_REQUIRED. The required "
                                            "reading was not found."
                                        ),
                                    }
                                ]
                            },
                        }
                    }
                ]
            }
        },
    }
    client = _Client(response)

    result = BedrockFindingsComposer(_config(), client).compose((_finding(),))

    assert result.model_id == "operator-configured-model"
    assert result.prompt_id == "prompt-v1"
    assert len(result.narratives) == 1
    request = client.requests[0]
    assert request["modelId"] == "operator-configured-model"
    tool_config = request["toolConfig"]
    assert isinstance(tool_config, dict)
    assert tool_config["toolChoice"] == {"tool": {"name": TOOL_NAME}}
    messages = request["messages"]
    assert isinstance(messages, list)
    assert all("text" in block for block in messages[0]["content"])
