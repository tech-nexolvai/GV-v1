"""The fixed answer shapes, effort and picture limits for the two Claude readers (#1051).

**Why this exists.** The Claude readers used to answer in free text that the slot reader then
searched for a JSON object, at the API's default effort (`medium` on Opus 5.5), with pictures the
API could shrink without saying so. Each is a documented source of a garbled or wrong reading.
This module states, once:

- **the answer shape of every Claude question** as a JSON schema, sent as the request's structured
  output (`output_config.format`). Structure only: booleans, integers, closed word lists. The
  copied label text is never constrained by a pattern — the prompt asks the model to copy words
  such as `Filler`, `(10EQ)`, `INCLUDING FIELD CUT` or `VIF` exactly, and the holds that keep a
  wrong PASS out read those words. A pattern would make the model drop them.
- **the strict check of an answer against that shape.** An answer that stopped for any reason but
  the end of its turn (a refusal, the token limit), or whose text is not exactly one JSON object
  of the stated shape, is malformed: the caller's existing path re-asks once and then abstains,
  which sends the question to the reviewer. It is never a value.
- **the effort level**, always sent, so a run never depends on a model's changing default.
- **the documented picture limits** (long edge 2576 px and 4784 visual tokens of 28 x 28 px on the
  high-resolution tier, which both readers are on; 1568/1568 otherwise). A picture the API would
  resize is refused here, before any call, and the question abstains with that reason. The sizes
  the slot reader draws are not changed by this module.

Sources: https://platform.claude.com/docs/en/build-with-claude/structured-outputs ,
.../effort , .../vision , .../vision-coordinates (read 2026-10-09).
Verification: `tests/extraction/slot_reader/test_claude_output.py`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal, get_args

from extraction.form_reader.bedrock import MalformedFormAnswer, _response_text

__all__ = [
    "ARCH_MEASURES",
    "ARCH_PAIR_SCHEMA",
    "CLAUDE_EFFORTS",
    "COUNTER_BREAK_SCHEMA",
    "CROP_SCHEMA",
    "DEFAULT_CLAUDE_EFFORT",
    "OUTPUT_CONFIG_KEY",
    "ROW_CHOICE_SCHEMA",
    "SPAN_SCHEMA",
    "WALL_SCHEMA",
    "ClaudeEffort",
    "PictureWouldBeResized",
    "claude_answer",
    "is_claude_model",
    "output_config",
    "picture_fits",
    "png_size",
    "require_picture_fits",
    "visual_tokens",
]

type ClaudeEffort = Literal["low", "medium", "high", "xhigh", "max"]
CLAUDE_EFFORTS: Final[tuple[str, ...]] = get_args(ClaudeEffort.__value__)
#: Accuracy first (admin, 2026-10-08: AI cost is not a constraint for V1). Opus 5.5's own default
#: would be `medium`; Sonnet 5.5's is `high`. Stated on every call so the two never drift apart.
DEFAULT_CLAUDE_EFFORT: Final[ClaudeEffort] = "high"

#: The key a slot-reader request carries its Claude output settings under. Only the Claude
#: adapters read it (Anthropic's API, or OpenRouter since #1094); a Bedrock (non-Claude) request
#: never has it.
OUTPUT_CONFIG_KEY: Final = "anthropicOutputConfig"

_MODEL_PREFIX: Final = "anthropic."
#: Models on the documented high-resolution tier (Claude 4.7 and later). Any other Claude model is
#: held to the stricter standard tier, so an unknown model can only refuse more pictures.
_HIGH_RESOLUTION_MODELS: Final = frozenset({"claude-opus-5-5", "claude-sonnet-5-5"})
_TILE_PX: Final = 28
_HIGH_RESOLUTION_LIMITS: Final = (2576, 4784)
_STANDARD_LIMITS: Final = (1568, 1568)
#: The Messages API's per-image limit, base64-encoded (Claude API direct).
_MAX_BASE64_BYTES: Final = 10 * 1024 * 1024
_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"


def is_claude_model(model_id: str) -> bool:
    """Whether a reader id names one of Anthropic's models, called through the Messages API."""
    return model_id.startswith(_MODEL_PREFIX)


