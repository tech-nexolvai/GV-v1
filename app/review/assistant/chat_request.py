"""The chat's models and the exact call each turn makes (#1463).

The reviewer picks the model, the effort and thinking; those values go into the API call as they
are, following Anthropic's rules for each model (CONTRACT, "Models, effort, thinking"):

| model | thinking on | thinking off |
|---|---|---|
| Claude Fable 5.1 | `{"type": "adaptive", "display": "summarized"}` | not offered (always on) |
| Claude Opus 5.5 | the same | not offered (always on) |
| Claude Sonnet 5.5 | the same | `{"type": "between_tools"}`, effort `high` or below |
| Claude Haiku 5.5 | the same | `{"type": "disabled"}`, effort `high` or below |

Effort goes in `output_config.effort`. A helper's effort is the lead's, but never above
`HELPER_MAX_EFFORT` (medium): Anthropic's advice is low or medium effort for subagents, and a helper
at the lead's high effort spent most of a message's money thinking. No `budget_tokens`, no `temperature`; `tool_choice` is
`auto`, or `none` for a forced final answer (`any` and `tool` are a 400 on Fable 5.1 and the 5.5
models). Fable 5.1, Opus 5.5 and Sonnet 5.5 on Anthropic's own API carry the server-side refusal
fallback (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`); Haiku 5.5 has no
server-side fallback, and OpenRouter takes neither (its keep-no-data routing block goes in
`provider` instead).

**Claude Fable 5.1 (#1577)** is offered on Anthropic's own API only: it needs an account with 30-day
data retention, and the OpenRouter route keeps no data. Its thinking display stays `summarized`, as
for the other models: the summary includes Fable's progress notes between tool calls, and the feed
shows thinking blocks as "Thought for Ns" (the `updates` display would hide the reasoning summary
and needs one more beta for no gain on this screen).

**Preserved thinking.** A thinking block is bound to its model and to the conversation before it:
the system prompt, the tools and every earlier message. The lead's system prompt and tools are
frozen per chat (`chat_harness`), and the history is append-only. As a safety net, adaptive
thinking on Anthropic's own API carries `block_binding: {prefix_mismatch_behavior: "drop_block"}`
(beta `thinking-binding-controls-2026-08-01`): a block that no longer matches is dropped instead of
failing the whole call, and the reply's `input_transformations` say so (the engine logs them).

**Tool search (Anthropic's API).** The lead's rarer tools carry `defer_loading`
(`chat_tools.LEAD_LOADED` are always loaded): the request starts its tools with Anthropic's tool
search tool, and a deferred tool reaches the model only when its search finds it. The search runs
inside Anthropic's API (its `server_tool_use` and `tool_search_tool_result` blocks are kept in the
history as received). OpenRouter gets every tool loaded and no search tool.

**Prompt caching.** Three cache breakpoints: the last loaded tool (so the tool list is cached), the system
prompt, and the newest message's last block. Each turn re-sends the whole conversation unchanged,
so everything before the newest message is read from the cache.

**Models from outside Claude (comparison tests, 2026-10-25).** `EXTRA_MODELS` (GPT-6 Astra,
GPT-6.1 Sol, GPT-6 Luna on Azure; Gemini 4 Argon on Google Vertex global) run only through
OpenRouter's keep-no-data route, only when the server allows them (`GV_ASSISTANT_EXTRA_MODELS=1`),
and never in the model menu. Their requests are the Claude ones with three differences: `thinking`
is always `{"type": "adaptive"}` (OpenRouter turns it, with `output_config.effort`, into the model's
own reasoning effort; `display` is Claude's), the effort is the highest the model takes when it
takes no higher (Argon: low, medium, high), and `provider.only` names the model's own keep-no-data
hosts. Under such a lead every call of the answer, helpers' included, runs on the lead's model
(`ChatEngine.run_model`), so a test measures one model.

**Pictures.** The history keeps a picture as a reference (`{"type": "gv_picture", ...}` as its
`source`), and each request puts the picture's bytes back in, from the chat's folder, the same way
every time, so the history is byte for byte what was sent before.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Final, Literal

from app.review.assistant.chat_context import CONTEXT_BETA

__all__ = [
    "BINDING_BETA",
    "DEFAULT_EFFORT",
    "DEFAULT_MODEL",
    "EFFORTS",
    "EXTRA_MODELS",
    "FALLBACK_BETA",
    "HELPER_MAX_EFFORT",
    "HELPER_MAX_TOKENS",
    "LEAD_MAX_TOKENS",
    "MODELS",
    "OPENROUTER_BASE_URL",
    "TOOL_SEARCH",
    "ChatModel",
    "ChoiceRefused",
    "Provider",
    "call_params",
    "check_choice",
    "full_check_helper_effort",
    "helper_effort",
    "helper_shorts",
    "helper_thinking",
    "inline_pictures",
    "picture_sizes",
    "rates_key_for",
    "schema_note",
    "thinking_config",
    "thinking_tokens",
]

type Provider = Literal["anthropic", "openrouter"]
type Effort = Literal["low", "medium", "high", "xhigh", "max"]
#: Who makes a model: Claude's, or one tested against it through OpenRouter (`EXTRA_MODELS`).
type Family = Literal["claude", "openai", "google"]

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
#: Lets a call say what happens to a thinking block that no longer matches its conversation, and
#: makes the reply list what was dropped (`input_transformations`).
BINDING_BETA: Final = "thinking-binding-controls-2026-08-01"
_BLOCK_BINDING: Final[Mapping[str, str]] = {"prefix_mismatch_behavior": "drop_block"}
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
    short: Literal["fable", "opus", "sonnet", "haiku", "astra", "sol", "luna", "argon"]
    thinking: Literal["always", "optional"]
    thinking_off: Mapping[str, str] | None
    """What "thinking off" sends (`None`: it cannot be turned off)."""
    fallbacks: bool
    """Whether Anthropic's own API takes the server-side refusal fallback for it."""
    default: bool = False
    providers: tuple[Provider, ...] = ("anthropic", "openrouter")
    """The routes that offer it (Fable 5.1: Anthropic's own API only)."""
    helper: bool = True
    """Whether the `Agent` tool may name it for a helper (not Fable 5.1: a helper's work is
    routine reading, and Fable costs 2.5 times Opus 5.5 per token)."""
    family: Family = "claude"
    hosts: tuple[str, ...] = ()
    """The OpenRouter hosts it may run on (`provider.only`), each on OpenRouter's zero-data-retention
    list; empty: the readers' own (`extraction.slot_reader.openrouter._routing`)."""
    efforts: tuple[str, ...] = EFFORTS
    """The efforts it takes, lowest first; a higher one is sent as the highest of these."""
    long_prompt_tokens: int | None = None
    """Above this many prompt tokens a call costs twice the input prices and 1.5 times the output
    price (OpenRouter's price list for GPT-6: over 272,000 tokens)."""

    @property
    def thinking_off_max_effort(self) -> str | None:
        return None if self.thinking_off is None else "high"

    @property
    def extra(self) -> bool:
        """A model from outside Claude, offered only for comparison tests (`EXTRA_MODELS`)."""
        return self.family != "claude"

    def effort_for(self, effort: str) -> str:
        """The effort sent: the one asked, or the highest the model takes when it takes no higher."""
        return effort if effort in self.efforts else self.efforts[-1]


