"""The chat's models and the exact call each turn makes (#1463).

The reviewer picks the model, the effort and thinking; those values go into the API call as they
are, following Anthropic's rules for each model (CONTRACT, "Models, effort, thinking"):

| model | thinking on | thinking off |
|---|---|---|
| Claude Opus 5.5 | `{"type": "adaptive", "display": "summarized"}` | not offered (always on) |
| Claude Sonnet 5.5 | the same | `{"type": "between_tools"}`, effort `high` or below |
| Claude Haiku 5.5 | the same | `{"type": "disabled"}`, effort `high` or below |

Effort goes in `output_config.effort`. A helper's effort is the lead's, but never above
`HELPER_MAX_EFFORT` (medium): Anthropic's advice is low or medium effort for subagents, and a helper
at the lead's high effort spent most of a message's money thinking. No `budget_tokens`, no `temperature`; `tool_choice` is
`auto`, or `none` for a forced final answer. Opus 5.5 and Sonnet 5.5 on Anthropic's own API carry the
server-side refusal fallback (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`);
Haiku 5.5 has no server-side fallback, and OpenRouter takes neither (its keep-no-data routing block
goes in `provider` instead).

**Prompt caching.** Three cache breakpoints: the last tool (so the tool list is cached), the system
prompt, and the newest message's last block. Each turn re-sends the whole conversation unchanged,
so everything before the newest message is read from the cache.

**Pictures.** The history keeps a picture as a reference (`{"type": "gv_picture", ...}` as its
`source`), and each request puts the picture's bytes back in, from the chat's folder, the same way
every time, so the history is byte for byte what was sent before.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Final, Literal

__all__ = [
    "DEFAULT_EFFORT",
    "DEFAULT_MODEL",
    "EFFORTS",
    "FALLBACK_BETA",
    "HELPER_MAX_EFFORT",
    "HELPER_MAX_TOKENS",
    "LEAD_MAX_TOKENS",
    "MODELS",
    "OPENROUTER_BASE_URL",
    "ChatModel",
    "ChoiceRefused",
    "Provider",
    "call_params",
    "check_choice",
    "full_check_helper_effort",
    "helper_effort",
    "helper_thinking",
    "inline_pictures",
    "picture_sizes",
    "thinking_config",
]

type Provider = Literal["anthropic", "openrouter"]
type Effort = Literal["low", "medium", "high", "xhigh", "max"]

EFFORTS: Final[tuple[str, ...]] = ("low", "medium", "high", "xhigh", "max")
#: Effort levels at which a model's thinking may be turned off.
_THINKING_OFF_EFFORTS: Final = frozenset({"low", "medium", "high"})
DEFAULT_MODEL: Final = "claude-opus-5-5"
DEFAULT_EFFORT: Final = "high"
LEAD_MAX_TOKENS: Final = 64000
HELPER_MAX_TOKENS: Final = 32000
#: The highest effort a helper runs at; the lead keeps the reviewer's pick (`helper_effort`).
HELPER_MAX_EFFORT: Final = "medium"
#: The server-side refusal fallback's `"default"` form (Anthropic's own API only).
FALLBACK_BETA: Final = "server-side-fallback-2026-07-01"
#: OpenRouter's Anthropic-compatible endpoint; the SDK adds `/v1/messages`.
OPENROUTER_BASE_URL: Final = "https://openrouter.ai/api"
_EPHEMERAL: Final[Mapping[str, str]] = {"type": "ephemeral"}


@dataclass(frozen=True, slots=True)
class ChatModel:
    """One model the reviewer can pick."""

    id: str
    """Anthropic's model id, sent on Anthropic's own API."""
    label: str
    openrouter: str
    """OpenRouter's name for the same model."""
    rates_key: str
    """The model's key in the price file (`deploy/model_rates.us-east-1.json`)."""
    short: Literal["opus", "sonnet", "haiku"]
    thinking: Literal["always", "optional"]
    thinking_off: Mapping[str, str] | None
    """What "thinking off" sends (`None`: it cannot be turned off)."""
    fallbacks: bool
    """Whether Anthropic's own API takes the server-side refusal fallback for it."""
    default: bool = False

    @property
    def thinking_off_max_effort(self) -> str | None:
        return None if self.thinking_off is None else "high"


MODELS: Final[Mapping[str, ChatModel]] = {
    model.id: model
    for model in (
        ChatModel(
            id="claude-opus-5-5",
            label="Claude Opus 5.5",
            openrouter="anthropic/claude-opus-5.5",
            rates_key="anthropic.claude-opus-5-5",
            short="opus",
            thinking="always",
            thinking_off=None,
            fallbacks=True,
            default=True,
        ),
        ChatModel(
            id="claude-sonnet-5-5",
            label="Claude Sonnet 5.5",
            openrouter="anthropic/claude-sonnet-5.5",
            rates_key="anthropic.claude-sonnet-5-5",
            short="sonnet",
            thinking="optional",
            thinking_off={"type": "between_tools"},
            fallbacks=True,
        ),
        ChatModel(
            id="claude-haiku-5-5",
            label="Claude Haiku 5.5",
            openrouter="anthropic/claude-haiku-5.5",
            rates_key="anthropic.claude-haiku-5-5",
            short="haiku",
            thinking="optional",
            thinking_off={"type": "disabled"},
            fallbacks=False,
        ),
    )
}
#: A helper's model by the `Agent` tool's short name.
BY_SHORT: Final[Mapping[str, ChatModel]] = {model.short: model for model in MODELS.values()}


class ChoiceRefused(ValueError):
    """A model, effort and thinking combination the API would refuse, in plain words."""


