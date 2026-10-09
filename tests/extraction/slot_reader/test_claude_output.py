"""The Claude readers' answer shapes, strict answer check and picture limits (#1051).

Verification for `extraction/slot_reader/claude_output.py`. No network, no client values.
"""

from __future__ import annotations

import json
from typing import Any, get_args

import pytest

from evidence.crop import encode_png
from extraction.form_reader.bedrock import MalformedFormAnswer
from extraction.slot_reader.bedrock import _CounterBreakReply
from extraction.slot_reader.claude_output import (
    CLAUDE_EFFORTS,
    COUNTER_BREAK_SCHEMA,
    CROP_SCHEMA,
    DEFAULT_CLAUDE_EFFORT,
    ROW_CHOICE_SCHEMA,
    SPAN_SCHEMA,
    WALL_SCHEMA,
    PictureWouldBeResized,
    claude_answer,
    output_config,
    picture_fits,
    png_size,
    require_picture_fits,
    visual_tokens,
)
from extraction.slot_reader.walls import Side

OPUS = "anthropic.claude-opus-5-5"
SONNET = "anthropic.claude-sonnet-5-5"
SCHEMAS = {
    "row": ROW_CHOICE_SCHEMA,
    "crop": CROP_SCHEMA,
    "span": SPAN_SCHEMA,
    "counter_break": COUNTER_BREAK_SCHEMA,
    "walls": WALL_SCHEMA,
}
SPAN_ANSWER = {
    "belongs": True,
    "text": '3"+2" Filler',
    "stacked": False,
    "combined": True,
    "readable": True,
    "no_dimension": False,
}


def reply(text: str, stop: str | None = "end_turn") -> dict[str, Any]:
    response: dict[str, Any] = {"output": {"message": {"content": [{"text": text}]}}}
    if stop is not None:
        response["stopReason"] = stop
    return response


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_every_answer_shape_is_a_closed_object_with_every_key_required(name: str) -> None:
    schema = SCHEMAS[name]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_no_answer_shape_constrains_text_with_a_pattern_or_length(name: str) -> None:
    """Structure only: a pattern on the copied label would make the model drop the words the
    holds read (`Filler`, `(10EQ)`, `INCLUDING FIELD CUT`, `VIF`)."""
    serialised = json.dumps(SCHEMAS[name])
    for unsupported_or_forbidden in ("pattern", "minLength", "maxLength", "minimum", "maximum"):
        assert unsupported_or_forbidden not in serialised


def test_the_shapes_match_the_parsers_word_lists() -> None:
    stone_ends = COUNTER_BREAK_SCHEMA["properties"]["stone_ends"]["enum"]
    annotation = _CounterBreakReply.model_fields["stone_ends"].annotation
    assert set(stone_ends) == set(get_args(annotation))
    for side in ("left", "right", "behind"):
        assert set(WALL_SCHEMA["properties"][side]["enum"]) == {item.value for item in Side}
    assert set(WALL_SCHEMA["properties"]["view"]["enum"]) == {"elevation", "plan", "other"}
    assert SPAN_SCHEMA["properties"]["text"] == {"type": "string"}
    assert ROW_CHOICE_SCHEMA["properties"]["row"] == {"type": "integer"}


def test_output_config_states_the_schema_and_the_effort() -> None:
    assert output_config(SPAN_SCHEMA, "high") == {
        "format": {"type": "json_schema", "schema": SPAN_SCHEMA},
        "effort": "high",
    }
    assert DEFAULT_CLAUDE_EFFORT == "high"
    assert CLAUDE_EFFORTS == ("low", "medium", "high", "xhigh", "max")
    with pytest.raises(ValueError, match="effort"):
        output_config(SPAN_SCHEMA, "adaptive")


def test_a_finished_answer_of_the_right_shape_is_returned_with_its_words_intact() -> None:
    assert claude_answer(reply(json.dumps(SPAN_ANSWER)), SPAN_SCHEMA) == SPAN_ANSWER


@pytest.mark.parametrize("stop", ["refusal", "max_tokens", "model_mismatch", None])
def test_an_answer_that_did_not_finish_its_turn_is_malformed(stop: str | None) -> None:
    with pytest.raises(MalformedFormAnswer):
        claude_answer(reply(json.dumps(SPAN_ANSWER), stop), SPAN_SCHEMA)


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "```json\n" + json.dumps(SPAN_ANSWER) + "\n```",
        json.dumps(SPAN_ANSWER) + " and some words",
        json.dumps({key: value for key, value in SPAN_ANSWER.items() if key != "belongs"}),
        json.dumps(SPAN_ANSWER | {"why": "an extra key"}),
        json.dumps(SPAN_ANSWER | {"belongs": "true"}),
        json.dumps(SPAN_ANSWER | {"text": None}),
        json.dumps([SPAN_ANSWER]),
    ],
)
def test_an_answer_that_misses_its_shape_is_malformed_never_coerced(text: str) -> None:
    with pytest.raises(MalformedFormAnswer):
        claude_answer(reply(text), SPAN_SCHEMA)


def test_a_word_outside_a_closed_list_or_a_boolean_row_is_malformed() -> None:
    with pytest.raises(MalformedFormAnswer):
        claude_answer(
            reply(json.dumps({"contains_tall_appliance": False, "stone_ends": "maybe", "why": ""})),
            COUNTER_BREAK_SCHEMA,
        )
    with pytest.raises(MalformedFormAnswer):
        claude_answer(reply(json.dumps({"row": True, "why": ""})), ROW_CHOICE_SCHEMA)


def test_png_size_reads_the_header_and_refuses_what_is_not_a_png() -> None:
    assert png_size(encode_png(7, 3, bytes(7 * 3 * 3))) == (7, 3)
    with pytest.raises(PictureWouldBeResized):
        png_size(b"not a picture")


@pytest.mark.parametrize(
    ("size", "fits"),
    [
        # The documented table (vision.md, high-resolution tier): not resized ...
        ((1000, 1000), True),
        ((1920, 1080), True),
        ((2000, 1500), True),
        # ... and resized.
        ((3840, 2160), False),
        # The long edge, padded to a whole 28 px tile, at most 2576.
        ((2576, 100), True),
        ((2577, 100), False),
        ((100, 2577), False),
        # The visual-token budget: 72 x 72 tiles is over 4784.
        ((2016, 2016), False),
        ((1932, 1932), True),
    ],
)
def test_the_documented_high_resolution_limits(size: tuple[int, int], fits: bool) -> None:
    assert picture_fits(*size, OPUS) is fits
    assert picture_fits(*size, SONNET) is fits


def test_an_unknown_claude_model_is_held_to_the_stricter_standard_tier() -> None:
    assert picture_fits(1092, 1092, "anthropic.claude-other") is True
    assert picture_fits(1920, 1080, "anthropic.claude-other") is False


def test_visual_tokens_use_whole_28_px_tiles() -> None:
    assert visual_tokens(200, 200) == 64
    assert visual_tokens(1000, 1000) == 1296
    assert visual_tokens(1920, 1080) == 2691


def test_a_picture_that_would_be_resized_is_refused_with_its_reason() -> None:
    small = encode_png(10, 10, bytes(300))
    wide = encode_png(2600, 2, bytes(2600 * 2 * 3))
    require_picture_fits((small,), OPUS)
    with pytest.raises(PictureWouldBeResized, match="picture 2 is 2600x2 px"):
        require_picture_fits((small, wide), OPUS)