MODELS: Final[Mapping[str, ChatModel]] = {
    model.id: model
    for model in (
        ChatModel(
            id="claude-opus-5-5",
            label="Opus 5.5",
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
            label="Sonnet 5.5",
            openrouter="anthropic/claude-sonnet-5.5",
            rates_key="anthropic.claude-sonnet-5-5",
            short="sonnet",
            thinking="optional",
            thinking_off={"type": "between_tools"},
            fallbacks=True,
        ),
        ChatModel(
            id="claude-haiku-5-5",
            label="Haiku 5.5",
            openrouter="anthropic/claude-haiku-5.5",
            rates_key="anthropic.claude-haiku-5-5",
            short="haiku",
            thinking="optional",
            thinking_off={"type": "disabled"},
            fallbacks=False,
        ),
        # #1577: the premium choice, last in the menu; not the default (Anthropic: Fable is for
        # the hardest long tasks, at 2.5 times Opus 5.5's price per token).
        ChatModel(
            id="claude-fable-5-1",
            label="Fable 5.1",
            openrouter="",
            rates_key="anthropic.claude-fable-5-1",
            short="fable",
            thinking="always",
            thinking_off=None,
            fallbacks=True,
            providers=("anthropic",),
            helper=False,
        ),
    )
}
#: OpenAI's GPT-6 models on Azure only: OpenRouter's keep-no-data hosts for them (checked on
#: `GET /api/v1/endpoints/zdr`, 2026-10-25).
_AZURE: Final = ("azure",)
_GPT6_LONG_PROMPT: Final = 272_000