def check_choice(model_id: str, effort: str, thinking: bool) -> ChatModel:
    """The model, after checking the combination is one the API takes (else `ChoiceRefused`)."""
    model = MODELS.get(model_id)
    if model is None:
        raise ChoiceRefused(f"{model_id!r} is not one of the models offered here")
    if effort not in EFFORTS:
        raise ChoiceRefused(f"effort must be one of {', '.join(EFFORTS)}")
    if not thinking:
        if model.thinking_off is None:
            raise ChoiceRefused(f"thinking is always on for {model.label}")
        if effort not in _THINKING_OFF_EFFORTS:
            raise ChoiceRefused(
                f"with thinking off, {model.label} takes effort high or below, not {effort}"
            )
    return model


def thinking_config(model: ChatModel, thinking: bool) -> dict[str, str]:
    """The `thinking` field for a checked choice (`check_choice` first)."""
    if thinking or model.thinking_off is None:
        return {"type": "adaptive", "display": "summarized"}
    return dict(model.thinking_off)


def helper_effort(lead_effort: str) -> str:
    """A helper's effort: the lead's, or `HELPER_MAX_EFFORT` when the lead's is higher (lead low:
    low; lead medium, high, xhigh or max: medium)."""
    return min(lead_effort, HELPER_MAX_EFFORT, key=EFFORTS.index)


def full_check_helper_effort(lead_effort: str) -> str:
    """A full check's helper effort (#1512): low, or `HELPER_MAX_EFFORT` (medium) when the lead's
    is max. A full check's helpers each read a few pages with a code-written brief; the lead
    checks what matters."""
    return HELPER_MAX_EFFORT if lead_effort == "max" else "low"


def helper_thinking(model: ChatModel, effort: str, thinking: bool) -> dict[str, str]:
    """A helper's `thinking` at the helper's own effort (`helper_effort`): the lead's choice where
    the helper's model takes it, else adaptive (a helper may run on another model, which may not
    take thinking off at that effort)."""
    if not thinking and model.thinking_off is not None and effort in _THINKING_OFF_EFFORTS:
        return dict(model.thinking_off)
    return {"type": "adaptive", "display": "summarized"}


# ---- pictures in the history -----------------------------------------------------------------


def _is_picture_ref(block: object) -> bool:
    if not isinstance(block, Mapping) or block.get("type") != "image":
        return False
    source = block.get("source")
    return isinstance(source, Mapping) and source.get("type") == "gv_picture"


def inline_pictures(
    messages: Sequence[Mapping[str, Any]], read: Callable[[str], bytes]
) -> list[dict[str, Any]]:
    """The messages with each picture reference replaced by the picture's bytes (base64), read with
    `read(picture_id)`. Every other value is copied as it is."""

    def walk(value: Any) -> Any:
        if _is_picture_ref(value):
            source = value["source"]
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(read(str(source["picture_id"]))).decode("ascii"),
                },
            }
        if isinstance(value, Mapping):
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    return [walk(message) for message in messages]


def picture_sizes(messages: Sequence[Mapping[str, Any]]) -> list[tuple[int, int]]:
    """Each referenced picture's (width, height), for the cost estimate."""
    found: list[tuple[int, int]] = []

    def walk(value: Any) -> None:
        if _is_picture_ref(value):
            source = value["source"]
            found.append((int(source.get("width") or 0), int(source.get("height") or 0)))
        elif isinstance(value, Mapping):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for message in messages:
        walk(message)
    return found


# ---- the call ------------------------------------------------------------------------------------


def _with_breakpoint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The messages with a cache breakpoint on the newest block, copied only where it changes."""
    if not messages:
        return messages
    last = dict(messages[-1])
    content = last.get("content")
    if isinstance(content, list) and content and isinstance(content[-1], Mapping):
        blocks = list(content)
        blocks[-1] = {**blocks[-1], "cache_control": dict(_EPHEMERAL)}
        last["content"] = blocks
    return [*messages[:-1], last]


def _openrouter_routing() -> dict[str, object]:
    """The readers' keep-no-data routing block (Google Vertex global only, no fallbacks, no data
    kept), loaded at call time: `app/` does not import `extraction/` at module level."""
    routing: Callable[[], dict[str, object]] = import_module(
        "extraction.slot_reader.openrouter"
    )._routing
    return routing()


def call_params(
    *,
    provider: Provider,
    model: ChatModel,
    effort: str,
    thinking: Mapping[str, str],
    system: str,
    tools: Sequence[Mapping[str, Any]],
    messages: Sequence[Mapping[str, Any]],
    final: bool,
    max_tokens: int,
) -> dict[str, Any]:
    """The keyword arguments of one streamed call (`client.beta.messages.create(stream=True)` on
    Anthropic's API with a beta, else `client.messages.create(stream=True)`): everything that
    leaves the process apart from the key. `messages` already carry their pictures' bytes."""
    listed = [dict(tool) for tool in tools]
    if listed:
        listed[-1]["cache_control"] = dict(_EPHEMERAL)
    params: dict[str, Any] = {
        "model": model.id if provider == "anthropic" else model.openrouter,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system, "cache_control": dict(_EPHEMERAL)}],
        "messages": _with_breakpoint([dict(message) for message in messages]),
        "tools": listed,
        "tool_choice": {"type": "none"} if final else {"type": "auto"},
        "thinking": dict(thinking),
        "output_config": {"effort": effort},
    }
    if not listed:
        # A call without tools (a finding's second read) sends neither field.
        del params["tools"], params["tool_choice"]
    if provider == "anthropic":
        if model.fallbacks:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
    else:
        params["extra_body"] = {"provider": _openrouter_routing()}
    return params
