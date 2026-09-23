"""Closed-question page-layout classification for issue #654."""

from __future__ import annotations

import pathlib
import tempfile
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml  # type: ignore[import-untyped]

from evidence.crop import CropSpec, CropStatus, RenderedPage, generate_crop
from evidence.polygon import Polygon
from extraction.layout import (
    BedrockClosedQuestionConfig,
    BedrockClosedQuestionReader,
    ClosedQuestion,
    LayoutReaderResult,
    LayoutStatus,
    classify_layout,
    full_page_region,
    question_from_discriminator,
)
from rules.required_inputs import required_inputs
from rules.schema import Rule
from storage.local import LocalStore

DOCUMENT = UUID("22222222-2222-4222-8222-222222222222")
RULEBOOK = pathlib.Path(__file__).resolve().parents[2] / "rules" / "rulebook"


class FakeReader:
    def __init__(self, result: LayoutReaderResult) -> None:
        self.result = result

    def read(self, rendered: RenderedPage, question: ClosedQuestion) -> LayoutReaderResult:
        del rendered, question
        return self.result


class RecordingBedrock:
    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []

    def converse(self, **kwargs: object) -> Mapping[str, Any]:
        self.requests.append(kwargs)
        return self.response


def _rendered() -> RenderedPage:
    return RenderedPage(
        document_version_id=DOCUMENT,
        page_index=0,
        page_content_hash="0" * 64,
        rotation=0,
        render_failed=False,
        width_px=10,
        height_px=8,
        dpi=72,
        rgb_bytes=b"\xff" * 10 * 8 * 3,
    )


def _question(*choices: str) -> ClosedQuestion:
    return ClosedQuestion(
        name="wall_config",
        prompt="Which wall configuration is shown?",
        choices=choices or ("back_left_right", "back_only", "island"),
    )


def _reading(answer: str | None, *, reader: str = "reader-a") -> LayoutReaderResult:
    return LayoutReaderResult(
        reader=reader,
        answer=answer,
        region=full_page_region(_rendered()),
        reason=(
            "visible in the title block" if answer is not None else "wall symbols are not legible"
        ),
    )


def _tool_response(payload: object) -> dict[str, Any]:
    return {
        "stopReason": "tool_use",
        "output": {
            "message": {
                "content": [
                    {
                        "toolUse": {
                            "name": "answer_closed_layout_question",
                            "toolUseId": "call-1",
                            "input": payload,
                        }
                    }
                ]
            }
        },
    }


def test_answer_outside_the_permitted_set_is_refused_not_coerced() -> None:
    result = classify_layout(
        _rendered(),
        _question("back_left_right", "back_only", "island"),
        [FakeReader(_reading("back-left-right"))],
    )

    assert result.status is LayoutStatus.ABSTAINED
    assert result.answer is None
    assert "outside the permitted set" in result.reason
    assert "back-left-right" in result.reason


def test_abstention_is_a_first_class_outcome_with_a_reason() -> None:
    result = classify_layout(_rendered(), _question(), [FakeReader(_reading(None))])

    assert result.status is LayoutStatus.ABSTAINED
    assert result.answer is None
    assert "wall symbols are not legible" in result.reason
    assert result.readings[0].region == result.region


def test_returned_region_renders_to_a_reviewer_crop() -> None:
    result = classify_layout(_rendered(), _question(), [FakeReader(_reading("back_only"))])

    assert isinstance(result.region, Polygon)
    with tempfile.TemporaryDirectory() as directory:
        store = LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")
        crop = generate_crop(
            _rendered(),
            CropSpec(result.region, context_margin_pt=Decimal(1), dpi=72),
            store,
        )

        assert crop.status is CropStatus.AVAILABLE
        assert crop.artifact is not None


def test_multiple_reader_disagreement_surfaces_without_confidence_resolution() -> None:
    result = classify_layout(
        _rendered(),
        _question(),
        [
            FakeReader(_reading("back_only", reader="nova")),
            FakeReader(_reading("island", reader="haiku")),
        ],
    )

    assert result.status is LayoutStatus.DISAGREEMENT
    assert result.answer is None
    assert "not resolved by confidence" in result.reason
    assert "nova='back_only'" in result.reason
    assert "haiku='island'" in result.reason


def test_no_answer_is_written_to_a_table_by_the_classifier() -> None:
    result = classify_layout(_rendered(), _question(), [FakeReader(_reading("island"))])

    assert result.status is LayoutStatus.ANSWERED
    assert result.answer == "island"
    assert result.readings[0].answer == "island"


def test_bedrock_reader_constrains_the_tool_answer_to_rulebook_choices() -> None:
    client = RecordingBedrock(
        _tool_response(
            {
                "answer": "island",
                "reason": "the plan has no back wall",
                "region": [["0", "0"], ["1", "0"], ["1", "1"], ["0", "1"]],
            }
        )
    )
    reader = BedrockClosedQuestionReader(
        BedrockClosedQuestionConfig(
            model_id="amazon.nova-lite-v1:0",
            prompt_id="layout-discriminator-v1",
            template_id="closed-question-page-v1",
        ),
        client,
    )

    reading = reader.read(_rendered(), _question("back_left_right", "back_only", "island"))

    assert reading.answer == "island"
    request = client.requests[0]
    tool_config = request["toolConfig"]
    assert isinstance(tool_config, dict)
    tools = tool_config["tools"]
    assert isinstance(tools, list)
    first_tool = tools[0]
    assert isinstance(first_tool, dict)
    tool_spec = first_tool["toolSpec"]
    assert isinstance(tool_spec, dict)
    input_schema = tool_spec["inputSchema"]
    assert isinstance(input_schema, dict)
    schema = input_schema["json"]
    assert isinstance(schema, dict)
    assert schema["properties"]["answer"]["enum"] == ["back_left_right", "back_only", "island"]


def test_permitted_answers_come_from_required_inputs_not_a_hardcoded_list() -> None:
    source = yaml.safe_load((RULEBOOK / "ct_width_001.yaml").read_text(encoding="utf-8"))
    source["id"] = "CT-WIDTH-SYNTHETIC"
    source["applicability"]["variants"].append(
        {"when": "back_left_right_peninsula", "extras": {"field_cut_count": 3}}
    )
    rule = Rule.model_validate(source)
    needs = required_inputs([rule])
    question = question_from_discriminator(needs.discriminators[0])

    result = classify_layout(
        _rendered(),
        question,
        [FakeReader(_reading("back_left_right_peninsula"))],
    )

    assert "back_left_right_peninsula" in question.choices
    assert result.status is LayoutStatus.ANSWERED
    assert result.answer == "back_left_right_peninsula"