def _object(properties: Mapping[str, Mapping[str, object]]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {name: dict(schema) for name, schema in properties.items()},
        "required": list(properties),
        "additionalProperties": False,
    }


_BOOLEAN: Final = {"type": "boolean"}
_STRING: Final = {"type": "string"}

#: Row choice (`_RowChoiceReply`). The 0..candidates range is checked by the reader: the schema
#: subset has no numeric bounds.
ROW_CHOICE_SCHEMA: Final = _object({"row": {"type": "integer"}, "why": _STRING})
#: A label read without the ownership question (`_CropAnswer`).
CROP_SCHEMA: Final = _object(
    {
        "text": _STRING,
        "stacked": _BOOLEAN,
        "combined": _BOOLEAN,
        "readable": _BOOLEAN,
        "no_dimension": _BOOLEAN,
    }
)
#: The grounded span reading (`_GroundedCropAnswer`), in the prompt's own order. `text` is a plain
#: string on purpose: see the module note. `belongs` is a closed word list since
#: `claude-slot-span-v4` (#1110): "unsure" is its own answer, never folded into "no".
SPAN_SCHEMA: Final = _object(
    {
        "belongs": {"type": "string", "enum": ["yes", "no", "unsure"]},
        "text": _STRING,
        "stacked": _BOOLEAN,
        "combined": _BOOLEAN,
        "readable": _BOOLEAN,
        "no_dimension": _BOOLEAN,
    }
)
#: The hold-only line question (`_CounterBreakReply`).
COUNTER_BREAK_SCHEMA: Final = _object(
    {
        "contains_tall_appliance": _BOOLEAN,
        "stone_ends": {
            "type": "string",
            "enum": ["to_walls", "short_of_ends", "into_walls", "no_stone", "unsure"],
        },
        "why": _STRING,
    }
)
_SIDE: Final = {"type": "string", "enum": ["yes", "no", "unsure"]}
#: The wall question (`_WallReply`), in the prompt's own order.
WALL_SCHEMA: Final = _object(
    {
        "left": _SIDE,
        "right": _SIDE,
        "behind": _SIDE,
        "left_evidence": _STRING,
        "right_evidence": _STRING,
        "behind_evidence": _STRING,
        "view": {"type": "string", "enum": ["elevation", "plan", "other"]},
    }
)


#: What one architect dimension measures, in the words of the architect-pairing question
#: (`arch-pair-v2`, #1053). Code, not the model, decides which of these may pair with what.
ARCH_MEASURES: Final[tuple[str, ...]] = (
    "countertop",
    "cabinet_run",
    "single_cabinet",
    "filler_or_end_panel",
    "wall_to_wall",
    "clearance_or_gap",
    "blocking_or_backing",
    "fixture_or_appliance_centre",
    "appliance_opening",
    "height_or_other",
    "unsure",
)

#: The architect-pairing question (#1053, v2): first what EVERY numbered architect dimension
#: measures (`architect`, one entry per A-number), then for the vendor's overall and each vendor
#: piece the A-number of the architect dimension that measures the same physical thing, or 0.
#: Structure only; the ranges (every A once, 0..A, one entry per vendor piece) are checked by the
#: reader: the schema subset has no bounds.
ARCH_PAIR_SCHEMA: Final = _object(
    {
        "architect": {
            "type": "array",
            "items": _object(
                {
                    "a": {"type": "integer"},
                    "measures": {"type": "string", "enum": list(ARCH_MEASURES)},
                }
            ),
        },
        "overall": {"type": "integer"},
        "pieces": {"type": "array", "items": {"type": "integer"}},
        "why": _STRING,
    }
)