#: Models from outside Claude, for comparison tests of the full check (Anant, 2026-10-25): offered
#: only through OpenRouter's keep-no-data route and only with `GV_ASSISTANT_EXTRA_MODELS=1`; never
#: in the model menu, never a helper the lead may name (under one, every call runs on it).
EXTRA_MODELS: Final[Mapping[str, ChatModel]] = {
    model.id: model
    for model in (
        ChatModel(
            id="gpt-6-astra",
            label="GPT-6 Astra",
            openrouter="openai/gpt-6-astra",
            rates_key="openai.gpt-6-astra",
            short="astra",
            thinking="always",
            thinking_off=None,
            fallbacks=False,
            providers=("openrouter",),
            helper=False,
            family="openai",
            hosts=_AZURE,
            long_prompt_tokens=_GPT6_LONG_PROMPT,
        ),
        ChatModel(
            id="gpt-6.1-sol",
            label="GPT-6.1 Sol",
            openrouter="openai/gpt-6.1-sol",
            rates_key="openai.gpt-6.1-sol",
            short="sol",
            thinking="always",
            thinking_off=None,
            fallbacks=False,
            providers=("openrouter",),
            helper=False,
            family="openai",
            hosts=_AZURE,
            long_prompt_tokens=_GPT6_LONG_PROMPT,
        ),
        ChatModel(
            id="gpt-6-luna",
            label="GPT-6 Luna",
            openrouter="openai/gpt-6-luna",
            rates_key="openai.gpt-6-luna",
            short="luna",
            thinking="always",
            thinking_off=None,
            fallbacks=False,
            providers=("openrouter",),
            helper=False,
            family="openai",
            hosts=_AZURE,
            long_prompt_tokens=_GPT6_LONG_PROMPT,
        ),
        ChatModel(
            id="gemini-4-argon",
            label="Gemini 4 Argon",
            openrouter="google/gemini-4-argon",
            rates_key="google.gemini-4-argon",
            short="argon",
            thinking="always",
            thinking_off=None,
            fallbacks=False,
            providers=("openrouter",),
            helper=False,
            family="google",
            hosts=("google-vertex/global",),
            efforts=("low", "medium", "high"),
        ),
    )
}
#: A model by its short name (the `Agent` tool takes only those `helper_shorts` lists).
BY_SHORT: Final[Mapping[str, ChatModel]] = {model.short: model for model in MODELS.values()}


def helper_shorts() -> tuple[str, ...]:
    """The short names the `Agent` tool may name for a helper."""
    return tuple(model.short for model in MODELS.values() if model.helper)


_SNAPSHOT_SUFFIX: Final = re.compile(r"-\d{8}$")


def rates_key_for(model_id: str) -> str:
    """The price-file key of a model the API named (a refusal fallback's model too): the offered
    model's own key, else `anthropic.<id>` without a dated snapshot suffix."""
    base = _SNAPSHOT_SUFFIX.sub("", model_id.removeprefix("anthropic/"))
    known = MODELS.get(base)
    return known.rates_key if known is not None else f"anthropic.{base}"


class ChoiceRefused(ValueError):
    """A model, effort and thinking combination the API would refuse, in plain words."""


def check_choice(
    model_id: str,
    effort: str,
    thinking: bool,
    provider: Provider | None = None,
    *,
    extra: bool = False,
) -> ChatModel:
    """The model, after checking the combination is one the API takes (else `ChoiceRefused`).
    With `provider`, also that the route offers the model. `extra`: the server allows the models
    from outside Claude (`EXTRA_MODELS`, `GV_ASSISTANT_EXTRA_MODELS`)."""
    model = MODELS.get(model_id)
    if model is None and extra:
        model = EXTRA_MODELS.get(model_id)
    if model is None:
        raise ChoiceRefused(f"{model_id!r} is not one of the models offered here")
    if provider is not None and provider not in model.providers and model.extra:
        raise ChoiceRefused(
            f"{model.label} runs only through OpenRouter's keep-no-data route, and this server "
            "uses Anthropic's own API (set GV_ASSISTANT_PROVIDER=openrouter)."
        )
    if provider is not None and provider not in model.providers:
        raise ChoiceRefused(
            f"{model.label} is offered only on Anthropic's own API (it needs 30-day data "
            "retention), and this server uses OpenRouter's keep-no-data route. Pick Opus 5.5."
        )
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


