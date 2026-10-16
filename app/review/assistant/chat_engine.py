"""One chat message, answered the way claude.ai answers (#1463): stream, use tools, delegate, answer.

**The loop (the lead).** The turn's request is the chat's earlier API messages, replayed byte for byte
(`api_content` of every earlier answer), then this message. Each call streams: thinking and text
reach the screen as they arrive (`thinking_start`, `thinking_delta`, `thinking_end`, `text_delta`);
the reply goes back into the history unchanged (thinking blocks included). When the reply asks for
tools, each runs in order (`step_start`, `step_end`; `tasks` after a task-list change; `file` for a
handed-over file), the `Agent` calls of one reply together, at most four at once (`agent_start`,
`agent_step`, `agent_end`); all the results go back in one user message, and the model is asked
again. Up to `lead_max_turns` turns of tools; then one last call with `tool_choice: none` and a note
to answer from what it has (`stopped: turn_limit`).

**Money.** Before every call its cost is estimated (an upper bound: text at one token per two
bytes, pictures by their size, the whole output allowance, the previous call's prompt read from the
cache). The lead stops cleanly when the message's spend plus the estimate would pass the message
cap, or the drawing set's spend plus it the set's cap: first with one final, smaller call told to
answer now from what it has, else with no call; the answer says so (`stopped: cost_cap`).

**Helpers always report.** A helper runs on Sonnet when the lead is Opus (else on the lead's model)
unless the lead names one, and has `helper_max_turns` turns. The helpers the lead starts in one turn
may together spend `HELPER_SHARE_PERCENT` of what is left of the message cap at that moment; the rest
stays for the lead to check their reports and answer. Near any of these limits a helper is told
"wrap up now: report what you found and what you did not get to" and gets one final call without
tools, made even when that call passes the cap (bounded by a smaller output allowance). If even that
fails, its report is written from the steps it took. There are no per-helper money shares that kill
a helper before it reports.

**Failures.** A provider 5xx, 520/529, a lost connection or an error in the middle of a stream is
asked once more (for the lead and for helpers); a call's partial output is then dropped from what is
stored. A refusal (`stop_reason: refusal`) ends the message with a plain "Claude declined" answer;
its partial output is never stored or replayed. Cancel (or the client going away) stops between
steps and in the middle of a stream, kills the running sandbox commands and stops the helpers; the
message is still stored, `stopped: cancelled`.

**The history stays valid and append-only.** A reply whose tools did not run (cancelled, or a final
call that asked for tools anyway) is followed by one tool result per call saying so. An answer that
got no reply at all stores no API messages, so an unanswered question never enters the history.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from time import monotonic, monotonic_ns
from typing import Any, Final, Protocol

from app.review.assistant.agent import (
    HELPER_PICTURE_EDGE_PX,
    billed_input_tokens,
    estimate_micros,
    prompt_tokens_upper,
)
from app.review.assistant.chat_provider import ChatProvider, ProviderFailure, failure_from
from app.review.assistant.chat_request import (
    BY_SHORT,
    HELPER_MAX_TOKENS,
    LEAD_MAX_TOKENS,
    ChatModel,
    call_params,
    helper_thinking,
    inline_pictures,
    picture_sizes,
    thinking_config,
)
from app.review.assistant.chat_tools import (
    HELPER_SYSTEM_PROMPT,
    HELPER_TOOLS,
    LEAD_SYSTEM_PROMPT,
    LEAD_TOOLS,
    MAX_AGENTS,
    ChatTools,
    TaskList,
    ToolResult,
    arguments,
    new_step,
)
from app.review.assistant.sandbox import SandboxRefused
from app.runs.rates import ModelRates, call_cost_micros

__all__ = [
    "HARNESS_ID",
    "ChatEngine",
    "EngineSettings",
    "Ledger",
    "MessageOutcome",
    "cost_text",
    "prompt_ids",
]

_log = logging.getLogger(__name__)

#: Names the system prompts and tools: a change to either is a new id. (chat-v2: the `Agent`
#: tool's `model` text names Sonnet as the default.) Only the request's prefix changes; a chat's
#: stored messages replay unchanged.
HARNESS_ID: Final = "chat-v2"
#: The output allowance of a forced last call (a wrap-up or a final answer at the cap).
WRAP_UP_MAX_TOKENS: Final = 8000
FINAL_MAX_TOKENS: Final = 16000
#: The share of what is left of the message cap that the helpers of one delegation may spend
#: together; the rest is the lead's, to check their reports and write the answer.
HELPER_SHARE_PERCENT: Final = 70
#: A helper's model when the lead names none, by the lead's: Opus leads, Sonnet helps.
HELPER_DEFAULT_MODEL: Final[Mapping[str, str]] = {
    "opus": "sonnet",
    "sonnet": "sonnet",
    "haiku": "haiku",
}
_RETRY_WAIT_SECONDS: Final = 3.0
_MAX_REPORT_CHARS: Final = 40_000
_MAX_PROMPT_CHARS: Final = 20_000
_MAX_DESCRIPTION: Final = 120
#: Claude Haiku 5.5's price is five times higher for a prompt over 100K tokens.
_HAIKU_LONG_PROMPT: Final = 100_000
_HAIKU_LONG_FACTOR: Final = 5
_MICROS: Final = Decimal(1_000_000)

COST_CAP_NOTE: Final = (
    "This message has nearly used its money cap. Do not use any more tools: answer now from what "
    "you have found, and say what you did not get to."
)
TURN_LIMIT_NOTE: Final = (
    "You have used all the tool turns for this message. Do not use any more tools: answer now "
    "from what you have found, and say what you did not get to."
)
WRAP_UP_NOTE: Final = (
    "Wrap up now ({why}). Do not use any more tools: report what you found, and which pages or "
    "parts you did not get to."
)


def prompt_ids(provider: str) -> tuple[str, str]:
    """The lead's and the helpers' prompt ids as the Usage page reads them (route and purpose)."""
    return (
        f"review-assistant-chat-{provider}-{HARNESS_ID}",
        f"review-assistant-chat-helper-{provider}-{HARNESS_ID}",
    )


def cost_text(micros: int) -> str:
    """Millionths of a dollar as "1.72"."""
    return str((Decimal(micros) / _MICROS).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def cap_text(micros: int) -> str:
    """A cap in words: "40", or "7.50" when it has cents."""
    return cost_text(micros).removesuffix(".00")


class Ledger(Protocol):
    """Where each model call is recorded (the Usage page's rows)."""

    def record(
        self,
        *,
        model_key: str,
        prompt_id: str,
        started_ns: int,
        input_tokens: int,
        output_tokens: int,
        stop_reason: str | None,
        error: BaseException | None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class EngineSettings:
    model: ChatModel
    effort: str
    thinking: bool
    lead_max_turns: int
    helper_max_turns: int
    message_cap_micros: int
    package_cap_micros: int
    rates: Callable[[str], ModelRates | None]
    """The price list for a model's price key (`None`: no stated price)."""


@dataclass(slots=True)
class MessageOutcome:
    """What the message stores: the answer, the screen's blocks, the turn's API messages."""

    text: str
    blocks: list[dict[str, Any]]
    api_content: list[dict[str, Any]]
    cost_micros: int
    stopped: str | None = None
    error: str | None = None
    cap_hit: str | None = None
    """With `stopped: cost_cap`, which cap stopped it: `message` (this answer's) or `package` (the
    drawing set's budget)."""


# ---- one streamed call ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Reply:
    """A finished call: the content as received, why it stopped, its usage and the model named."""

    content: list[dict[str, Any]]
    stop_reason: str | None
    stop_details: Mapping[str, Any] | None
    usage: dict[str, Any]
    model: str | None
    bad_inputs: set[int] = field(default_factory=set)

    def history_content(self) -> list[dict[str, Any]]:
        """The content to send back. After a refusal fallback, the declined model's thinking and
        tool calls before the last switch are left out, as the API asks; the switch marker too."""
        last = max(
            (n for n, block in enumerate(self.content) if block.get("type") == "fallback"),
            default=None,
        )
        if last is None:
            return list(self.content)
        dropped = {"thinking", "redacted_thinking", "tool_use", "server_tool_use", "fallback"}
        return [
            block
            for n, block in enumerate(self.content)
            if not (n <= last and block.get("type") in dropped)
        ]

    def text(self) -> str:
        parts = [
            str(block.get("text") or "").strip()
            for block in self.history_content()
            if block.get("type") == "text"
        ]
        return "\n\n".join(part for part in parts if part)

    def tool_uses(self) -> list[dict[str, Any]]:
        return [block for block in self.history_content() if block.get("type") == "tool_use"]

    @property
    def fell_back(self) -> bool:
        """Whether Anthropic's refusal fallback served it: a switch marker in the content, or (a
        conversation already switched, "sticky") a `fallback_message` among the usage's attempts."""
        attempts = self.usage.get("iterations")
        sticky = isinstance(attempts, list) and any(
            isinstance(item, Mapping) and item.get("type") == "fallback_message"
            for item in attempts
        )
        return sticky or any(block.get("type") == "fallback" for block in self.content)


class _Cancelled(Exception):
    """The message was cancelled in the middle of a call."""


#: The display side of a streamed call (the lead's): called with each raw event.
type _Display = Callable[[Mapping[str, Any]], None]


def _merge_usage(usage: dict[str, Any], update: Mapping[str, Any] | None) -> None:
    if not isinstance(update, Mapping):
        return
    for key, value in update.items():
        if value is not None:
            usage[key] = value


def _accumulate(
    events: Any,
    display: _Display | None,
    cancelled: Callable[[], bool],
    usage: dict[str, Any],
) -> _Reply:
    """A call's reply, built from its raw stream events exactly as they arrived. `usage` is filled
    as the events come, so a cancelled call's usage so far is known."""
    content: list[dict[str, Any]] = []
    buffers: dict[int, str] = {}
    stop_reason: str | None = None
    stop_details: Mapping[str, Any] | None = None
    model: str | None = None
    bad: set[int] = set()
    for event in events:
        if cancelled():
            raise _Cancelled
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") or {}
            model = message.get("model") if isinstance(message.get("model"), str) else None
            _merge_usage(usage, message.get("usage"))
        elif kind == "content_block_start":
            index = int(event.get("index", len(content)))
            block = dict(event.get("content_block") or {})
            if block.get("type") == "tool_use":
                block["input"] = {}
            while len(content) <= index:
                content.append({})
            content[index] = block
            if block.get("type") == "fallback":
                to = block.get("to")
                if isinstance(to, Mapping) and isinstance(to.get("model"), str):
                    model = to["model"]
        elif kind == "content_block_delta":
            index = int(event.get("index", 0))
            delta = event.get("delta") or {}
            if index >= len(content):
                continue
            block = content[index]
            dtype = delta.get("type")
            if dtype == "text_delta":
                block["text"] = str(block.get("text") or "") + str(delta.get("text") or "")
            elif dtype == "thinking_delta":
                block["thinking"] = str(block.get("thinking") or "") + str(
                    delta.get("thinking") or ""
                )
            elif dtype == "signature_delta":
                block["signature"] = delta.get("signature")
            elif dtype == "input_json_delta":
                buffers[index] = buffers.get(index, "") + str(delta.get("partial_json") or "")
            elif dtype == "citations_delta":
                block.setdefault("citations", None)
                block["citations"] = [*(block["citations"] or []), delta.get("citation")]
        elif kind == "content_block_stop":
            index = int(event.get("index", 0))
            if index < len(content) and content[index].get("type") == "tool_use":
                raw = buffers.pop(index, "")
                try:
                    parsed = json.loads(raw) if raw.strip() else {}
                except ValueError:
                    parsed = {}
                    bad.add(index)
                if isinstance(parsed, dict):
                    content[index]["input"] = parsed
                else:
                    bad.add(index)
        elif kind == "message_delta":
            delta = event.get("delta") or {}
            stop_reason = delta.get("stop_reason") or stop_reason
            details = delta.get("stop_details")
            stop_details = details if isinstance(details, Mapping) else stop_details
            _merge_usage(usage, event.get("usage"))
        if display is not None:
            display(event)
    # A tool call whose input never finished (cut at max_tokens) is not run.
    for index in buffers:
        bad.add(index)
    return _Reply(
        content=[block for block in content if block],
        stop_reason=stop_reason,
        stop_details=stop_details,
        usage=usage,
        model=model,
        bad_inputs={id(content[n]) for n in bad if n < len(content)},
    )


_SNAPSHOT: Final = re.compile(r"(?:[-@]\d{8})?")


def _names_model(reply: _Reply, model: ChatModel, provider: str) -> bool:
    """Whether the reply came from the model asked (or from Anthropic's own refusal fallback)."""
    if provider == "anthropic" and reply.fell_back:
        return True
    reported = reply.model
    if not isinstance(reported, str):
        return False
    names = (model.id, model.openrouter, model.openrouter.removeprefix("anthropic/"))
    return any(
        reported.startswith(name) and _SNAPSHOT.fullmatch(reported, len(name)) is not None
        for name in names
    )


# ---- the engine ----------------------------------------------------------------------------------


@dataclass(slots=True)
class _Agent:
    call_id: str
    description: str
    prompt: str
    model: ChatModel


@dataclass(slots=True)
class _Share:
    """The helpers' money for one delegation: the message spend they may take it up to, and the
    estimates of their calls running now (held, so helpers running at once do not overshoot)."""

    limit: int
    held: int = 0


class ChatEngine:
    """One message's run. `emit(event, data)` sends an SSE event; it must be thread-safe."""

    def __init__(
        self,
        *,
        settings: EngineSettings,
        provider: ChatProvider,
        tools: ChatTools,
        ledger: Ledger,
        emit: Callable[[str, dict[str, Any]], None],
        history: Sequence[Mapping[str, Any]],
        user_content: list[dict[str, Any]],
        package_spent: Callable[[], int],
        read_picture: Callable[[str], bytes],
        cancel: threading.Event,
        sleep: Callable[[float], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.tools = tools
        self.ledger = ledger
        self._emit = emit
        self.history = [dict(message) for message in history]
        self.user_content = user_content
        self.package_spent = package_spent
        self.read_picture = read_picture
        self.cancel = cancel
        # Waits before a retry; returns True when cancelled during the wait.
        self._sleep = sleep or (lambda seconds: cancel.wait(seconds))
        self.lead_prompt_id, self.helper_prompt_id = prompt_ids(provider.name)
        self.blocks: list[dict[str, Any]] = []
        self._discarded: set[int] = set()
        self._lock = threading.RLock()
        self._spent = 0
        self._attempt_blocks: list[int] = []
        self._streams: set[Any] = set()
        self.task_list = TaskList()

    # ---- the screen ------------------------------------------------------------------------------

    def emit(self, event: str, data: dict[str, Any]) -> None:
        try:
            self._emit(event, data)
        except Exception:  # noqa: BLE001 - a gone screen never stops the answer being stored
            _log.debug("assistant chat event could not be sent")

    def _add_block(self, block: dict[str, Any]) -> int:
        with self._lock:
            self.blocks.append(block)
            return len(self.blocks) - 1

    def _add_text(self, text: str) -> None:
        """A text block written by the app (a stop note), streamed like the model's."""
        index = self._add_block({"type": "text", "text": text})
        self.emit("text_delta", {"index": index, "text": text})

    # ---- money -----------------------------------------------------------------------------------

    @property
    def spent(self) -> int:
        with self._lock:
            return self._spent

    def _price(self, model: ChatModel, billed_in: int, out: int) -> int | None:
        return call_cost_micros(
            self.settings.rates(model.rates_key), model.rates_key, billed_in, out
        )

    def _weighted(self, model: ChatModel, usage: Mapping[str, Any]) -> tuple[int, int]:
        """The call's tokens as price-weighted equivalents at the model's base price: cache
        writes 1.25x, reads 0.1x; Haiku's long-prompt price 5x."""
        billed_in = billed_input_tokens(usage)
        out = int(usage.get("output_tokens") or 0)
        prompt = sum(
            int(usage.get(key) or 0)
            for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        if model.short == "haiku" and prompt > _HAIKU_LONG_PROMPT:
            return billed_in * _HAIKU_LONG_FACTOR, out * _HAIKU_LONG_FACTOR
        return billed_in, out

    def _estimate(
        self, model: ChatModel, body: Mapping[str, Any], cached: int, previous: int
    ) -> int | None:
        pictures = picture_sizes(body.get("messages") or [])
        estimate = estimate_micros(
            body,
            pictures,
            model.rates_key,
            self.settings.rates(model.rates_key),
            cached_tokens=cached,
            previous_tokens=previous,
        )
        if (
            estimate is not None
            and model.short == "haiku"
            and prompt_tokens_upper(body, pictures) > _HAIKU_LONG_PROMPT
        ):
            estimate *= _HAIKU_LONG_FACTOR
        return estimate

    def _fits(self, estimate: int | None) -> bool:
        return self._over(estimate) is None

    def _over(self, estimate: int | None) -> str | None:
        """The cap a call of this estimate would pass: `message`, `package`, or `None` (it fits).
        The set's budget is named first when both would be passed: more answers cannot help."""
        if estimate is None:
            return None
        try:
            package = self.package_spent()
        except Exception:  # noqa: BLE001 - an unknown spend stops rather than overspends
            _log.exception("assistant chat could not read the drawing set's spend")
            return "package"
        spent = self.spent
        if package + spent + estimate > self.settings.package_cap_micros:
            return "package"
        if spent + estimate > self.settings.message_cap_micros:
            return "message"
        return None

    def _hold(self, share: _Share, estimate: int | None) -> bool:
        """Whether a helper's next call fits the helpers' share; if it does, its estimate is held
        until the call is charged."""
        if estimate is None:
            return True
        with self._lock:
            if self._spent + share.held + estimate > share.limit:
                return False
            share.held += estimate
            return True

    def _release(self, share: _Share, held: int) -> None:
        with self._lock:
            share.held -= held

    def _charge(self, model: ChatModel, usage: Mapping[str, Any]) -> None:
        billed_in, out = self._weighted(model, usage)
        cost = self._price(model, billed_in, out) or 0
        with self._lock:
            self._spent += cost
            total = self._spent
        self.emit("usage", {"cost_usd": cost_text(total)})

    # ---- one call --------------------------------------------------------------------------------

    def _body(
        self,
        *,
        model: ChatModel,
        thinking: Mapping[str, str],
        system: str,
        tools: Sequence[Mapping[str, Any]],
        messages: list[dict[str, Any]],
        final: bool,
        max_tokens: int,
    ) -> dict[str, Any]:
        """The call as stored (pictures as references), for the estimate."""
        return {
            "max_tokens": max_tokens,
            "system": system,
            "tools": list(tools),
            "tool_choice": {"type": "none"} if final else {"type": "auto"},
            "thinking": dict(thinking),
            "messages": messages,
        }

    def _call(
        self,
        *,
        model: ChatModel,
        thinking: Mapping[str, str],
        system: str,
        tools: Sequence[Mapping[str, Any]],
        messages: list[dict[str, Any]],
        final: bool,
        max_tokens: int,
        prompt_id: str,
        display: Callable[[], _Display] | None,
    ) -> _Reply:
        """One call, streamed, asked once more after a provider failure worth retrying. Raises
        `ProviderFailure` or `_Cancelled`."""
        attempts = 0
        while True:
            try:
                inlined = inline_pictures(messages, self.read_picture)
            except OSError:
                raise ProviderFailure(
                    "A picture this chat looked at earlier is missing from its folder, so the chat "
                    "cannot go on. Start a new chat.",
                    status=None,
                    retryable=False,
                ) from None
            params = call_params(
                provider=self.provider.name,
                model=model,
                effort=self.settings.effort,
                thinking=thinking,
                system=system,
                tools=tools,
                messages=inlined,
                final=final,
                max_tokens=max_tokens,
            )
            started = monotonic_ns()
            shown = display() if display is not None else None
            reply: _Reply | None = None
            failure: ProviderFailure | None = None
            usage: dict[str, Any] = {}
            try:
                stream = self.provider.open_stream(params)
                with self._lock:
                    self._streams.add(stream)
                try:
                    reply = _accumulate(stream, shown, self.cancel.is_set, usage)
                finally:
                    with self._lock:
                        self._streams.discard(stream)
                    stream.close()
            except _Cancelled:
                self._cancelled_call(model, prompt_id, started, usage)
                raise
            except Exception as raised:  # noqa: BLE001 - every provider failure is told plainly
                if self.cancel.is_set():
                    # Cancel closed the stream under the read: a stop, not a failure.
                    self._cancelled_call(model, prompt_id, started, usage)
                    raise _Cancelled from None
                failure = failure_from(raised)
            if reply is not None and not _names_model(reply, model, self.provider.name):
                failure = ProviderFailure(
                    "The AI service answered with a different model than the one asked, so the "
                    "answer was not used.",
                    status=None,
                    retryable=False,
                )
            self._record(model, prompt_id, started, reply, failure)
            if reply is not None:
                self._charge(model, reply.usage)
            if failure is None and reply is not None:
                return reply
            assert failure is not None
            if shown is not None:
                self._discard_attempt()
            if failure.retryable and attempts == 0:
                attempts += 1
                _log.warning("assistant chat call failed, asking once more: %s", failure.status)
                if self._sleep(_RETRY_WAIT_SECONDS):
                    raise _Cancelled
                continue
            raise failure

    def _cancelled_call(
        self, model: ChatModel, prompt_id: str, started: int, usage: dict[str, Any]
    ) -> None:
        """A call stopped by cancel: recorded and charged for what it used so far."""
        self._record(model, prompt_id, started, _Reply([], None, None, usage, None), None)
        self._charge(model, usage)

    def stop_streams(self) -> None:
        """Close the calls streaming now, so a cancel does not wait for the next event."""
        with self._lock:
            streams = list(self._streams)
        for stream in streams:
            try:
                stream.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                _log.debug("assistant chat stream did not close on cancel")

    def _record(
        self,
        model: ChatModel,
        prompt_id: str,
        started: int,
        reply: _Reply | None,
        error: BaseException | None,
    ) -> None:
        billed_in, out = (0, 0) if reply is None else self._weighted(model, reply.usage)
        try:
            self.ledger.record(
                model_key=model.rates_key,
                prompt_id=prompt_id,
                started_ns=started,
                input_tokens=billed_in,
                output_tokens=out,
                stop_reason=None if reply is None else reply.stop_reason,
                error=error,
            )
        except Exception:  # noqa: BLE001 - the answer still goes on; the failure is logged
            _log.exception("assistant chat call could not be recorded")

    # ---- the lead's stream on the screen ---------------------------------------------------------

    def _new_attempt(self) -> _Display:
        """The display of one streamed attempt: API content indexes to the screen's blocks."""
        shown: dict[int, int] = {}
        started: dict[int, float] = {}
        kinds: dict[int, str] = {}
        self._attempt_blocks = []

        def block_for(index: int, kind: str) -> int:
            if index not in shown:
                if kind == "thinking":
                    position = self._add_block({"type": "thinking", "text": "", "seconds": 0})
                    self.emit("thinking_start", {"index": position})
                else:
                    position = self._add_block({"type": "text", "text": ""})
                shown[index] = position
                self._attempt_blocks.append(position)
            return shown[index]

        def display(event: Mapping[str, Any]) -> None:
            kind = event.get("type")
            index = int(event.get("index", -1)) if "index" in event else -1
            if kind == "content_block_start":
                block = event.get("content_block") or {}
                kinds[index] = str(block.get("type"))
                started[index] = monotonic()
            elif kind == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "thinking_delta" and delta.get("thinking"):
                    position = block_for(index, "thinking")
                    with self._lock:
                        self.blocks[position]["text"] += str(delta["thinking"])
                    self.emit("thinking_delta", {"index": position, "text": delta["thinking"]})
                elif delta.get("type") == "text_delta" and delta.get("text"):
                    position = block_for(index, "text")
                    with self._lock:
                        self.blocks[position]["text"] += str(delta["text"])
                    self.emit("text_delta", {"index": position, "text": delta["text"]})
            elif kind == "content_block_stop":
                if kinds.get(index) == "thinking" and index in shown:
                    seconds = max(1, round(monotonic() - started.get(index, monotonic())))
                    position = shown[index]
                    with self._lock:
                        self.blocks[position]["seconds"] = seconds
                    self.emit("thinking_end", {"index": position, "seconds": seconds})

        return display

    def _discard_attempt(self) -> None:
        """Leave the last attempt's blocks out of what is stored (the screen replaces its live
        blocks with the stored message at `message_end`)."""
        with self._lock:
            self._discarded.update(self._attempt_blocks)
            self._attempt_blocks = []

    def stored_blocks(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                json.loads(json.dumps(block))
                for n, block in enumerate(self.blocks)
                if n not in self._discarded
            ]

    # ---- tools -----------------------------------------------------------------------------------

    @staticmethod
    def _result(call: Mapping[str, Any], outcome: ToolResult) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": call.get("id"),
            "content": outcome.content,
        }
        if outcome.is_error:
            result["is_error"] = True
        return result

    @staticmethod
    def _not_run(call: Mapping[str, Any], why: str) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": call.get("id"),
            "content": [{"type": "text", "text": f"Not run: {why}."}],
            "is_error": True,
        }

    def _dispatch(
        self, name: str, raw: object, step: dict[str, Any], *, helper: bool
    ) -> ToolResult:
        tools = self.tools
        if name == "bash_tool":
            return tools.bash(raw, step)
        if name == "view":
            return tools.view(raw, step, HELPER_PICTURE_EDGE_PX if helper else None)
        if name == "create_file":
            return tools.create_file(raw, step)
        if name == "str_replace":
            return tools.str_replace(raw, step)
        if not helper:
            if name == "present_files":
                return tools.present_files(raw, step)
            if name == "TaskCreate":
                return self.task_list.create(raw, step)
            if name == "TaskUpdate":
                return self.task_list.update(raw, step)
            if name == "review_results":
                return tools.review_results(raw, step)
        return ToolResult(
            content=[{"type": "text", "text": f"Refused: there is no tool {name!r}"}],
            step={**step, "ok": False, "output": f"There is no tool {name!r}."},
            is_error=True,
        )

    def _run_lead_tools(self, reply: _Reply) -> list[dict[str, Any]]:
        calls = reply.tool_uses()
        results: dict[int, dict[str, Any]] = {}
        agents = [call for call in calls if call.get("name") == "Agent"]
        for call in calls:
            if call.get("name") == "Agent":
                continue
            if self.cancel.is_set():
                results[id(call)] = self._not_run(call, "the reviewer cancelled the message")
                continue
            name = str(call.get("name"))
            if id(call) in reply.bad_inputs:
                results[id(call)] = self._not_run(call, "its input was cut off or not valid JSON")
                continue
            step = new_step(str(call.get("id")), name, call.get("input"))
            index = self._add_block({"type": "step", "step": step})
            self.emit("step_start", {"index": index, "step": step})
            outcome = self._dispatch(name, call.get("input"), step, helper=False)
            with self._lock:
                self.blocks[index] = {"type": "step", "step": outcome.step}
            self.emit("step_end", {"index": index, "step": outcome.step})
            if name in ("TaskCreate", "TaskUpdate") and outcome.step.get("tasks") is not None:
                self.emit("tasks", {"tasks": outcome.step["tasks"]})
            results[id(call)] = self._result(call, outcome)
        if agents:
            results.update(self._run_agents(agents))
        return [results[id(call)] for call in calls]

    # ---- helpers (`Agent`) -----------------------------------------------------------------------

    def _agent_call(self, call: Mapping[str, Any]) -> _Agent:
        given = arguments(
            call.get("input"),
            frozenset({"description", "prompt", "model"}),
            frozenset({"description", "prompt"}),
        )
        description = str(given["description"]).strip().replace("\n", " ")[:_MAX_DESCRIPTION]
        prompt = str(given["prompt"]).strip()
        if not description or not prompt:
            raise SandboxRefused("an agent needs a description and a prompt")
        if len(prompt) > _MAX_PROMPT_CHARS:
            raise SandboxRefused(f"the prompt is longer than {_MAX_PROMPT_CHARS} characters")
        short = given.get("model")
        if short is None:
            model = BY_SHORT[HELPER_DEFAULT_MODEL[self.settings.model.short]]
        elif isinstance(short, str) and short in BY_SHORT:
            model = BY_SHORT[short]
        else:
            raise SandboxRefused("model must be opus, sonnet or haiku")
        return _Agent(str(call.get("id")), description, prompt, model)

    def _run_agents(self, calls: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        results: dict[int, dict[str, Any]] = {}
        accepted: list[tuple[dict[str, Any], _Agent]] = []
        for call in calls:
            if self.cancel.is_set():
                results[id(call)] = self._not_run(call, "the reviewer cancelled the message")
                continue
            try:
                agent = self._agent_call(call)
            except SandboxRefused as refused:
                results[id(call)] = self._not_run(call, f"refused: {refused}")
                continue
            if len(accepted) >= MAX_AGENTS:
                results[id(call)] = self._not_run(
                    call, f"at most {MAX_AGENTS} agents run at once; start it again next turn"
                )
                continue
            accepted.append((call, agent))
        if not accepted:
            return results
        with self._lock:
            left = max(0, self.settings.message_cap_micros - self._spent)
            share = _Share(limit=self._spent + left * HELPER_SHARE_PERCENT // 100)
        first = accepted[0][1]
        count = len(accepted)
        entries: list[dict[str, Any]] = [
            {
                "id": agent.call_id,
                "description": agent.description,
                "model": agent.model.id,
                "ok": None,
                "report": None,
                "steps": [],
                "stopped": None,
            }
            for _call, agent in accepted
        ]
        step = {
            **new_step(first.call_id, "Agent", {}),
            "label": f"Running {count} agent{'s' if count > 1 else ''} · {first.description}",
            "agents": entries,
        }
        index = self._add_block({"type": "step", "step": step})
        self.emit("step_start", {"index": index, "step": json.loads(json.dumps(step))})
        for entry in entries:
            self.emit(
                "agent_start",
                {
                    "step_index": index,
                    "agent": {k: entry[k] for k in ("id", "description", "model")},
                },
            )
        started = monotonic()

        def run(position: int, agent: _Agent) -> None:
            entry = entries[position]
            try:
                report, ok, stopped = self._helper(agent, index, entry, share)
            except Exception as failed:  # noqa: BLE001 - one helper failing never stops the others
                _log.warning("assistant chat helper failed: %s", type(failed).__name__)
                report = _report_from_steps(entry["steps"], "it failed unexpectedly")
                ok, stopped = False, "error"
            with self._lock:
                entry.update({"ok": ok, "report": report, "stopped": stopped})
                summary = {
                    k: entry[k] for k in ("id", "description", "model", "ok", "report", "stopped")
                }
            self.emit("agent_end", {"step_index": index, "agent": summary})

        with ThreadPoolExecutor(max_workers=MAX_AGENTS) as pool:
            for future in [pool.submit(run, n, agent) for n, (_call, agent) in enumerate(accepted)]:
                future.result()
        with self._lock:
            finished = {
                **step,
                "label": f"Ran {count} agent{'s' if count > 1 else ''} · {first.description}",
                "ok": all(entry["ok"] for entry in entries),
                "seconds": round(monotonic() - started, 1),
                "agents": json.loads(json.dumps(entries)),
            }
            self.blocks[index] = {"type": "step", "step": finished}
        self.emit("step_end", {"index": index, "step": finished})
        for (call, agent), entry in zip(accepted, entries, strict=True):
            text = (
                f"Report from helper agent '{agent.description}' (check what it flags before you "
                "state it):\n" + str(entry["report"])
            )[:_MAX_REPORT_CHARS]
            result: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": call.get("id"),
                "content": [{"type": "text", "text": text}],
            }
            if not entry["ok"]:
                result["is_error"] = True
            results[id(call)] = result
        return results

    def _helper(
        self, agent: _Agent, step_index: int, entry: dict[str, Any], share: _Share
    ) -> tuple[str, bool, str | None]:
        """One helper's loop. Always returns a report: (report, ok, stopped)."""
        model = agent.model
        thinking = helper_thinking(model, self.settings.effort, self.settings.thinking)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": [{"type": "text", "text": agent.prompt}]}
        ]
        turns = 0
        wrap: str | None = None
        cached = previous = 0
        while True:
            if self.cancel.is_set():
                return (
                    _report_from_steps(entry["steps"], "the reviewer cancelled"),
                    False,
                    "cancelled",
                )
            final = wrap is not None
            max_tokens = WRAP_UP_MAX_TOKENS if final else HELPER_MAX_TOKENS
            body = self._body(
                model=model,
                thinking=thinking,
                system=HELPER_SYSTEM_PROMPT,
                tools=HELPER_TOOLS,
                messages=messages,
                final=final,
                max_tokens=max_tokens,
            )
            held = 0
            if not final:
                why = None
                estimate = self._estimate(model, body, cached, previous)
                if turns >= self.settings.helper_max_turns:
                    wrap, why = "turn_limit", "you have used all your tool turns"
                elif not self._fits(estimate):
                    wrap, why = "cost_cap", "this message is near its money cap"
                elif not self._hold(share, estimate):
                    wrap, why = "cost_cap", "the helpers have used their share of the money"
                if why is not None:
                    _append_note(messages, WRAP_UP_NOTE.format(why=why))
                    continue
                held = estimate or 0
            upper = prompt_tokens_upper(body, picture_sizes(messages))
            try:
                reply = self._call(
                    model=model,
                    thinking=thinking,
                    system=HELPER_SYSTEM_PROMPT,
                    tools=HELPER_TOOLS,
                    messages=messages,
                    final=final,
                    max_tokens=max_tokens,
                    prompt_id=self.helper_prompt_id,
                    display=None,
                )
            except _Cancelled:
                return (
                    _report_from_steps(entry["steps"], "the reviewer cancelled"),
                    False,
                    "cancelled",
                )
            except ProviderFailure as failure:
                return _report_from_steps(entry["steps"], str(failure)), False, "error"
            finally:
                self._release(share, held)
            cached = sum(
                int(reply.usage.get(key) or 0)
                for key in (
                    "input_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                )
            )
            previous = upper
            if reply.stop_reason == "refusal":
                return (
                    _report_from_steps(entry["steps"], "the AI service declined this task"),
                    False,
                    "error",
                )
            content = reply.history_content()
            messages.append({"role": "assistant", "content": content})
            calls = reply.tool_uses()
            if reply.stop_reason == "tool_use" and calls and not final:
                results: list[dict[str, Any]] = []
                for call in calls:
                    if self.cancel.is_set():
                        results.append(self._not_run(call, "the reviewer cancelled the message"))
                        continue
                    if id(call) in reply.bad_inputs:
                        results.append(self._not_run(call, "its input was cut off"))
                        continue
                    name = str(call.get("name"))
                    step = new_step(str(call.get("id")), name, call.get("input"))
                    outcome = self._dispatch(name, call.get("input"), step, helper=True)
                    with self._lock:
                        entry["steps"].append(outcome.step)
                    self.emit(
                        "agent_step",
                        {"step_index": step_index, "agent_id": agent.call_id, "step": outcome.step},
                    )
                    results.append(self._result(call, outcome))
                messages.append({"role": "user", "content": results})
                turns += 1
                continue
            report = reply.text()
            if not report:
                report = _report_from_steps(entry["steps"], "it gave no written report")
                return report, False, wrap or "error"
            return report, True, wrap

    # ---- the lead --------------------------------------------------------------------------------

    def run(self) -> MessageOutcome:
        """The lead's loop. Never raises: every way it ends is a stored outcome."""
        try:
            return self._run()
        except Exception as failed:  # noqa: BLE001 - the message is still stored, plainly
            _log.exception("assistant chat message failed: %s", type(failed).__name__)
            self.tools.sandbox.kill_all()
            return MessageOutcome(
                text="",
                blocks=self.stored_blocks(),
                api_content=[],
                cost_micros=self.spent,
                stopped="error",
                error="Something went wrong in the assistant, so this answer stopped. Please ask "
                "again.",
            )

    def _run(self) -> MessageOutcome:
        settings = self.settings
        model = settings.model
        thinking = thinking_config(model, settings.thinking)
        turn: list[dict[str, Any]] = [{"role": "user", "content": self.user_content}]
        answered = False
        tool_turns = 0
        forced: str | None = None
        cached = previous = 0
        stopped: str | None = None
        error: str | None = None
        cap_hit: str | None = None
        text = ""
        while True:
            if self.cancel.is_set():
                stopped = "cancelled"
                break
            if tool_turns >= settings.lead_max_turns and forced is None:
                forced = "turn_limit"
            final = forced is not None
            max_tokens = FINAL_MAX_TOKENS if forced == "cost_cap" else LEAD_MAX_TOKENS
            messages = [*self.history, *turn]
            body = self._body(
                model=model,
                thinking=thinking,
                system=LEAD_SYSTEM_PROMPT,
                tools=LEAD_TOOLS,
                messages=messages,
                final=final,
                max_tokens=max_tokens,
            )
            over = self._over(self._estimate(model, body, cached, previous))
            if over is not None:
                cap_hit = cap_hit or over
                if forced != "cost_cap":
                    # One final, smaller call told to answer now, if even that fits.
                    forced = "cost_cap"
                    _append_note(turn, COST_CAP_NOTE)
                    continue
                stopped = "cost_cap"
                break
            upper = prompt_tokens_upper(body, picture_sizes(messages))
            try:
                reply = self._call(
                    model=model,
                    thinking=thinking,
                    system=LEAD_SYSTEM_PROMPT,
                    tools=LEAD_TOOLS,
                    messages=messages,
                    final=final,
                    max_tokens=max_tokens,
                    prompt_id=self.lead_prompt_id,
                    display=self._new_attempt,
                )
            except _Cancelled:
                stopped = "cancelled"
                break
            except ProviderFailure as failure:
                stopped, error = "error", str(failure)
                break
            cached = sum(
                int(reply.usage.get(key) or 0)
                for key in (
                    "input_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                )
            )
            previous = upper
            if reply.stop_reason == "refusal":
                # A refusal's partial output is never stored or replayed.
                self._discard_attempt()
                text = _declined(reply.stop_details)
                self._add_text(text)
                break
            answered = True
            turn.append({"role": "assistant", "content": reply.history_content()})
            calls = reply.tool_uses()
            if reply.stop_reason == "tool_use" and calls and not final:
                turn.append({"role": "user", "content": self._run_lead_tools(reply)})
                tool_turns += 1
                if self.cancel.is_set():
                    stopped = "cancelled"
                    break
                if tool_turns >= settings.lead_max_turns:
                    forced = "turn_limit"
                    _append_note(turn, TURN_LIMIT_NOTE)
                continue
            if calls:
                turn.append(
                    {
                        "role": "user",
                        "content": [
                            self._not_run(call, "no more tools in this message") for call in calls
                        ],
                    }
                )
            text = reply.text()
            if reply.stop_reason == "max_tokens":
                note = "*The answer reached its length limit and was cut off here.*"
                self._add_text(note)
                text = f"{text}\n\n{note}" if text else note
            elif reply.stop_reason == "model_context_window_exceeded":
                note = "*This chat is now too long for the model. Start a new chat to go on.*"
                self._add_text(note)
                text = f"{text}\n\n{note}" if text else note
            # A cap or turn-limit stop is carried by `stopped` (the screen says it under the
            # answer); a note is written only when there is no answer to show.
            stopped = forced
            break
        if stopped in ("cost_cap", "turn_limit") and not text:
            text = _stop_note(stopped, self.settings, cap_hit)
            self._add_text(text)
        elif stopped in ("cancelled", "error"):
            # What was written before it stopped.
            text = "\n\n".join(
                block["text"].strip()
                for block in self.stored_blocks()
                if block.get("type") == "text" and block.get("text", "").strip()
            )
            _close_open_calls(turn)
        if not answered:
            api_content: list[dict[str, Any]] = []
        else:
            api_content = turn
        return MessageOutcome(
            text=text,
            blocks=self.stored_blocks(),
            api_content=json.loads(json.dumps(api_content)),
            cost_micros=self.spent,
            stopped=stopped,
            error=error,
            cap_hit=cap_hit if stopped == "cost_cap" else None,
        )


def _append_note(messages: list[dict[str, Any]], note: str) -> None:
    """A note added to the newest (not yet sent) user message, as its last text block."""
    last = messages[-1]
    if last.get("role") != "user":
        messages.append({"role": "user", "content": [{"type": "text", "text": note}]})
        return
    content = last.get("content")
    if isinstance(content, str):
        last["content"] = [{"type": "text", "text": content}, {"type": "text", "text": note}]
    elif isinstance(content, list):
        last["content"] = [*content, {"type": "text", "text": note}]


def _close_open_calls(turn: list[dict[str, Any]]) -> None:
    """After a stop, every tool call in the history gets a result (the API needs one)."""
    if not turn or turn[-1].get("role") != "assistant":
        return
    calls = [
        block
        for block in turn[-1].get("content") or []
        if isinstance(block, Mapping) and block.get("type") == "tool_use"
    ]
    if calls:
        turn.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call.get("id"),
                        "content": [{"type": "text", "text": "Not run: the message stopped."}],
                        "is_error": True,
                    }
                    for call in calls
                ],
            }
        )


def _report_from_steps(steps: Sequence[Mapping[str, Any]], why: str) -> str:
    """A helper's report when it could not write one: why, and what it did."""
    lines = [f"This helper stopped before it could write its report ({why})."]
    if steps:
        lines.append("What it did before it stopped:")
        for step in list(steps)[:40]:
            what = step.get("command") or step.get("path") or ""
            lines.append(f"- {step.get('label')}" + (f": `{what}`" if what else ""))
    else:
        lines.append("It had not used any tool yet.")
    return "\n".join(lines)


def _declined(details: Mapping[str, Any] | None) -> str:
    category = details.get("category") if isinstance(details, Mapping) else None
    why = f" (the AI service's safety check: {category})" if isinstance(category, str) else ""
    return (
        f"Claude declined to answer this request{why}. Nothing else changed; you can ask in a "
        "different way."
    )


def _stop_note(stopped: str, settings: EngineSettings, cap_hit: str | None = None) -> str:
    """The answer when a cap or the turn limit stopped the message before it wrote one."""
    if stopped == "cost_cap" and cap_hit == "package":
        return (
            f"I stopped before finishing: this drawing set has used its "
            f"${cap_text(settings.package_cap_micros)} AI budget."
        )
    if stopped == "cost_cap":
        return (
            f"I stopped before finishing: this answer reached its "
            f"${cap_text(settings.message_cap_micros)} limit. Ask a narrower question, or ask me "
            "to go on."
        )
    return (
        f"I stopped before finishing: I used all {settings.lead_max_turns} tool turns for this "
        "message. Ask me to go on."
    )