def output_config(schema: Mapping[str, object], effort: str) -> dict[str, object]:
    """The `output_config` a Claude request carries: the answer's schema and the stated effort."""
    if effort not in CLAUDE_EFFORTS:
        raise ValueError(f"Claude effort must be one of {', '.join(CLAUDE_EFFORTS)}: {effort!r}")
    return {"format": {"type": "json_schema", "schema": dict(schema)}, "effort": effort}


def _conforms(value: object, schema: Mapping[str, Any]) -> bool:
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return False
        properties = schema["properties"]
        if set(value) != set(properties) or set(schema["required"]) != set(properties):
            return False
        return all(_conforms(value[name], properties[name]) for name in properties)
    if kind == "string":
        if not isinstance(value, str):
            return False
        allowed = schema.get("enum")
        return allowed is None or value in allowed
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "array":
        return isinstance(value, list) and all(_conforms(item, schema["items"]) for item in value)
    return False


def claude_answer(response: Mapping[str, Any], schema: Mapping[str, Any]) -> Mapping[str, Any]:
    """The one JSON object a Claude reply holds, checked strictly against its schema.

    Raises `MalformedFormAnswer` — the reader's re-ask-once-then-abstain path — when the reply did
    not end its turn normally (`refusal`, `max_tokens`, or anything else), when its text is not
    exactly one JSON object, or when that object's keys or types differ from the schema.
    """
    stop = response.get("stopReason")
    if stop != "end_turn":
        raise MalformedFormAnswer(f"Claude reply stopped with {stop!r}, not a finished answer")
    raw = _response_text(response)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise MalformedFormAnswer("Claude reply is not one JSON object") from error
    if not _conforms(parsed, schema):
        raise MalformedFormAnswer("Claude reply does not match its answer schema")
    assert isinstance(parsed, dict)
    return parsed


class PictureWouldBeResized(ValueError):
    """A picture the API would resize (or could not size) is refused before any call."""


def png_size(png: bytes) -> tuple[int, int]:
    """A PNG's width and height from its header; refused when the bytes are not a PNG."""
    if len(png) < 24 or not png.startswith(_PNG_SIGNATURE) or png[12:16] != b"IHDR":
        raise PictureWouldBeResized("the picture is not a PNG whose size can be read")
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def visual_tokens(width: int, height: int) -> int:
    """The documented cost of a picture: one visual token per 28 x 28 px tile."""
    return -(-width // _TILE_PX) * -(-height // _TILE_PX)


def _limits(model_id: str) -> tuple[int, int]:
    model = model_id.removeprefix(_MODEL_PREFIX)
    return _HIGH_RESOLUTION_LIMITS if model in _HIGH_RESOLUTION_MODELS else _STANDARD_LIMITS


def picture_fits(width: int, height: int, model_id: str) -> bool:
    """The documented test (vision-coordinates, "How Claude resizes"): each side, padded up to a
    whole tile, within the long-edge limit, and the tile count within the visual-token limit."""
    max_edge, max_tokens = _limits(model_id)
    if width < 1 or height < 1:
        return False
    return (
        -(-width // _TILE_PX) * _TILE_PX <= max_edge
        and -(-height // _TILE_PX) * _TILE_PX <= max_edge
        and visual_tokens(width, height) <= max_tokens
    )


def require_picture_fits(pictures: Sequence[bytes], model_id: str) -> None:
    """Refuse, before any call, every picture the API would resize or reject for its size."""
    for position, png in enumerate(pictures, start=1):
        width, height = png_size(png)
        if not picture_fits(width, height, model_id):
            max_edge, max_tokens = _limits(model_id)
            raise PictureWouldBeResized(
                f"picture {position} is {width}x{height} px ({visual_tokens(width, height)} "
                f"visual tokens); {model_id} reads at most {max_edge} px a side and "
                f"{max_tokens} visual tokens without resizing"
            )
        if -(-len(png) // 3) * 4 > _MAX_BASE64_BYTES:
            raise PictureWouldBeResized(f"picture {position} exceeds the API's 10 MB image limit")
