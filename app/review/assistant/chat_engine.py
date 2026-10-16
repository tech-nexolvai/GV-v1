"""One chat message, answered the way claude.ai answers (#1463): stream, use tools, delegate, answer.

**The loop (the lead).** The turn's request is the chat's earlier API messages, replayed byte for byte
(`api_content` of every earlier answer), then this message. Each call streams: thinking and text
reach the screen as they arrive (`thinking_start`, `thinking_delta`, `thinking_end`, `text_delta`);
the reply goes back into the history unchanged (thinking blocks included). When the reply asks for
tools, they run (`step_start`, `step_end`; `tasks` after a task-list change; `file` for a
handed-over file): the read-only ones of one reply (commands, views, zooms, the recorded results)
at the same time, the others in order; then the `Agent` calls of the reply together, at most
`max_parallel_agents` at once (`agent_start`, `agent_step`, `agent_end`). All the results go back
in one user message, in the order asked, and the model is asked again. A helper's reply runs the
same way (its finding records at the same time too), and the app adds a footer to every helper's
brief (`chat_tools.helper_footer`: its pages from the set pack's page map, the zoom tool, its
budget, what to return). Up to `lead_max_turns` turns of tools; then one last call with `tool_choice: none` and a note
to answer from what it has (`stopped: turn_limit`).

**Money.** Before every call its cost is estimated (an upper bound: text at one token per two
bytes, pictures by their size, the whole output allowance, the previous call's prompt read from the
cache). The lead stops cleanly when the message's spend plus the estimate would pass the message
cap, or the drawing set's spend plus it the set's cap: first with one final, smaller call told to
answer now from what it has, else with no call; the answer says so (`stopped: cost_cap`).

**Helpers always report.** A helper runs on Sonnet when the lead is Opus (else on the lead's model)
unless the lead names one, at the lead's effort but never above `HELPER_MAX_EFFORT` (medium), and
has `helper_max_turns` turns. The helpers the lead starts in one turn
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
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from time import monotonic, monotonic_ns
from typing import Any, Final, Protocol
from uuid import uuid4

from app.review.assistant.agent import (
    HELPER_PICTURE_EDGE_PX,
    billed_input_tokens,
    estimate_micros,
    prompt_tokens_upper,
)
from app.review.assistant.chat_findings import (
    COVERAGE_CHECK_PREFIX,
    FINDING_TOOL_NAMES,
    HELPER_FINDING_TOOL_NAMES,
    FindingBook,
    MarkupFile,
    coverage_note,
    main_upload,
    make_markup_file,
    make_report_file,
    page_ranges,
    products_words,
    read_listing,
)
from app.review.assistant.chat_provider import ChatProvider, ProviderFailure, failure_from
from app.review.assistant.chat_request import (
    BY_SHORT,
    HELPER_MAX_TOKENS,
    LEAD_MAX_TOKENS,
    ChatModel,
    call_params,
    helper_effort,
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
    MAX_PARALLEL_COMMANDS,
    PARALLEL_TOOLS,
    ChatTools,
    TaskList,
    ToolResult,
    arguments,
    helper_footer,
    new_step,
)
from app.review.assistant.chat_trust import (
    CHECKS_MODULE,
    SECOND_READ_MODULE,
    Recorded,
    accuracy,
    apply_checks,
    cloud_kind,
    load_module,
    recorded_from,
    recorded_missing,
    round_statuses,
    states_drawing_values,
    trust_report,
    with_second_read,
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
#: tool's `model` text names Sonnet as the default. chat-v3: helpers run at most at medium effort.
#: chat-v4: the findings and coverage tools, and the answer-style lines. chat-v5: dismiss_recorded
#: and cite. chat-v6: the zoom tool, the set pack, parallel tools, the helper footer, the four-part
#: Agent brief and the effort lines.) Only the request's prefix changes; a chat's stored messages
#: replay unchanged.
HARNESS_ID: Final = "chat-v6"
#: Helpers one turn may start, at most, whatever the setting says.
MAX_PARALLEL_AGENTS_LIMIT: Final = 8
#: The lead's tools that run at the same time within one reply (its finding tools stay in order,
#: so the findings table keeps the order they were reported in).
LEAD_PARALLEL_TOOLS: Final = frozenset({"bash_tool", "view", "zoom", "review_results"})
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
#: Second reads (CONTRACT3): a cheap model, no tools, at most this many findings per answer.
SECOND_READ_MODEL: Final = "haiku"
SECOND_READ_EFFORT: Final = "low"
SECOND_READ_MAX_TOKENS: Final = 1024
SECOND_READ_LIMIT: Final = 30
#: An upper bound of one second read's prompt: two crops (at most 1600 px a side, about 3,400
#: tokens each) and the question.
_SECOND_READ_PROMPT_TOKENS: Final = 8_000
SECOND_READ_SYSTEM: Final = (
    "You read values printed on crops of construction drawings. Answer only from what you can "
    "see in the pictures; never guess a number you cannot read."
)
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


def second_read_prompt_id(provider: str) -> str:
    """A finding's second read, on the Usage page (counted in the chat's spend)."""
    return f"review-assistant-chat-check-{provider}-{HARNESS_ID}"


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
    max_parallel_agents: int = MAX_AGENTS
    """Helpers one turn runs at once (`GV_ASSISTANT_MAX_PARALLEL_AGENTS`, 1 to 8)."""
    mode: str = "chat"
    """`chat`, or `full_check` (CONTRACT4): GV's recorded differences are made findings at the
    start, and code makes the cloud drawing and the check report at the end."""
    products: tuple[str, ...] = ()


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
    seen: list[dict[str, Any]] = field(default_factory=list)
    """Every zoom crop of the answer, the lead's and its helpers' (`ChatTools.seen`)."""


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


class _NoRoom(Exception):
    """A second read would pass the message's caps: it is not made."""


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
    number: int = 0


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
        self.check_prompt_id = second_read_prompt_id(provider.name)
        self.blocks: list[dict[str, Any]] = []
        self._discarded: set[int] = set()
        self._lock = threading.RLock()
        self._spent = 0
        self._attempt_blocks: list[int] = []
        self._streams: set[Any] = set()
        self.task_list = TaskList()
        listing = list(tools.uploads) or read_listing(tools.workspace)
        main = main_upload(listing)
        self.book = FindingBook(
            workspace=tools.workspace,
            listing=listing,
            page_count=int(main.get("pages") or 0) if main is not None else tools.page_count,
            checker=self._checked,
        )
        self._positions: dict[str, int] = {}
        self._coverage_index: int | None = None
        self._helpers_started = 0
        self._recorded: Recorded | None = None
        self._recorded_read = False

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
    def _max_agents(self) -> int:
        return max(1, min(MAX_PARALLEL_AGENTS_LIMIT, self.settings.max_parallel_agents))

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
        effort: str,
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
                effort=effort,
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
        self, name: str, raw: object, step: dict[str, Any], *, helper: bool, by: str = "lead"
    ) -> ToolResult:
        tools = self.tools
        if name == "bash_tool":
            return tools.bash(raw, step)
        if name == "view":
            return tools.view(raw, step, HELPER_PICTURE_EDGE_PX if helper else None)
        if name == "zoom":
            return tools.zoom(raw, step, by)
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

    @staticmethod
    def _in_turns(
        calls: Sequence[dict[str, Any]],
        parallel: Callable[[dict[str, Any]], bool],
        prepare: Callable[[dict[str, Any]], Callable[[], dict[str, Any]]],
    ) -> dict[int, dict[str, Any]]:
        """Each call's result. A run of calls that `parallel` says may run together runs at the
        same time; any other call runs alone, in its place. `prepare` is called in the order
        asked (it may show a step) and returns the work."""
        results: dict[int, dict[str, Any]] = {}
        group: list[tuple[dict[str, Any], Callable[[], dict[str, Any]]]] = []

        def flush() -> None:
            if len(group) == 1:
                call, work = group[0]
                results[id(call)] = work()
            elif group:
                with ThreadPoolExecutor(max_workers=min(len(group), MAX_PARALLEL_COMMANDS)) as pool:
                    futures = [(call, pool.submit(work)) for call, work in group]
                    for call, future in futures:
                        results[id(call)] = future.result()
            group.clear()

        for call in calls:
            if parallel(call):
                group.append((call, prepare(call)))
                continue
            flush()
            results[id(call)] = prepare(call)()
        flush()
        return results

    def _run_lead_tools(self, reply: _Reply) -> list[dict[str, Any]]:
        calls = reply.tool_uses()
        agents = [call for call in calls if call.get("name") == "Agent"]

        def prepare(call: dict[str, Any]) -> Callable[[], dict[str, Any]]:
            if self.cancel.is_set():
                cancelled = self._not_run(call, "the reviewer cancelled the message")
                return lambda: cancelled
            name = str(call.get("name"))
            if id(call) in reply.bad_inputs:
                bad = self._not_run(call, "its input was cut off or not valid JSON")
                return lambda: bad
            if name in FINDING_TOOL_NAMES:
                return lambda: self._result(
                    call, self._finding_tool(name, call.get("input"), "lead")
                )
            # The step shows at once, in the order asked, even when its work runs alongside others.
            step = new_step(str(call.get("id")), name, call.get("input"))
            index = self._add_block({"type": "step", "step": step})
            self.emit("step_start", {"index": index, "step": step})

            def work() -> dict[str, Any]:
                outcome = self._dispatch(name, call.get("input"), step, helper=False)
                with self._lock:
                    self.blocks[index] = {"type": "step", "step": outcome.step}
                self.emit("step_end", {"index": index, "step": outcome.step})
                if name in ("TaskCreate", "TaskUpdate") and outcome.step.get("tasks") is not None:
                    self.emit("tasks", {"tasks": outcome.step["tasks"]})
                return self._result(call, outcome)

            return work

        results = self._in_turns(
            [call for call in calls if call.get("name") != "Agent"],
            lambda call: call.get("name") in LEAD_PARALLEL_TOOLS
            and id(call) not in reply.bad_inputs,
            prepare,
        )
        if agents:
            results.update(self._run_agents(agents))
        return [results[id(call)] for call in calls]

    # ---- findings, coverage and trust (chat v2 / v3) ---------------------------------------------

    def _recorded_checks(self) -> Recorded:
        """What GV Review's own checks recorded for the set, read once per message."""
        with self._lock:
            if self._recorded_read and self._recorded is not None:
                return self._recorded
        loader = self.tools.results
        recorded = Recorded(readable=False)
        if loader is not None:
            try:
                recorded = recorded_from(loader(None))
            except Exception as failed:  # noqa: BLE001 - the report says it could not be read
                _log.warning("assistant chat could not read the records: %s", type(failed).__name__)
        with self._lock:
            self._recorded, self._recorded_read = recorded, True
        return recorded

    def _checked(self, finding: dict[str, Any]) -> dict[str, Any]:
        """A finding with code's checks (its second read starts again: its values may be new)."""
        recorded = self._recorded_checks() if finding.get("source") == "rules" else None
        checked = apply_checks(finding, self.book.pdf_paths(), recorded)
        checked.setdefault("round_status", None)
        checked.setdefault("second_read_values", None)
        return checked

    def _show_finding(self, finding: dict[str, Any]) -> None:
        """A new or changed finding: its block (replaced in place) and the `finding` event."""
        with self._lock:
            index = self._positions.get(finding["id"])
            if index is None:
                self.blocks.append({"type": "finding", "finding": finding})
                index = len(self.blocks) - 1
                self._positions[finding["id"]] = index
            else:
                self.blocks[index] = {"type": "finding", "finding": finding}
        self.emit("finding", {"index": index, "finding": json.loads(json.dumps(finding))})

    def _show_coverage(self, coverage: dict[str, Any]) -> None:
        with self._lock:
            index = self._coverage_index
            if index is None:
                self.blocks.append({"type": "coverage", "coverage": coverage})
                index = self._coverage_index = len(self.blocks) - 1
            else:
                self.blocks[index] = {"type": "coverage", "coverage": coverage}
        self.emit("coverage", {"index": index, "coverage": json.loads(json.dumps(coverage))})

    def _finding_tool(self, name: str, raw: object, by: str) -> ToolResult:
        """`report_finding`, `update_finding`, `drop_finding`, `set_coverage`: no step on the
        screen; the finding and coverage blocks are what the reviewer sees."""
        book = self.book
        try:
            if name == "report_finding":
                finding, notes = book.report(raw, by)
                self._show_finding(finding)
                text = f"Finding {finding['id']} recorded."
            elif name == "update_finding":
                finding, notes = book.update(raw)
                self._show_finding(finding)
                text = f"Finding {finding['id']} updated."
            elif name == "cite":
                module = load_module(CHECKS_MODULE)

                def printed(path: Path, page: int, box: Mapping[str, Any], value: str) -> str:
                    if module is None:
                        return "not_run"
                    return str(module.printed_at_spot(path, page, box, value))

                cite = book.cite(raw, printed)
                notes = []
                index = self._add_block({"type": "cite", "cite": cite})
                self.emit("cite", {"index": index, "cite": json.loads(json.dumps(cite))})
                text = f"Cited {cite['id']}: {cite['printed'].replace('_', ' ')}."
            elif name == "dismiss_recorded":
                record_id, reason = book.dismiss(raw)
                notes = []
                text = f"Recorded difference {record_id} dismissed: {reason}"
            elif name == "drop_finding":
                finding_id, reason = book.drop(raw)
                notes = []
                with self._lock:
                    dropped_at = self._positions.get(finding_id)
                    block = {"type": "finding_dropped", "finding_id": finding_id, "reason": reason}
                    if dropped_at is not None:
                        self.blocks[dropped_at] = block
                self.emit("finding_dropped", {"index": dropped_at, **block})
                text = f"Finding {finding_id} dropped."
            else:
                coverage = book.set_coverage(raw, by)
                self._show_coverage(coverage)
                counts = coverage["counts"]
                gaps = book.coverage_gaps()
                text = (
                    f"Coverage: {counts['same']} same, {counts['different']} different, "
                    f"{counts['not_comparable']} not comparable, of {counts['total']} pages."
                )
                if gaps:
                    text += f" Not accounted for yet: pages {page_ranges(gaps)}."
                notes = []
        except SandboxRefused as refused:
            return ToolResult(
                content=[{"type": "text", "text": f"Refused: {refused}"}],
                step={},
                is_error=True,
            )
        if notes:
            text += " " + "; ".join(notes) + "."
        return ToolResult(content=[{"type": "text", "text": text}], step={})

    def _second_read_call(self, content: list[dict[str, Any]], share: _Share) -> str:
        """One second read: the cheap model, no tools, within the message's caps."""
        model = BY_SHORT[SECOND_READ_MODEL]
        thinking = thinking_config(model, False)
        estimate = self._price(model, _SECOND_READ_PROMPT_TOKENS, SECOND_READ_MAX_TOKENS)
        if not self._fits(estimate) or not self._hold(share, estimate):
            raise _NoRoom
        try:
            reply = self._call(
                model=model,
                effort=SECOND_READ_EFFORT,
                thinking=thinking,
                system=SECOND_READ_SYSTEM,
                tools=(),
                messages=[{"role": "user", "content": content}],
                final=False,
                max_tokens=SECOND_READ_MAX_TOKENS,
                prompt_id=self.check_prompt_id,
                display=None,
            )
        finally:
            self._release(share, estimate or 0)
        if reply.stop_reason == "refusal":
            raise ProviderFailure("declined", status=None, retryable=False)
        return reply.text()

    def _second_reads(self) -> int:
        """Second reads of the findings with evidence pictures, in parallel (at most
        `SECOND_READ_LIMIT`); returns how many findings were left without one."""
        module = load_module(SECOND_READ_MODULE)
        findings = [
            f for f in self.book.live() if f.get("arch_picture_id") or f.get("vendor_picture_id")
        ]
        if module is None or not findings:
            return len(findings) if module is None else 0
        # Differences first: they are what a reviewer acts on.
        findings.sort(key=lambda f: {"different": 0, "check": 1}.get(str(f.get("status")), 2))
        chosen, skipped = findings[:SECOND_READ_LIMIT], len(findings) - SECOND_READ_LIMIT
        share = _Share(limit=self.settings.message_cap_micros)
        left = [max(0, skipped)]

        def one(finding: dict[str, Any]) -> None:
            if self.cancel.is_set():
                left[0] += 1
                return
            crops: dict[str, bytes] = {}
            for side, key in (("architect", "arch_picture_id"), ("vendor", "vendor_picture_id")):
                if finding.get(key):
                    try:
                        crops[side] = self.read_picture(str(finding[key]))
                    except OSError:
                        continue
            try:
                read = module.second_read(
                    crops,
                    str(finding.get("item") or ""),
                    lambda content: self._second_read_call(content, share),
                )
                read = module.compare_read(read, finding)
                result = str(read.result)
                values = {"architect": read.architect, "vendor": read.vendor}
            except (_NoRoom, _Cancelled, ProviderFailure):
                left[0] += 1
                return
            except Exception as failed:  # noqa: BLE001 - a failed read is "not run"
                _log.warning("assistant chat second read failed: %s", type(failed).__name__)
                left[0] += 1
                return
            if result not in ("agrees", "disagrees", "unsure"):
                result = "unsure"
            current = next((f for f in self.book.live() if f["id"] == finding["id"]), None)
            if current is None:
                return
            updated = {**with_second_read(current, result), "second_read_values": values}
            if self.book.replace(updated):
                self._show_finding(updated)

        with ThreadPoolExecutor(max_workers=MAX_AGENTS) as pool:
            for future in [pool.submit(one, finding) for finding in chosen]:
                future.result()
        return left[0]

    def _accuracy(self) -> None:
        """The answer's accuracy, by code (CONTRACT5): a block and the `accuracy` event."""
        book = self.book
        whole_set = book.coverage_used or self.settings.mode == "full_check"
        score = accuracy(book.live(), list(book.cites), book.coverage_map() if whole_set else None)
        index = self._add_block({"type": "accuracy", "accuracy": score})
        self.emit("accuracy", {"index": index, "accuracy": json.loads(json.dumps(score))})

    def _trust_phase(self) -> None:
        """After the lead's answer: second reads, round links, then the trust report."""
        book = self.book
        if not book.live() and not book.coverage_used:
            return
        skipped = self._second_reads()
        statuses = round_statuses(book.live())
        for finding in book.live():
            status = statuses.get(finding["id"])
            if status is not None and finding.get("round_status") != status:
                updated = {**finding, "round_status": status}
                if book.replace(updated):
                    self._show_finding(updated)
        report = trust_report(
            book.live(),
            book.coverage_map() if book.coverage_used else None,
            self._recorded_checks(),
            second_reads_skipped=skipped,
            dismissed=book.dismissed,
        )
        index = self._add_block({"type": "trust", "trust": report})
        self.emit("trust", {"index": index, "trust": json.loads(json.dumps(report))})

    def _recorded_missing(self) -> list[dict[str, Any]]:
        """GV Review's recorded differences this answer neither reports nor dismissed (whole-set
        work: a full check, or an answer that used coverage)."""
        if self.settings.mode != "full_check" and not self.book.coverage_used:
            return []
        recorded = self._recorded_checks()
        if not recorded.readable:
            return []
        missing = set(recorded_missing(self.book.live(), recorded, self.book.dismissed))
        return [item for item in recorded.differences if item["record_id"] in missing]

    def _has_evidence(self) -> bool:
        return any(f.get("arch_box") or f.get("vendor_box") for f in self.book.live()) or any(
            cite.get("box") for cite in self.book.cites
        )

    def _coverage_gaps_note(self, answer: str = "") -> str | None:
        """The finish check's note: pages unaccounted for (when coverage was used), GV Review's
        recorded differences the answer lost, and drawing values stated with no evidence."""
        parts: list[str] = []
        if states_drawing_values(answer) and not self._has_evidence():
            parts.append(
                f"{COVERAGE_CHECK_PREFIX} show the evidence: your answer states values read off "
                "the drawings with no evidence. Back each with report_finding (a comparison) or "
                "cite (a value), with boxes; then give your answer."
            )
        if self.book.coverage_used:
            gaps = self.book.coverage_gaps()
            if gaps:
                parts.append(coverage_note(gaps, self.book.main_file))
        missing = self._recorded_missing()
        if missing:
            listed = "; ".join(
                f"{item['record_id']} (page {item.get('page')}: {item.get('what')})"
                for item in missing[:40]
            )
            more = f" and {len(missing) - 40} more" if len(missing) > 40 else ""
            lead = "" if parts else f"{COVERAGE_CHECK_PREFIX} "  # one prefix per note
            parts.append(
                f"{lead}GV Review recorded these differences and your answer neither reports nor "
                f"dismisses them: {listed}{more}. For each, report_finding with its record_id "
                "(after checking it on the drawing) or dismiss_recorded with your reason; then "
                "give your answer."
            )
        return " ".join(parts) or None

    def _seed_recorded(self) -> None:
        """Full check: each difference GV Review recorded becomes a finding at once, so none is
        lost; the lead is told which, to check each (or drop it with a reason)."""
        recorded = self._recorded_checks()
        if not recorded.readable:
            return
        self.book.record_ids = frozenset(item["record_id"] for item in recorded.differences)
        seeded = []
        for item in recorded.differences:
            finding = self.book.seed(item)
            if finding is None:
                continue
            self._show_finding(finding)
            seeded.append(
                f"- {finding['id']} page {finding['page']}: {finding['item']}: architect "
                f"{finding['architect']}; vendor {finding['vendor']} ({item['record_id']})"
            )
        if seeded:
            self.user_content = [
                *self.user_content,
                {
                    "type": "text",
                    "text": "GV Review already recorded these differences; the app made a "
                    "finding for each. Check each on the drawing: update_finding (boxes, values "
                    "as printed, verified true) or drop_finding with your reason. Never leave "
                    "one out.\n" + "\n".join(seeded),
                },
            ]

    def _full_check_files(self) -> None:
        """Full check: the cloud drawing and the check report, made by code, as chat files."""
        register: Callable[[str, str, int, str, str], dict[str, Any]] | None = (
            self.tools.register_file
        )
        if register is None:
            return
        book, workspace = self.book, self.tools.workspace
        findings = book.live()
        report: dict[str, Any] = next(
            (b["trust"] for b in reversed(self.stored_blocks()) if b.get("type") == "trust"), {}
        )
        counts = {
            kind: sum(1 for f in findings if cloud_kind(f) == key)
            for kind, key in (("wrong", "wrong"), ("to check", "check"), ("right", None))
        }
        coverage = book.coverage_map()["counts"] if book.coverage_used else None
        trust = report.get("counts") or {}
        products = products_words(self.settings.products)
        summary = {
            "Drawing set": book.main_file or "",
            "Products": products,
            "Made": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            "Model": f"{self.settings.model.label}, {self.settings.effort} effort",
            "Items": f"{counts['wrong']} wrong, {counts['to check']} to check, "
            f"{counts['right']} right",
            "Coverage": (
                "not recorded"
                if coverage is None
                else f"{coverage['total'] - coverage['not_opened']} of {coverage['total']} pages "
                f"accounted for ({coverage['not_comparable']} not comparable)"
            ),
            "Trust": f"{trust.get('confirmed', 0)} confirmed, {trust.get('likely', 0)} likely, "
            f"{trust.get('to_check', 0)} to check",
        }
        not_checked = list(report.get("not_checked") or [])
        for item in (report.get("gv_checks") or {}).get("left_out") or []:
            not_checked.append(
                f"GV Review's recorded difference {item.get('record_id')} (page {item.get('page')}: "
                f"{item.get('what')}) is not in this answer."
            )

        def drawing(out: Path) -> MarkupFile:
            return make_markup_file(workspace, findings, out, "AI cloud drawing")

        def checked(out: Path) -> MarkupFile:
            return make_report_file(workspace, summary, findings, not_checked, out)

        makers: tuple[tuple[str, Callable[[Path], MarkupFile]], ...] = (
            ("cloud drawing", drawing),
            ("check report", checked),
        )
        for what, make in makers:
            file_id = str(uuid4())
            out = workspace.files / file_id
            try:
                made = make(out)
                register(file_id, made.name, made.size, "application/pdf", made.sha256)
            except Exception as failed:  # noqa: BLE001 - told plainly in the answer
                _log.warning("assistant chat %s failed: %s", what, type(failed).__name__)
                out.unlink(missing_ok=True)
                self._add_text(f"*The {what} could not be made for this answer.*")

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
            if len(accepted) >= self._max_agents:
                results[id(call)] = self._not_run(
                    call,
                    f"at most {self._max_agents} agents run at once; start it again next turn",
                )
                continue
            with self._lock:
                self._helpers_started += 1
                agent.number = self._helpers_started
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

        with ThreadPoolExecutor(max_workers=self._max_agents) as pool:
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
            reported = self.book.by(f"helper:{agent.number}")
            listed = "".join(
                f"\n- {f['id']} page {f['page']}: {f['item']}: architect {f['architect']}; "
                f"vendor {f['vendor']} ({f['status']})"
                for f in reported
            )
            if listed:
                listed = (
                    "\n\nFindings it reported (not verified: check each, then update_finding "
                    "with verified true, correct it, or drop_finding):" + listed
                )
            text = (
                f"Report from helper agent '{agent.description}' (check what it flags before you "
                "state it):\n" + str(entry["report"])
            )[: _MAX_REPORT_CHARS - len(listed)] + listed
            result: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": call.get("id"),
                "content": [{"type": "text", "text": text}],
            }
            if not entry["ok"]:
                result["is_error"] = True
            results[id(call)] = result
        return results

    def _helper_tool(
        self,
        agent: _Agent,
        step_index: int,
        entry: dict[str, Any],
        call: dict[str, Any],
        bad: set[int],
    ) -> Callable[[], dict[str, Any]]:
        """One of a helper's tool calls, as work to run (`_in_turns`)."""
        if self.cancel.is_set():
            cancelled = self._not_run(call, "the reviewer cancelled the message")
            return lambda: cancelled
        if id(call) in bad:
            cut = self._not_run(call, "its input was cut off")
            return lambda: cut
        name = str(call.get("name"))
        by = f"helper:{agent.number}"
        if name in HELPER_FINDING_TOOL_NAMES:
            return lambda: self._result(call, self._finding_tool(name, call.get("input"), by))

        def work() -> dict[str, Any]:
            step = new_step(str(call.get("id")), name, call.get("input"))
            outcome = self._dispatch(name, call.get("input"), step, helper=True, by=by)
            with self._lock:
                entry["steps"].append(outcome.step)
            self.emit(
                "agent_step",
                {"step_index": step_index, "agent_id": agent.call_id, "step": outcome.step},
            )
            return self._result(call, outcome)

        return work

    def _helper(
        self, agent: _Agent, step_index: int, entry: dict[str, Any], share: _Share
    ) -> tuple[str, bool, str | None]:
        """One helper's loop. Always returns a report: (report, ok, stopped)."""
        model = agent.model
        effort = helper_effort(self.settings.effort)
        thinking = helper_thinking(model, effort, self.settings.thinking)
        try:
            footer = helper_footer(agent.prompt, self.tools.workspace)
        except Exception:  # noqa: BLE001 - the helper still gets its brief
            _log.warning("assistant chat helper footer could not be made")
            footer = ""
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": [{"type": "text", "text": agent.prompt + footer}]}
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
                    effort=effort,
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
                bad = reply.bad_inputs

                def parallel(call: dict[str, Any], bad: set[int] = bad) -> bool:
                    return call.get("name") in PARALLEL_TOOLS and id(call) not in bad

                def prepare(
                    call: dict[str, Any], bad: set[int] = bad
                ) -> Callable[[], dict[str, Any]]:
                    return self._helper_tool(agent, step_index, entry, call, bad)

                done = self._in_turns(calls, parallel, prepare)
                results = [done[id(call)] for call in calls]
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
        guard_sent = False
        if settings.mode == "full_check":
            try:
                self._seed_recorded()
            except Exception:  # noqa: BLE001 - the check goes on; the trust report says it
                _log.exception("assistant chat could not seed the recorded differences")
        turn[0] = {"role": "user", "content": self.user_content}
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
                    effort=settings.effort,
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
            note = None if final or calls or guard_sent else self._coverage_gaps_note(reply.text())
            if note is not None and reply.stop_reason == "end_turn":
                # The whole-set guard: once, and only when the caps leave room for more work.
                guard_sent = True
                probe = [*turn, {"role": "user", "content": [{"type": "text", "text": note}]}]
                estimate = self._estimate(
                    model,
                    self._body(
                        model=model,
                        thinking=thinking,
                        system=LEAD_SYSTEM_PROMPT,
                        tools=LEAD_TOOLS,
                        messages=[*self.history, *probe],
                        final=False,
                        max_tokens=LEAD_MAX_TOKENS,
                    ),
                    cached,
                    previous,
                )
                if tool_turns < settings.lead_max_turns and self._fits(estimate):
                    turn.append(probe[-1])
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
        if stopped not in ("cancelled", "error"):
            try:
                self._trust_phase()
            except Exception:  # noqa: BLE001 - the answer is stored without its trust report
                _log.exception("assistant chat trust report failed")
            if settings.mode == "full_check" and not self.cancel.is_set():
                self._full_check_files()
        try:
            self._accuracy()
        except Exception:  # noqa: BLE001 - the answer is stored without its score
            _log.exception("assistant chat accuracy failed")
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
            seen=[dict(entry) for entry in self.tools.seen],
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
