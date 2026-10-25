"""Models from outside Claude for comparison tests (2026-10-25): no network, no paid call.

- Each of GPT-6 Astra, GPT-6.1 Sol, GPT-6 Luna and Gemini 4 Argon leaves the process as the exact
  JSON OpenRouter needs: its own name, only its keep-no-data hosts, no data kept, no fallbacks,
  every parameter honoured, and no Claude-only field.
- Every Claude request is byte for byte what it was before these models were added.
- With the server setting off (the default) the models are refused; on, only through OpenRouter.
- Under such a lead every call of the answer runs on the lead's model, is priced at its rates, and
  a reply is used only when it names that model (exactly, or a dated snapshot of it).
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant.chat_engine import _without_unsigned_thinking
from app.review.assistant.chat_provider import failure_from
from app.review.assistant.chat_request import (
    BY_SHORT,
    EXTRA_MODELS,
    MODELS,
    ChoiceRefused,
    call_params,
    check_choice,
    helper_shorts,
    helper_thinking,
    thinking_config,
)
from app.runs.rates import load_model_rates
from tests.review.assistant import test_chat_engine as engine_tests
from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text, thinking, tool
from tests.review.assistant.test_chat_request import (
    OPENROUTER_KEY,
    _drain,
    _provider,
    _sse,
    _Transport,
)

RATES = load_model_rates(
    Path(__file__).resolve().parents[3] / "deploy" / "model_rates.us-east-1.json"
)
TOOLS: list[dict[str, Any]] = [
    {"name": "a", "description": "A.", "input_schema": {"type": "object"}},
    {"name": "b", "description": "B.", "input_schema": {"type": "object"}, "defer_loading": True},
]
MESSAGES: list[dict[str, Any]] = [
    {
        "role": "user",
        "content": [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
            },
            {"type": "text", "text": "Hi"},
        ],
    },
    {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "t", "signature": "s"},
            {"type": "tool_use", "id": "t1", "name": "a", "input": {}},
        ],
    },
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
]
#: OpenRouter's name, its keep-no-data hosts and its price key, per model (checked 2026-10-25 on
#: `GET /api/v1/endpoints/zdr` and `GET /api/v1/models`).
EXPECTED = {
    "gpt-6-astra": ("openai/gpt-6-astra", ["azure"], ("0.01", "0.05")),
    "gpt-6.1-sol": ("openai/gpt-6.1-sol", ["azure"], ("0.002", "0.01")),
    "gpt-6-luna": ("openai/gpt-6-luna", ["azure"], ("0.0001", "0.0005")),
    "gemini-4-argon": ("google/gemini-4-argon", ["google-vertex/global"], ("0.002", "0.01")),
}


def _wire(params: dict[str, Any]) -> dict[str, Any]:
    """The request body as the official SDK sends it to OpenRouter (a fake transport)."""
    transport = _Transport(200, _sse(reply(text("ok"), model=params["model"])))
    provider = _provider("openrouter", OPENROUTER_KEY, transport)
    list(_drain(provider.open_stream(params)))
    request = transport.requests[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/messages"
    body: dict[str, Any] = json.loads(request.content)
    return body


# ---- the request ---------------------------------------------------------------------------------


@pytest.mark.parametrize("model_id", sorted(EXPECTED))
def test_each_extra_model_leaves_as_the_exact_keep_no_data_request(model_id: str) -> None:
    model = check_choice(model_id, "high", True, "openrouter", extra=True)
    params = call_params(
        provider="openrouter",
        model=model,
        effort="high",
        # A Claude helper's "thinking off" is never sent to these models: they always reason.
        thinking={"type": "disabled"},
        system="SYSTEM",
        tools=TOOLS,
        messages=MESSAGES,
        final=False,
        max_tokens=32000,
    )

    body = _wire(params)

    name, hosts, _prices = EXPECTED[model_id]
    assert body == {
        "model": name,
        "max_tokens": 32000,
        "system": [{"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}],
        "messages": [
            *MESSAGES[:-1],
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "ok",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
        ],
        "tools": [
            TOOLS[0],
            {
                "name": "b",
                "description": "B.",
                "input_schema": {"type": "object"},
                "cache_control": {"type": "ephemeral"},
            },
        ],
        "tool_choice": {"type": "auto"},
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
        "provider": {
            "only": hosts,
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
            "zdr": True,
        },
        "stream": True,
    }


def test_argon_gets_the_highest_effort_it_takes_and_a_final_answer_uses_no_tool() -> None:
    model = EXTRA_MODELS["gemini-4-argon"]
    for asked, sent in (("low", "low"), ("medium", "medium"), ("xhigh", "high"), ("max", "high")):
        params = call_params(
            provider="openrouter",
            model=model,
            effort=asked,
            thinking=thinking_config(model, True),
            system="S",
            tools=TOOLS,
            messages=MESSAGES,
            final=True,
            max_tokens=1000,
        )
        assert params["output_config"] == {"effort": sent}
        assert params["tool_choice"] == {"type": "none"}
    gpt = EXTRA_MODELS["gpt-6-astra"]
    for effort in ("low", "medium", "high", "xhigh", "max"):
        params = call_params(
            provider="openrouter",
            model=gpt,
            effort=effort,
            thinking=helper_thinking(gpt, effort, False),
            system="S",
            tools=(),
            messages=MESSAGES,
            final=False,
            max_tokens=1000,
            output_format={"type": "object"},
        )
        assert params["output_config"] == {"effort": effort}
        assert params["thinking"] == {"type": "adaptive"}
        assert "tools" not in params and "betas" not in params and "fallbacks" not in params
        assert params["system"][0]["text"].endswith(
            'JSON schema, and nothing else: {"type":"object"}'
        )


#: Every Claude request shape below, as made by origin/main e0c48201 before these models existed.
_CLAUDE_SHAPES = 1504
_CLAUDE_SHA256 = "8cdaf912d6ac8b8206740c023e08a412c2337dd996de48511ca0b9cf6366f681"


def test_every_claude_request_is_byte_for_byte_unchanged() -> None:
    cases = []
    for provider in ("anthropic", "openrouter"):
        for model in MODELS.values():
            if provider not in model.providers:
                continue
            for effort in ("low", "medium", "high", "xhigh", "max"):
                for on in (True, False):
                    try:
                        check_choice(model.id, effort, on, provider)
                    except ChoiceRefused:
                        continue
                    for sent in (thinking_config(model, on), helper_thinking(model, effort, on)):
                        for final in (False, True):
                            for tools in (TOOLS, ()):
                                for fmt in (
                                    None,
                                    {"type": "object", "properties": {"x": {"type": "string"}}},
                                ):
                                    for ctx in (
                                        None,
                                        {"edits": [{"type": "clear_tool_uses_20250919"}]},
                                    ):
                                        cases.append(
                                            call_params(
                                                provider=provider,
                                                model=model,
                                                effort=effort,
                                                thinking=sent,
                                                system="SYSTEM",
                                                tools=tools,
                                                messages=MESSAGES,
                                                final=final,
                                                max_tokens=64000,
                                                context_management=ctx,
                                                output_format=fmt,
                                            )
                                        )
    blob = json.dumps(cases, sort_keys=True).encode()
    assert len(cases) == _CLAUDE_SHAPES
    assert hashlib.sha256(blob).hexdigest() == _CLAUDE_SHA256


# ---- the choice ----------------------------------------------------------------------------------


@pytest.mark.parametrize("model_id", sorted(EXPECTED))
def test_with_the_setting_off_the_extra_models_are_refused(model_id: str) -> None:
    with pytest.raises(ChoiceRefused, match="not one of the models offered here"):
        check_choice(model_id, "high", True, "openrouter")
    with pytest.raises(ChoiceRefused, match="only through OpenRouter"):
        check_choice(model_id, "high", True, "anthropic", extra=True)
    with pytest.raises(ChoiceRefused, match="always on"):
        check_choice(model_id, "high", False, "openrouter", extra=True)
    assert check_choice(model_id, "high", True, "openrouter", extra=True).extra


def test_the_extra_models_stay_out_of_the_menu_and_the_helper_list() -> None:
    assert not set(EXPECTED) & set(MODELS)
    assert set(helper_shorts()) == {"opus", "sonnet", "haiku"}
    assert not any(model.extra for model in MODELS.values())
    assert (
        check_choice("claude-opus-5-5", "high", True, "openrouter", extra=True)
        is MODELS["claude-opus-5-5"]
    )


@pytest.mark.parametrize("model_id", sorted(EXPECTED))
def test_each_extra_model_has_its_openrouter_price(model_id: str) -> None:
    rate = RATES.rate_for(EXTRA_MODELS[model_id].rates_key)
    assert rate is not None
    assert (rate.input_per_1k_tokens, rate.output_per_1k_tokens) == tuple(
        Decimal(p) for p in EXPECTED[model_id][2]
    )


# ---- the answer under such a lead ----------------------------------------------------------------

LUNA = "openai/gpt-6-luna"


def _harness(tmp_path: Path, provider: ScriptedProvider, monkeypatch: pytest.MonkeyPatch, **kw: Any):  # type: ignore[no-untyped-def]
    # The test harness looks its lead up in `MODELS` (the same mapping): put there for this test.
    monkeypatch.setitem(MODELS, "gpt-6-luna", EXTRA_MODELS["gpt-6-luna"])
    provider.name = "openrouter"
    return engine_tests.Harness(tmp_path, provider, model="gpt-6-luna", **kw)


def test_every_helper_runs_on_the_leads_model_and_unsigned_thinking_stays_out_of_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    redacted = {"type": "redacted_thinking", "data": "ENCRYPTED"}
    provider = ScriptedProvider(
        lead=[
            reply(
                thinking("Summary of my reasoning.", signature=""),
                redacted,
                tool(
                    "ag1", "Agent", {"description": "Pages", "prompt": "Check p1", "model": "opus"}
                ),
                stop="tool_use",
                model=LUNA,
            ),
            reply(text("One issue."), model=f"{LUNA}-20260922"),
        ],
        helpers={"Check p1": [reply(text("Page 1: sink differs."), model=LUNA)]},
    )
    harness = _harness(tmp_path, provider, monkeypatch)

    outcome = harness.run()

    assert outcome.error is None and outcome.text == "One issue."
    sent = [json.loads(call) for call in provider.calls]
    assert {call["model"] for call in sent} == {LUNA}
    assert all(call["thinking"] == {"type": "adaptive"} for call in sent)
    assert all(call["extra_body"]["provider"]["only"] == ["azure"] for call in sent)
    # The reasoning goes back as received; its unsigned summary (for the screen) does not.
    replayed = json.loads(provider.lead_calls[1])["messages"][1]
    assert replayed == {
        "role": "assistant",
        "content": [
            redacted,
            {
                "type": "tool_use",
                "id": "ag1",
                "name": "Agent",
                "input": {"description": "Pages", "prompt": "Check p1", "model": "opus"},
            },
        ],
    }
    # Priced at GPT-6 Luna's rates (1000 in + 500 out per call: $0.0001 + $0.00025).
    assert harness.engine.spent == 3 * 350
    assert {row["model_key"] for row in harness.ledger.rows} == {"openai.gpt-6-luna"}


@pytest.mark.parametrize("named", ["openai/gpt-6-luna-pro", "openai/gpt-6.1-sol", "gpt-6-lunar"])
def test_a_reply_naming_another_model_is_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, named: str
) -> None:
    provider = ScriptedProvider(lead=[reply(text("Hi"), model=named)])
    harness = _harness(tmp_path, provider, monkeypatch)

    outcome = harness.run()

    assert outcome.stopped == "error" and "different model" in str(outcome.error)


def test_calls_asked_of_claude_models_are_priced_as_the_lead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path, ScriptedProvider(), monkeypatch)
    engine = harness.engine

    assert engine.run_model(BY_SHORT["opus"]) is EXTRA_MODELS["gpt-6-luna"]
    # 1,000,000 in + 100,000 out at $0.10 / $0.50 per million: $0.15.
    assert engine._price(BY_SHORT["opus"], 1_000_000, 100_000) == 150_000
    # Over 272,000 prompt tokens: twice the input price, 1.5 times the output's.
    assert engine._weighted(
        "openai.gpt-6-luna", {"input_tokens": 300_000, "output_tokens": 10}
    ) == (
        600_000,
        15,
    )
    assert engine._weighted(
        "openai.gpt-6-luna", {"input_tokens": 200_000, "output_tokens": 10}
    ) == (
        200_000,
        10,
    )


def test_a_claude_lead_still_runs_its_helpers_on_claude(tmp_path: Path) -> None:
    harness = engine_tests.Harness(tmp_path, ScriptedProvider())
    assert harness.engine.run_model(BY_SHORT["sonnet"]) is BY_SHORT["sonnet"]
    assert harness.engine._helper_model() is BY_SHORT["sonnet"]


def test_only_unsigned_thinking_is_left_out() -> None:
    content = [
        {"type": "thinking", "thinking": "a", "signature": ""},
        {"type": "thinking", "thinking": "b", "signature": "sig"},
        {"type": "redacted_thinking", "data": "x"},
        {"type": "text", "text": "c"},
    ]
    assert [b.get("thinking") or b["type"] for b in _without_unsigned_thinking(content)] == [
        "b",
        "redacted_thinking",
        "text",
    ]


def test_an_openrouter_policy_refusal_is_not_called_a_bad_key() -> None:
    import anthropic
    import httpx2

    request = httpx2.Request("POST", "https://openrouter.ai/api/v1/messages")
    refused = failure_from(
        anthropic.PermissionDeniedError(
            "declined",
            response=httpx2.Response(403, request=request),
            body={"type": "error", "error": {"type": "permission_error", "error_type": "refusal"}},
        )
    )
    assert refused.kind is None and not refused.retryable
    assert str(refused) == "The AI service declined this request, so the AI did not answer it."
