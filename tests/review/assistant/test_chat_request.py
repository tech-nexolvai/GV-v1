"""The chat's call, exactly as it leaves the process (#1463): no network, no paid call.

- Each model, effort and thinking choice goes into the call per Anthropic's rules (the exact JSON),
  and the combinations the API would refuse are refused first, in plain words.
- The provider is chosen from the keys present; the official SDK, run over a fake transport, sends
  the call to the right place with the right key only, and its stream is read back exactly.
- No key ever reaches an error, a log line or a repr.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx2
import pytest

from app.review.assistant.chat_engine import _accumulate, prompt_ids
from app.review.assistant.chat_provider import (
    RETENTION_WORDS,
    AnthropicSdkProvider,
    ProviderChoice,
    ProviderFailure,
    choose_provider,
    failure_from,
)
from app.review.assistant.chat_request import (
    BINDING_BETA,
    EFFORTS,
    FALLBACK_BETA,
    HELPER_MAX_EFFORT,
    MODELS,
    ChoiceRefused,
    call_params,
    check_choice,
    helper_effort,
    helper_shorts,
    helper_thinking,
    inline_pictures,
    rates_key_for,
    thinking_config,
)
from app.review.assistant.chat_tools import HELPER_SYSTEM_PROMPT, LEAD_SYSTEM_PROMPT, LEAD_TOOLS
from app.usage_history import infer_purpose, infer_route
from tests.review.assistant.chat_fakes import reply, text, thinking, tool

ANTHROPIC_KEY = "sk-ant-api03-TESTKEY-never-real"
OPENROUTER_KEY = "sk-or-v1-TESTKEY-never-real"
MESSAGES = [{"role": "user", "content": [{"type": "text", "text": "Hi"}]}]


def _params(model: str, effort: str, on: bool, *, provider: str = "anthropic", final: bool = False):  # type: ignore[no-untyped-def]
    chosen = check_choice(model, effort, on)
    return call_params(
        provider=provider,  # type: ignore[arg-type]
        model=chosen,
        effort=effort,
        thinking=thinking_config(chosen, on),
        system="SYSTEM",
        tools=[{"name": "a", "input_schema": {}}, {"name": "b", "input_schema": {}}],
        messages=MESSAGES,
        final=final,
        max_tokens=64000,
    )


def test_opus_always_thinks_with_the_summary_shown_and_the_refusal_fallback() -> None:
    assert _params("claude-opus-5-5", "high", True) == {
        "model": "claude-opus-5-5",
        "max_tokens": 64000,
        "system": [{"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}],
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "Hi", "cache_control": {"type": "ephemeral"}}],
            }
        ],
        "tools": [
            {"name": "a", "input_schema": {}},
            {"name": "b", "input_schema": {}, "cache_control": {"type": "ephemeral"}},
        ],
        "tool_choice": {"type": "auto"},
        "thinking": {
            "type": "adaptive",
            "display": "summarized",
            "block_binding": {"prefix_mismatch_behavior": "drop_block"},
        },
        "output_config": {"effort": "high"},
        "betas": ["thinking-binding-controls-2026-08-01", "server-side-fallback-2026-07-01"],
        "fallbacks": "default",
    }


def test_fable_always_thinks_with_the_summary_shown_the_fallback_and_the_binding_net() -> None:
    """#1577, Anthropic's Fable 5.1 guide: adaptive thinking only (no `disabled`, no budget),
    effort the only depth control, `fallbacks: "default"` with its beta, no forced tool."""
    params = _params("claude-fable-5-1", "xhigh", True)
    assert params["model"] == "claude-fable-5-1"
    assert params["thinking"] == {
        "type": "adaptive",
        "display": "summarized",
        "block_binding": {"prefix_mismatch_behavior": "drop_block"},
    }
    assert params["output_config"] == {"effort": "xhigh"}
    assert params["betas"] == [BINDING_BETA, FALLBACK_BETA] and params["fallbacks"] == "default"
    assert params["tool_choice"] == {"type": "auto"}
    assert _params("claude-fable-5-1", "low", True, final=True)["tool_choice"] == {"type": "none"}
    flat = json.dumps(params)
    for banned in ("budget_tokens", "temperature", "top_p", '"disabled"', "between_tools"):
        assert banned not in flat
    fable = MODELS["claude-fable-5-1"]
    assert fable.label == "Claude Fable 5.1" and fable.thinking == "always"
    assert fable.thinking_off is None and fable.thinking_off_max_effort is None
    assert fable.rates_key == "anthropic.claude-fable-5-1" and fable.providers == ("anthropic",)


@pytest.mark.parametrize("model", sorted(MODELS))
@pytest.mark.parametrize("final", [False, True])
def test_no_model_is_ever_sent_a_forced_tool_choice(model: str, final: bool) -> None:
    """`any` and `tool` are a 400 on Fable 5.1 and the 5.5 models: only `auto`, or `none` for a
    forced final answer."""
    params = _params(model, "high", True, final=final)
    assert params["tool_choice"] in ({"type": "auto"}, {"type": "none"})
    assert params["tool_choice"] == ({"type": "none"} if final else {"type": "auto"})


def test_fable_is_refused_on_openrouter_in_plain_words() -> None:
    with pytest.raises(ChoiceRefused, match="only on Anthropic's own API"):
        check_choice("claude-fable-5-1", "high", True, provider="openrouter")
    assert check_choice("claude-fable-5-1", "high", True, provider="anthropic").short == "fable"
    assert check_choice("claude-opus-5-5", "high", True, provider="openrouter").short == "opus"


def test_fable_never_runs_as_a_helper_and_a_fallback_model_has_a_price_key() -> None:
    assert helper_shorts() == ("opus", "sonnet", "haiku")
    assert rates_key_for("claude-fable-5-1") == "anthropic.claude-fable-5-1"
    assert rates_key_for("claude-opus-4-8") == "anthropic.claude-opus-4-8"
    assert rates_key_for("claude-opus-5-20260901") == "anthropic.claude-opus-5"
    assert rates_key_for("anthropic/claude-opus-5.5") == "anthropic.claude-opus-5.5"


@pytest.mark.parametrize(
    ("model", "effort", "on", "expected"),
    [
        ("claude-fable-5-1", "max", True, {"type": "adaptive", "display": "summarized"}),
        ("claude-fable-5-1", "low", True, {"type": "adaptive", "display": "summarized"}),
        ("claude-opus-5-5", "max", True, {"type": "adaptive", "display": "summarized"}),
        ("claude-opus-5-5", "low", True, {"type": "adaptive", "display": "summarized"}),
        ("claude-sonnet-5-5", "xhigh", True, {"type": "adaptive", "display": "summarized"}),
        ("claude-sonnet-5-5", "high", False, {"type": "between_tools"}),
        ("claude-sonnet-5-5", "low", False, {"type": "between_tools"}),
        ("claude-haiku-5-5", "medium", True, {"type": "adaptive", "display": "summarized"}),
        ("claude-haiku-5-5", "high", False, {"type": "disabled"}),
    ],
)
def test_each_choice_sends_its_thinking_and_effort(
    model: str, effort: str, on: bool, expected: dict[str, str]
) -> None:
    params = _params(model, effort, on)
    thinking = dict(params["thinking"])
    binding = thinking.pop("block_binding", None)
    assert thinking == expected
    # The preserved-thinking safety net rides on adaptive thinking only (#1577): `between_tools`
    # and `disabled` take no other field.
    if expected["type"] == "adaptive":
        assert binding == {"prefix_mismatch_behavior": "drop_block"}
        assert BINDING_BETA in params["betas"]
    else:
        assert binding is None and BINDING_BETA not in params.get("betas", [])
    assert params["output_config"] == {"effort": effort}
    assert params["model"] == model
    # Never a budget, a temperature or a forced tool.
    flat = json.dumps(params)
    for banned in ("budget_tokens", "temperature", "top_p", '"any"'):
        assert banned not in flat


def test_haiku_has_no_server_side_fallback() -> None:
    params = _params("claude-haiku-5-5", "low", True)
    assert params["betas"] == [BINDING_BETA] and "fallbacks" not in params
    assert "betas" not in _params("claude-haiku-5-5", "low", False)


def test_sonnet_takes_the_default_fallback_form_only() -> None:
    params = _params("claude-sonnet-5-5", "medium", False)
    assert params["betas"] == ["server-side-fallback-2026-07-01"]
    assert "block_binding" not in params["thinking"]
    assert params["fallbacks"] == "default"


def test_a_forced_final_answer_is_tool_choice_none() -> None:
    assert _params("claude-opus-5-5", "high", True, final=True)["tool_choice"] == {"type": "none"}


def test_openrouter_gets_its_names_and_the_keep_no_data_routing_and_no_betas() -> None:
    params = _params("claude-opus-5-5", "high", True, provider="openrouter")
    assert params["model"] == "anthropic/claude-opus-5.5"
    assert "betas" not in params and "fallbacks" not in params
    assert params["extra_body"] == {
        "provider": {
            "only": ["google-vertex/global"],
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
            "zdr": True,
        }
    }
    assert _params("claude-haiku-5-5", "low", True, provider="openrouter")["model"] == (
        "anthropic/claude-haiku-5.5"
    )


@pytest.mark.parametrize(
    ("model", "effort", "on", "words"),
    [
        ("claude-opus-5-5", "high", False, "always on"),
        ("claude-sonnet-5-5", "xhigh", False, "high or below"),
        ("claude-sonnet-5-5", "max", False, "high or below"),
        ("claude-haiku-5-5", "max", False, "high or below"),
        ("claude-fable-5-1", "high", False, "always on"),
        ("claude-fable-5", "high", True, "not one of the models"),
        ("claude-opus-5-5", "extreme", True, "effort must be"),
    ],
)
def test_choices_the_api_would_refuse_are_refused_first(
    model: str, effort: str, on: bool, words: str
) -> None:
    with pytest.raises(ChoiceRefused, match=words):
        check_choice(model, effort, on)


def test_a_helper_on_another_model_never_gets_a_setting_it_refuses() -> None:
    opus, sonnet = MODELS["claude-opus-5-5"], MODELS["claude-sonnet-5-5"]
    assert helper_thinking(opus, "high", False) == {"type": "adaptive", "display": "summarized"}
    assert helper_thinking(sonnet, "high", False) == {"type": "between_tools"}
    assert helper_thinking(sonnet, "max", False) == {"type": "adaptive", "display": "summarized"}


@pytest.mark.parametrize(
    ("lead", "helper"),
    [
        ("low", "low"),
        ("medium", "medium"),
        ("high", "medium"),
        ("xhigh", "medium"),
        ("max", "medium"),
    ],
)
def test_a_helper_thinks_at_the_leads_effort_but_never_above_medium(lead: str, helper: str) -> None:
    assert HELPER_MAX_EFFORT == "medium"
    assert helper_effort(lead) == helper


def _accepted(model: str, effort: str, setting: dict[str, str]) -> bool:
    """Anthropic's rules: adaptive everywhere; Sonnet 5.5's `between_tools` and Haiku 5.5's
    `disabled` only at effort high or below; Opus 5.5 never thinks off."""
    if setting == {"type": "adaptive", "display": "summarized"}:
        return True
    off = effort in ("low", "medium", "high")
    if setting == {"type": "between_tools"}:
        return model == "claude-sonnet-5-5" and off
    if setting == {"type": "disabled"}:
        return model == "claude-haiku-5-5" and off
    return False


@pytest.mark.parametrize("model", sorted(MODELS))
@pytest.mark.parametrize("effort", EFFORTS)
@pytest.mark.parametrize("on", [True, False])
def test_a_helpers_thinking_is_accepted_for_its_model_at_its_effort(
    model: str, effort: str, on: bool
) -> None:
    chosen = MODELS[model]
    # At any effort it is given, and at the effort a helper actually runs at under that lead.
    assert _accepted(model, effort, helper_thinking(chosen, effort, on))
    capped = helper_effort(effort)
    setting = helper_thinking(chosen, capped, on)
    assert _accepted(model, capped, setting)
    # Under the cap, a helper whose model can think off follows the lead's "thinking off".
    if not on and chosen.thinking_off is not None:
        assert setting == dict(chosen.thinking_off)


def test_pictures_are_put_back_the_same_way_every_time() -> None:
    stored = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "v",
                    "content": [
                        {"type": "text", "text": "Picture"},
                        {
                            "type": "image",
                            "source": {
                                "type": "gv_picture",
                                "picture_id": "p1",
                                "width": 2,
                                "height": 2,
                            },
                        },
                    ],
                }
            ],
        }
    ]
    once = inline_pictures(stored, lambda _pid: b"\x89PNG-bytes")
    twice = inline_pictures(json.loads(json.dumps(stored)), lambda _pid: b"\x89PNG-bytes")
    assert json.dumps(once) == json.dumps(twice)
    assert once[0]["content"][0]["content"][1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "iVBORy1ieXRlcw=="},
    }
    assert stored[0]["content"][0]["content"][1]["source"]["type"] == "gv_picture"


# ---- the provider ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setting", "anthropic", "openrouter", "name", "available"),
    [
        ("auto", ANTHROPIC_KEY, OPENROUTER_KEY, "anthropic", True),
        ("auto", None, OPENROUTER_KEY, "openrouter", True),
        ("auto", "  ", OPENROUTER_KEY, "openrouter", True),
        ("auto", None, None, "openrouter", False),
        ("anthropic", None, OPENROUTER_KEY, "anthropic", False),
        ("openrouter", ANTHROPIC_KEY, OPENROUTER_KEY, "openrouter", True),
        ("openrouter", ANTHROPIC_KEY, None, "openrouter", False),
    ],
)
def test_the_provider_follows_the_keys_present(
    setting: str, anthropic: str | None, openrouter: str | None, name: str, available: bool
) -> None:
    choice = choose_provider(setting, anthropic, openrouter)  # type: ignore[arg-type]
    assert choice.name == name and choice.available is available
    assert (choice.reason is None) is available
    assert ANTHROPIC_KEY not in repr(choice) and OPENROUTER_KEY not in repr(choice)


def _sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


class _Transport:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(
            self.status,
            headers={
                "content-type": "text/event-stream" if self.status == 200 else "application/json"
            },
            content=self.body,
        )


def _provider(name: str, key: str, transport: _Transport) -> AnthropicSdkProvider:
    import anthropic

    return AnthropicSdkProvider(
        ProviderChoice(name, key),  # type: ignore[arg-type]
        timeout_seconds=30,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(transport)),
    )


def _drain(stream: Any) -> Iterator[dict[str, Any]]:
    try:
        yield from stream
    finally:
        stream.close()


def test_anthropic_direct_streams_with_its_key_and_the_fallback_beta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_KEY)
    events = reply(
        thinking("Hmm."), text("Hello."), tool("t", "view", {"path": "/x"}), stop="tool_use"
    )
    transport = _Transport(200, _sse(events))
    provider = _provider("anthropic", ANTHROPIC_KEY, transport)
    params = _params("claude-opus-5-5", "high", True)

    got = list(_drain(provider.open_stream(params)))

    request = transport.requests[0]
    assert str(request.url).startswith("https://api.anthropic.com/v1/messages")
    assert request.headers["x-api-key"] == ANTHROPIC_KEY
    assert "server-side-fallback-2026-07-01" in request.headers["anthropic-beta"]
    assert "thinking-binding-controls-2026-08-01" in request.headers["anthropic-beta"]
    assert OPENROUTER_KEY not in str(request.headers)
    body = json.loads(request.content)
    assert body["stream"] is True and body["fallbacks"] == "default"
    assert body["thinking"] == {
        "type": "adaptive",
        "display": "summarized",
        "block_binding": {"prefix_mismatch_behavior": "drop_block"},
    }
    assert "betas" not in body
    # The stream reads back as the API sent it.
    accumulated = _accumulate(got, None, lambda: False, {})
    assert accumulated.content == [
        {"type": "thinking", "thinking": "Hmm.", "signature": "sig-abc"},
        {"type": "text", "text": "Hello."},
        {"type": "tool_use", "id": "t", "name": "view", "input": {"path": "/x"}},
    ]
    assert accumulated.stop_reason == "tool_use"


def test_openrouter_gets_a_bearer_token_and_never_the_anthropic_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    transport = _Transport(200, _sse(reply(text("Hi"), model="anthropic/claude-opus-5.5")))
    provider = _provider("openrouter", OPENROUTER_KEY, transport)

    list(
        _drain(
            provider.open_stream(_params("claude-opus-5-5", "high", True, provider="openrouter"))
        )
    )

    request = transport.requests[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/messages"
    assert request.headers["authorization"] == f"Bearer {OPENROUTER_KEY}"
    assert "x-api-key" not in request.headers
    assert ANTHROPIC_KEY not in str(request.headers) + request.content.decode()
    body = json.loads(request.content)
    assert body["model"] == "anthropic/claude-opus-5.5"
    assert body["provider"]["only"] == ["google-vertex/global"] and body["provider"]["zdr"] is True
    assert "fallbacks" not in body


def test_a_refused_key_is_told_plainly_without_the_key(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    error_body = json.dumps(
        {
            "type": "error",
            "error": {"type": "authentication_error", "message": f"bad {ANTHROPIC_KEY}"},
        }
    ).encode()
    provider = _provider("anthropic", ANTHROPIC_KEY, _Transport(401, error_body))

    with pytest.raises(ProviderFailure) as raised:
        list(_drain(provider.open_stream(_params("claude-opus-5-5", "high", True))))

    assert raised.value.retryable is False and raised.value.status == 401
    assert str(raised.value) == (
        "The AI service refused this server's key, so Claude did not answer. Tell your admin."
    )
    assert raised.value.__cause__ is None
    assert ANTHROPIC_KEY not in caplog.text and ANTHROPIC_KEY not in repr(provider)


def test_failures_are_plain_and_say_whether_to_ask_again() -> None:
    import anthropic

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    timeout = failure_from(anthropic.APITimeoutError(request=request))
    connection = failure_from(anthropic.APIConnectionError(request=request))
    assert timeout.retryable and connection.retryable
    overloaded = failure_from(
        anthropic.APIStatusError(
            "overloaded", response=httpx2.Response(529, request=request), body=None
        )
    )
    assert overloaded.retryable and overloaded.status == 529
    mid_stream = failure_from(
        anthropic.APIStatusError("boom", response=httpx2.Response(200, request=request), body=None)
    )
    assert mid_stream.retryable and "middle of the answer" in str(mid_stream)
    bad = failure_from(
        anthropic.BadRequestError("bad", response=httpx2.Response(400, request=request), body=None)
    )
    assert not bad.retryable and str(bad) == "The AI service refused the request (error 400)."
    # #1577: Fable 5.1 on an account without 30-day retention, told plainly (never their words).
    words = (
        "In order to access this model, your organization or workspace must have data "
        "retention enabled."
    )
    retention = failure_from(
        anthropic.BadRequestError(
            words,
            response=httpx2.Response(400, request=request),
            body={"type": "error", "error": {"type": "invalid_request_error", "message": words}},
        )
    )
    assert not retention.retryable and str(retention) == RETENTION_WORDS
    assert "Pick Claude Opus 5.5" in str(retention) and words not in str(retention)


def test_dropped_thinking_is_read_from_the_stream() -> None:
    """#1577: with the thinking-binding beta the reply says which thinking blocks the API left
    out (`input_transformations`, on `message_start` and again on `message_delta`)."""
    events = reply(text("Hi"))
    events[0]["message"]["input_transformations"] = [
        {
            "type": "thinking_dropped",
            "path": "messages.1.content.0",
            "reason": "prefix_binding_mismatch",
        }
    ]
    assert _accumulate(events, None, lambda: False, {}).transformations[0]["path"] == (
        "messages.1.content.0"
    )
    events[-2]["input_transformations"] = []
    assert _accumulate(events, None, lambda: False, {}).transformations == []


def test_usage_rows_name_the_route_and_the_purpose() -> None:
    lead, helper = prompt_ids("anthropic")
    assert infer_route("anthropic.claude-opus-5-5", lead) == "anthropic"
    assert infer_route("anthropic.claude-opus-5-5", helper) == "anthropic"
    assert infer_route("anthropic.claude-opus-5-5", prompt_ids("openrouter")[0]) == "openrouter"
    assert infer_purpose(lead) == "assistant" and infer_purpose(helper) == "assistant"


def test_the_system_prompt_is_short_and_names_the_skill_and_the_three_facts() -> None:
    # Short like claude.ai's. chat-v13 (the prompt audit) took out the tool catalogue: each tool's
    # own description says what it does and when to use it.
    # Tool search (#1606) added one line about deferred tools.
    assert len(LEAD_SYSTEM_PROMPT) < 2700
    for words in (
        "/mnt/skills/gv-review/SKILL.md",
        "inches",
        "GV's markup",
        "cannot change the review",
        "present_files",
        "markdown",
    ):
        assert words in LEAD_SYSTEM_PROMPT
    assert [t["name"] for t in LEAD_TOOLS] == [
        "bash_tool",
        "view",
        "zoom",
        "create_file",
        "str_replace",
        "present_files",
        "TaskCreate",
        "TaskUpdate",
        "Agent",
        "review_results",
        "report_finding",
        "update_finding",
        "drop_finding",
        "set_coverage",
        "dismiss_recorded",
        "cite",
        "record_string",
        "record_reading",
        "compare_rounds",
        "verify_value",
        "measure_on_drawing",
        "read_under_markup",
        "remember",
        "drawing_facts",
    ]
    # The reviewer's words, once, in the lead's prompt.
    for words in ("Wrong, Your decision or Right", "Not compared", "AI reading only"):
        assert words in LEAD_SYSTEM_PROMPT, words


def test_the_system_prompt_carries_the_answer_style_rules() -> None:
    """CONTRACT2: answers fit the question; comparisons go through report_finding, not a table."""
    for words in (
        "match the question",
        "one line",
        '"Summarize": one or two lines',
        "report_finding for each item",
        "never repeat the findings as a table",
        "No preamble, no disclaimer",
        "what you did not check",
        "ask one short question back",
        "set_coverage",
    ):
        assert words in LEAD_SYSTEM_PROMPT, words
    for words in ("report_finding", "set_coverage"):
        assert words in HELPER_SYSTEM_PROMPT, words
    # Where a box is lives in the tools' schemas now (tool_schema.BOX_UNITS), said one way.
    assert "origin top-left, y down" in json.dumps(LEAD_TOOLS)


class _QuietHandler(BaseHTTPRequestHandler):
    """Streams the start of an answer over a real socket, then says nothing (a long think)."""

    release = threading.Event()

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        self.wfile.write(_sse(reply(text("Thinking it over"))[:3]))
        self.wfile.flush()
        self.release.wait(30)


def test_closing_a_stream_from_another_thread_stops_its_read_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel closes the stream from another thread while the message's thread waits on the
    socket. Closing alone left that read waiting (until the call's 15-minute read timeout), so a
    cancelled message never ended and its chat answered 409 to every later message."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
    try:
        provider = AnthropicSdkProvider(
            ProviderChoice("anthropic", ANTHROPIC_KEY), timeout_seconds=60
        )
        stream = provider.open_stream(_params("claude-haiku-5-5", "high", True))
        got: list[Any] = []

        def read() -> None:
            try:
                for event in stream:
                    got.append(event["type"])
            except ProviderFailure as failure:
                got.append(failure)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        for _ in range(200):
            if len(got) >= 3:
                break
            threading.Event().wait(0.01)
        assert got == ["message_start", "content_block_start", "content_block_delta"]

        stream.close()
        reader.join(timeout=3)

        assert not reader.is_alive()
        assert isinstance(got[-1], ProviderFailure)
    finally:
        _QuietHandler.release.set()
        server.shutdown()
        server.server_close()


def _lead_params(provider: str, tools: Any = LEAD_TOOLS) -> dict[str, Any]:
    chosen = check_choice("claude-opus-5-5", "high", True)
    return call_params(
        provider=provider,  # type: ignore[arg-type]
        model=chosen,
        effort="high",
        thinking=thinking_config(chosen, True),
        system="SYSTEM",
        tools=tools,
        messages=MESSAGES,
        final=False,
        max_tokens=64000,
    )


def test_anthropic_gets_tool_search_first_and_the_cache_mark_on_the_last_loaded_tool() -> None:
    sent = _lead_params("anthropic")["tools"]
    assert sent[0] == {"type": "tool_search_tool_regex_20251119", "name": "tool_search_tool_regex"}
    marked = [tool["name"] for tool in sent if "cache_control" in tool]
    loaded = [tool["name"] for tool in sent[1:] if not tool.get("defer_loading")]
    assert marked == [loaded[-1]]
    # A deferred tool never carries the cache mark (the API refuses it).
    assert not any("cache_control" in tool for tool in sent if tool.get("defer_loading"))
    assert {tool["name"] for tool in sent[1:] if tool.get("defer_loading")} == {
        str(tool["name"]) for tool in LEAD_TOOLS if tool.get("defer_loading")
    }


def test_openrouter_gets_every_tool_loaded_and_no_tool_search() -> None:
    sent = _lead_params("openrouter")["tools"]
    assert [tool["name"] for tool in sent] == [tool["name"] for tool in LEAD_TOOLS]
    assert not any("defer_loading" in tool for tool in sent)
    assert "cache_control" in sent[-1]
    # The chat's stored tool list is not changed by the request built from it.
    assert any(tool.get("defer_loading") for tool in LEAD_TOOLS)


def test_tools_without_deferred_ones_go_as_before() -> None:
    sent = _lead_params(
        "anthropic", [{"name": "a", "input_schema": {}}, {"name": "b", "input_schema": {}}]
    )
    assert [tool["name"] for tool in sent["tools"]] == ["a", "b"]
    assert "cache_control" in sent["tools"][-1]


def test_tool_search_blocks_are_kept_as_received_and_never_run() -> None:
    search_input = {"query": "verify"}
    found = {
        "type": "tool_search_tool_result",
        "tool_use_id": "srv_1",
        "content": {
            "type": "tool_search_tool_search_result",
            "tool_references": [{"type": "tool_reference", "tool_name": "verify_value"}],
        },
    }
    raw = json.dumps(search_input)
    events: list[dict[str, Any]] = [
        {"type": "message_start", "message": {"model": "claude-opus-5-5", "usage": {}}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "server_tool_use",
                "id": "srv_1",
                "name": "tool_search_tool_regex",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": raw[:5]},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": raw[5:]},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": found},
        {"type": "content_block_stop", "index": 1},
        *reply(tool("t1", "verify_value", {"page": 1}), stop="tool_use")[1:],
    ]
    for event in events[7:]:
        if "index" in event:
            event["index"] += 2
    accumulated = _accumulate(events, None, lambda: False, {})
    kinds = [block["type"] for block in accumulated.history_content()]
    assert kinds == ["server_tool_use", "tool_search_tool_result", "tool_use"]
    # The search's input is replayed exactly as it streamed; only our own tool is run.
    assert accumulated.content[0]["input"] == search_input
    assert accumulated.content[1] == found
    assert [call["name"] for call in accumulated.tool_uses()] == ["verify_value"]