def thinking_tokens(usage: Mapping[str, Any] | None) -> int | None:
    """The thinking tokens a call reports (`usage.output_tokens_details.thinking_tokens`; a flat
    `thinking_tokens` already summed by code); `None` when the usage does not say (not reported is
    not "did not think"): saved with every judgement call so a red whose checker did not think at
    all can be found (#1775, C4)."""
    if not isinstance(usage, Mapping):
        return None
    flat = usage.get("thinking_tokens")
    if isinstance(flat, int) and not isinstance(flat, bool):
        return flat
    details = usage.get("output_tokens_details")
    found = details.get("thinking_tokens") if isinstance(details, Mapping) else None
    return found if isinstance(found, int) and not isinstance(found, bool) else None


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


#: Anthropic's tool search (regex), sent first whenever a tool is deferred (`defer_loading`): the
#: model finds a deferred tool by searching names and descriptions before it calls it.
TOOL_SEARCH: Final[Mapping[str, str]] = {
    "type": "tool_search_tool_regex_20251119",
    "name": "tool_search_tool_regex",
}


def schema_note(schema: Mapping[str, Any]) -> str:
    """The system prompt's last line where the provider takes no `output_config.format`."""
    shape = json.dumps(schema, separators=(",", ":"), sort_keys=True)
    return f"Answer with one JSON object that follows this JSON schema, and nothing else: {shape}"


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
    context_management: Mapping[str, Any] | None = None,
    output_format: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The keyword arguments of one streamed call (`client.beta.messages.create(stream=True)` on
    Anthropic's API with a beta, else `client.messages.create(stream=True)`): everything that
    leaves the process apart from the key. `messages` already carry their pictures' bytes.

    `output_format`: a JSON schema the reply must follow (structured outputs). On Anthropic's own
    API it goes in `output_config.format`, so the reply is that JSON and nothing else; OpenRouter's
    route is not known to pass it through, so there the schema is added to the system prompt."""
    listed = [dict(tool) for tool in tools]
    if provider != "anthropic":
        # Tool search is Anthropic's: every tool goes loaded elsewhere.
        for tool in listed:
            tool.pop("defer_loading", None)
    elif any(tool.get("defer_loading") for tool in listed):
        listed.insert(0, dict(TOOL_SEARCH))
    # The tools' cache breakpoint: on the last tool always loaded (a deferred tool takes none).
    loaded = [
        n
        for n, tool in enumerate(listed)
        if "input_schema" in tool and not tool.get("defer_loading")
    ]
    if loaded:
        listed[loaded[-1]]["cache_control"] = dict(_EPHEMERAL)
    if output_format is not None and provider != "anthropic":
        system = f"{system}\n\n{schema_note(output_format)}"
    params: dict[str, Any] = {
        "model": model.id if provider == "anthropic" else model.openrouter,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system, "cache_control": dict(_EPHEMERAL)}],
        "messages": _with_breakpoint([dict(message) for message in messages]),
        "tools": listed,
        # Never `any` or `tool`: forced tool use is a 400 on Fable 5.1 and the 5.5 models.
        "tool_choice": {"type": "none"} if final else {"type": "auto"},
        "thinking": dict(thinking),
        "output_config": {"effort": effort},
    }
    if not listed:
        # A call without tools (a finding's second read) sends neither field.
        del params["tools"], params["tool_choice"]
    if output_format is not None and provider == "anthropic":
        params["output_config"]["format"] = {
            "type": "json_schema",
            "schema": json.loads(json.dumps(output_format)),
        }
    if provider == "anthropic":
        betas: list[str] = []
        if thinking.get("type") == "adaptive":
            # The safety net for preserved thinking (`between_tools` and `disabled` take no other
            # field, and carry no thinking to bind).
            params["thinking"]["block_binding"] = dict(_BLOCK_BINDING)
            betas.append(BINDING_BETA)
        if model.fallbacks:
            betas.append(FALLBACK_BETA)
            params["fallbacks"] = "default"
        if context_management:
            # Old tool results and thinking cleared server-side (`chat_context`, #1548).
            betas.append(CONTEXT_BETA)
            params["context_management"] = json.loads(json.dumps(context_management))
        if betas:
            params["betas"] = betas
    else:
        if model.extra:
            # OpenRouter turns adaptive thinking and `output_config.effort` into the model's own
            # reasoning effort; `display` (and every other thinking form) is Claude's.
            params["thinking"] = {"type": "adaptive"}
            params["output_config"]["effort"] = model.effort_for(effort)
        routing = _openrouter_routing()
        if model.hosts:
            # Only the model's own keep-no-data hosts (the rest of the block unchanged).
            routing["only"] = list(model.hosts)
        params["extra_body"] = {"provider": routing}
    return params
