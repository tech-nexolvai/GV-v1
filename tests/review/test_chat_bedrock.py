"""The reviewer-chat transport is configured and tool-only."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from app.review.chat_bedrock import TOOL_NAME, BedrockReviewerChat, _Config
from workflow.findings_composer import ComposerFinding, NarrationFact, narration_overview_context


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
                                "summary": "1 FAIL needs attention: CT-DEPTH-001 records 25 1/2 in versus 25 in.",
                                "findings": [
                                    {
                                        "finding_key": "finding-a",
                                        # The provider now returns only its sentences. The
                                        # deterministic facts are prepended by
                                        # `ground_explanations`, which is what removed the
                                        # transcription step this fixture used to imitate.
                                        "explanation": (
                                            "The vendor drawing is shallower than the approved "
                                            "design allows."
                                        ),
                                    }
                                ],
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
    assert result.summary == "1 FAIL needs attention: CT-DEPTH-001 records 25 1/2 in versus 25 in."
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


def test_prompt_requires_literal_finding_fields_not_placeholder_words() -> None:
    request = BedrockReviewerChat(
        _Config("configured-model", "us-east-1", 1, 2), _Client(_response())
    )._request((_finding(),))
    system = request["system"]
    assert isinstance(system, list)
    text = system[0]["text"]
    assert isinstance(text, str)
    # The provider is told the facts arrive without it, and that adding a number is what gets the
    # answer discarded. It is no longer told to copy anything, because it no longer can: the
    # transcription it used to be asked for is done by `ground_explanations`.
    assert "do NOT restate, copy, or summarise `required_text`" in text
    assert "one or two plain sentences" in text
    assert "CT-DEPTH-001: FAIL." not in text
    assert "Do not merely list check ids" in text
    messages = request["messages"]
    assert isinstance(messages, list)
    user_text = messages[0]["content"][0]["text"]
    assert isinstance(user_text, str)
    assert "the system places ahead of your words" in user_text
    assert "Do not copy it, quote it, or restate its values." in user_text
    overview_payload = messages[0]["content"][1]["text"]
    assert isinstance(overview_payload, str)
    assert json.loads(overview_payload) == {
        "overview_context": narration_overview_context((_finding(),))
    }
    fact_payload = messages[0]["content"][2]["text"]
    assert isinstance(fact_payload, str)
    assert json.loads(fact_payload) == [
        NarrationFact.from_finding(_finding()).model_dump(mode="json")
    ]
    assert request["inferenceConfig"] == {"temperature": 0, "maxTokens": 1024}
    tool_config = request["toolConfig"]
    assert isinstance(tool_config, dict)
    schema = tool_config["tools"][0]["toolSpec"]["inputSchema"]["json"]
    assert schema == {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "finding_key": {"type": "string"},
                        "explanation": {"type": "string"},
                    },
                    "required": ["finding_key", "explanation"],
                },
            },
        },
        "required": ["summary", "findings"],
    }


def test_transport_reserves_bounded_output_room_for_a_complete_run() -> None:
    client = _Client(_response())
    composer = BedrockReviewerChat(_Config("configured-model", "us-east-1", 1, 2), client=client)

    composer.compose((_finding(),) * 9)

    assert client.requests[0]["inferenceConfig"] == {"temperature": 0, "maxTokens": 4096}
