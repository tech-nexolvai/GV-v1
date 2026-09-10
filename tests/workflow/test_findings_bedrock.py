"""The findings composer uses the configured Bedrock identity and one forced tool."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from app.config import Settings
from extraction.models.nova import NovaConfig
from workflow.findings_bedrock import (
    TOOL_NAME,
    BedrockFindingsComposer,
    FindingsBedrockError,
    configured_findings_composer,
)
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


def _response() -> dict[str, object]:
    return {
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


def test_the_configured_model_and_forced_tool_are_used_without_free_text() -> None:
    client = _Client(_response())

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


def test_model_text_beside_the_tool_call_is_rejected() -> None:
    response = _response()
    output = response["output"]
    assert isinstance(output, dict)
    message = output["message"]
    assert isinstance(message, dict)
    content = message["content"]
    assert isinstance(content, list)
    content.append({"text": "unstructured model prose"})

    with pytest.raises(FindingsBedrockError) as raised:
        BedrockFindingsComposer(_config(), _Client(response)).compose((_finding(),))
    assert raised.value.__cause__ is not None
    assert "exactly one findings tool call" in str(raised.value.__cause__)


def test_transport_honours_the_configured_total_attempt_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def client_factory(service: str, **kwargs: object) -> _Client:
        captured["service"] = service
        captured.update(kwargs)
        return _Client(_response())

    monkeypatch.setattr("boto3.client", client_factory)
    composer = BedrockFindingsComposer.from_environment(replace(_config(), max_attempts=3))

    composer.compose((_finding(),))

    assert captured["service"] == "bedrock-runtime"
    transport = captured["config"]
    assert transport.retries == {"total_max_attempts": 3, "mode": "standard"}  # type: ignore[attr-defined]


def test_invalid_optional_timeout_configuration_disables_only_narration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GV_BEDROCK_CONNECT_TIMEOUT", "not-a-number")

    assert configured_findings_composer() is None


def test_settings_configure_the_same_model_and_region_as_reviewer_chat() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://gv:gv@localhost:5433/gvtest",
        bedrock_model="qwen.qwen3-next-80b-a3b",
        bedrock_region="us-east-1",
        bedrock_connect_timeout=7,
        bedrock_read_timeout=19,
    )

    composer = configured_findings_composer(settings)

    assert composer is not None
    assert composer._config.model_id == "qwen.qwen3-next-80b-a3b"
    assert composer._config.region_name == "us-east-1"
    assert composer._config.connect_timeout_seconds == 7
    assert composer._config.read_timeout_seconds == 19


def test_prompt_requires_literal_finding_fields_not_placeholder_words() -> None:
    request = BedrockFindingsComposer(_config(), _Client(_response()))._request(
        (_finding(),), "operator-configured-model"
    )
    system = request["system"]
    assert isinstance(system, list)
    text = system[0]["text"]
    assert isinstance(text, str)
    assert "Never emit the placeholder words" in text
    assert "CT-DEPTH-001: FAIL." in text
    messages = request["messages"]
    assert isinstance(messages, list)
    user_text = messages[0]["content"][0]["text"]
    assert isinstance(user_text, str)
    assert "Copy that entire string character-for-character" in user_text
    fact_payload = messages[0]["content"][1]["text"]
    assert isinstance(fact_payload, str)
    assert '"required_text"' in fact_payload
