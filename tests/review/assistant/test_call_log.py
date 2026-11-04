"""The per-call usage and cache log (`call_log.py`, rebuild plan Step 1B), $0: scripted providers
only, no network, invented values.

- **One line per try** of every call through `ChatEngine._call`: the tokens with and without the
  5-minute / 1-hour cache-write split, the host that answered when the stream names one, a call
  that failed before any usage, one that failed in the middle, and a refusal fallback's two models.
- **The prefix hash** is the same whatever the newest block, and changes with the tools, the system
  prompt or a cached picture.
- **The phase follows the work into pools** (`work_pool.ContextPool`, `run_ordered`): a call made
  on a pool's thread is logged under the phase and tags of the thread that submitted it.
- **The file** is appended to, and a write that fails never stops the answer.
- **The request is unchanged**: `call_params` places its cache breakpoints as before, and an
  answer sends the same bytes with the log on as with it off.
- **The simple check's compare calls**, which stream on their own, are logged per try too.
"""

from __future__ import annotations

import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.review.assistant import call_log as calls
from app.review.assistant.call_log import CallFacts, CallLog, Part
from app.review.assistant.chat_request import (
    LEGACY_CACHE,
    MODELS,
    CachePlan,
    call_params,
    thinking_config,
)
from app.review.assistant.chat_timings import Timings
from app.review.assistant.simple_check import run as simple_run
from app.review.assistant.simple_check.compare import CompareSettings, Tried, compare_item
from app.review.assistant.work_pool import ContextPool, in_context, run_ordered
from tests.review.assistant.chat_fakes import ScriptedProvider, reply, server_error, text
from tests.review.assistant.test_chat_engine import Harness
from tests.review.assistant.test_simple_compare import SYSTEM, _events, _item, _pictures
from tests.review.assistant.test_simple_compare import _Provider as CompareProvider

SONNET = MODELS["claude-sonnet-5-5"]
WHEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _logged(harness: Harness, tmp_path: Path) -> Path:
    log = CallLog.for_message(tmp_path / "chat-folder", "chat-1", "message-1")
    harness.engine.call_log = log
    return log.path


def _with(events: list[dict[str, Any]], **message: Any) -> list[dict[str, Any]]:
    """`events` with the first message's fields (and its usage's) set as given."""
    start = events[0]["message"]
    usage = message.pop("usage", None)
    start.update(message)
    if usage is not None:
        start["usage"].update(usage)
    return events


# ---- building the lines --------------------------------------------------------------------------


def test_a_line_carries_the_cache_split_when_the_usage_has_one() -> None:
    usage = {
        "input_tokens": 120,
        "cache_creation_input_tokens": 3000,
        "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 2000},
        "cache_read_input_tokens": 50_000,
        "output_tokens": 700,
    }
    call = CallFacts(
        prompt_id="review-assistant-chat-check-anthropic-x", purpose="check", attempt=1
    )
    (line,) = calls.records(
        call,
        [Part("anthropic.claude-sonnet-5-5", usage, 123_456, 7_890)],
        chat_id="c",
        message_id="m",
        phase="second_reads",
        helper=None,
        tags={},
        ts=WHEN,
    )
    assert line["ephemeral_5m_input_tokens"] == 1000
    assert line["ephemeral_1h_input_tokens"] == 2000
    assert line["cache_creation_input_tokens"] == 3000
    assert line["cache_read_input_tokens"] == 50_000
    assert (line["input_tokens"], line["output_tokens"]) == (120, 700)
    assert line["cost_usd"] == "0.123456" and line["cache_write_cost_usd"] == "0.00789"
    assert line["ts"] == "2026-01-02T03:04:05+00:00"
    assert line["kind"] == "check" and line["phase"] == "second_reads"


def test_without_a_cache_creation_object_the_split_is_null_and_missing_counts_are_zero() -> None:
    call = CallFacts(prompt_id="p", purpose="p", attempt=2, error=server_error())
    (line,) = calls.records(
        call,
        [Part("k", {"input_tokens": 5}, None, None)],
        chat_id=None,
        message_id=None,
        phase="other",
        helper=None,
        tags={"kind": "numbers", "agent": "plan-1", "page": "3,4"},
        ts=WHEN,
    )
    assert line["ephemeral_5m_input_tokens"] is None
    assert line["ephemeral_1h_input_tokens"] is None
    assert line["cache_read_input_tokens"] == 0 and line["output_tokens"] == 0
    assert line["cost_usd"] is None
    assert (line["kind"], line["agent"], line["page"]) == ("numbers", "plan-1", "3,4")
    assert line["error"] == "ProviderFailure" and line["error_status"] == 529
    assert line["attempt"] == 2


def test_the_purpose_comes_from_the_prompt_id() -> None:
    assert calls.purpose_of("review-assistant-chat-anthropic-chat-v20", "anthropic") == "lead"
    assert calls.purpose_of("review-assistant-chat-helper-openrouter-chat-v20", "openrouter") == (
        "helper"
    )
    assert calls.purpose_of("review-assistant-chat-page-match-anthropic-chat-v20", "anthropic") == (
        "page-match"
    )
    assert calls.purpose_of("something-else", "anthropic") == "something-else"


def test_the_provider_asked_for_is_anthropic_or_the_openrouter_hosts() -> None:
    def params(provider: str) -> dict[str, Any]:
        return call_params(
            provider=provider,  # type: ignore[arg-type]
            model=SONNET,
            effort="low",
            thinking=thinking_config(SONNET, False),
            system="Neutral system.",
            tools=(),
            messages=[{"role": "user", "content": [{"type": "text", "text": "Hello."}]}],
            final=False,
            max_tokens=100,
        )

    assert calls.requested_provider("anthropic", params("anthropic")) == "anthropic"
    assert calls.requested_provider("openrouter", params("openrouter")) == "google-vertex/global"
    assert calls.requested_provider("openrouter", {}) == "openrouter"


def test_tags_nest_and_unknown_tags_are_refused() -> None:
    with calls.tagged(kind="gap", page="5"):
        with calls.tagged(page="6", agent=None):
            assert dict(calls.current_tags()) == {"kind": "gap", "page": "6"}
        assert dict(calls.current_tags()) == {"kind": "gap", "page": "5"}
    assert dict(calls.current_tags()) == {}
    with pytest.raises(ValueError), calls.tagged(colour="red"):
        pass


# ---- the engine's calls --------------------------------------------------------------------------


def test_every_engine_call_is_one_line_with_its_cache_split_and_answering_host(
    tmp_path: Path,
) -> None:
    answered = _with(
        reply(text("All good."), usage={"input_tokens": 300, "output_tokens": 40}),
        provider="Google",
        usage={
            "cache_creation_input_tokens": 1200,
            "cache_read_input_tokens": 9000,
            "cache_creation": {"ephemeral_5m_input_tokens": 1200, "ephemeral_1h_input_tokens": 0},
        },
    )
    harness = Harness(tmp_path, ScriptedProvider(lead=[answered]))
    path = _logged(harness, tmp_path)

    harness.run()

    (line,) = _lines(path)
    assert (line["chat_id"], line["message_id"]) == ("chat-1", "message-1")
    assert line["purpose"] == "lead" and line["kind"] == "lead"
    assert line["model"] == "anthropic.claude-opus-5-5"
    assert line["model_answered"] == "claude-opus-5-5"
    assert line["provider_requested"] == "anthropic"
    assert line["provider_answered"] == "Google"
    assert line["input_tokens"] == 300 and line["output_tokens"] == 40
    assert line["cache_creation_input_tokens"] == 1200
    assert line["ephemeral_5m_input_tokens"] == 1200 and line["ephemeral_1h_input_tokens"] == 0
    assert line["cache_read_input_tokens"] == 9000
    assert line["stop_reason"] == "end_turn" and line["error"] is None
    assert line["attempt"] == 1 and (line["part"], line["parts"]) == (1, 1)
    assert isinstance(line["latency_ms"], int) and isinstance(line["queued_ms"], int)
    assert len(line["prefix_hash"]) == 64
    # The same price the answer was charged: its only call.
    assert Decimal(line["cost_usd"]) * 1_000_000 == harness.engine.spent
    assert Decimal(line["cache_write_cost_usd"]) > 0


def test_no_host_named_in_the_stream_is_logged_as_null(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ScriptedProvider(lead=[reply(text("Fine."))]))
    path = _logged(harness, tmp_path)

    harness.run()

    (line,) = _lines(path)
    assert line["provider_answered"] is None
    assert line["ephemeral_5m_input_tokens"] is None


def test_a_call_that_failed_before_any_usage_is_a_line_with_its_error_and_no_tokens(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, ScriptedProvider(lead=[server_error(), reply(text("Fine now."))]))
    path = _logged(harness, tmp_path)

    harness.run()

    failed, answered = _lines(path)
    assert failed["error"] == "ProviderFailure"
    assert failed["error_status"] == 529 and "error_kind" in failed
    assert failed["input_tokens"] == 0 and failed["output_tokens"] == 0
    assert failed["cost_usd"] == "0" and failed["stop_reason"] is None
    assert failed["attempt"] == 1 and answered["attempt"] == 2
    assert answered["error"] is None
    # Never the failure's words: only its class, kind and status.
    assert "had a problem" not in json.dumps(failed)


def test_a_call_that_broke_mid_stream_keeps_its_usage_and_host(tmp_path: Path) -> None:
    broken = _with(reply(text("Garbled"), usage={"input_tokens": 50_000}), provider="Google")
    broken.insert(3, {"type": "x", "__raise__": server_error()})
    harness = Harness(tmp_path, ScriptedProvider(lead=[broken, reply(text("Fine now."))]))
    path = _logged(harness, tmp_path)

    harness.run()

    failed, _answered = _lines(path)
    assert failed["input_tokens"] == 50_000 and failed["error"] == "ProviderFailure"
    assert failed["provider_answered"] == "Google"


def test_a_refusal_fallback_is_one_line_per_model(tmp_path: Path) -> None:
    events = reply(text("Answered by the fallback."), usage={"input_tokens": 100})
    events[-2]["usage"] = {
        "output_tokens": 30,
        "iterations": [
            {
                "type": "message",
                "model": "claude-opus-5-5",
                "input_tokens": 100,
                "output_tokens": 0,
            },
            {
                "type": "fallback_message",
                "model": "claude-sonnet-5-5",
                "input_tokens": 100,
                "output_tokens": 30,
            },
        ],
    }
    harness = Harness(tmp_path, ScriptedProvider(lead=[events]))
    path = _logged(harness, tmp_path)

    harness.run()

    first, second = _lines(path)
    assert (first["part"], second["part"], second["parts"]) == (1, 2, 2)
    assert first["model_answered"] == "claude-opus-5-5"
    assert second["model_answered"] == "claude-sonnet-5-5"
    assert first["stop_reason"] is None and second["stop_reason"] == "end_turn"
    assert first["prefix_hash"] == second["prefix_hash"]


def test_without_a_call_log_nothing_is_written(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ScriptedProvider(lead=[reply(text("Fine."))]))
    assert harness.engine.call_log is None

    harness.run()

    assert not list(tmp_path.rglob("*.jsonl"))


# ---- the prefix hash -----------------------------------------------------------------------------


def _png(colour: int) -> str:
    return base64.b64encode(bytes([137, 80, 78, 71, colour])).decode("ascii")


def _image(colour: int) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": _png(colour)},
    }


def _params(
    newest: str = "Which is wider?",
    *,
    system: str = "Neutral system.",
    cached_picture: int = 1,
    newest_picture: int = 9,
    tool: str = "zoom",
    ttl: str | None = None,
) -> dict[str, Any]:
    """A call shaped like the page matcher's: pictures, a breakpoint after them, then the newest."""
    marker: dict[str, Any] = {"type": "ephemeral", **({"ttl": ttl} if ttl else {})}
    content = [
        {"type": "text", "text": "Reference pictures:"},
        {**_image(cached_picture), "cache_control": marker},
        _image(newest_picture),
        {"type": "text", "text": newest},
    ]
    return call_params(
        provider="anthropic",
        model=SONNET,
        effort="low",
        thinking=thinking_config(SONNET, False),
        system=system,
        tools=[{"name": tool, "description": "A tool.", "input_schema": {"type": "object"}}],
        messages=[{"role": "user", "content": content}],
        final=False,
        max_tokens=100,
        # A 1-hour breakpoint must come before every 5-minute one (Anthropic's rule, which
        # `call_params` holds since the cache layouts): the tools' and the system's too.
        cache=CachePlan(tools="1h", system="1h") if ttl == "1h" else LEGACY_CACHE,
    )


def test_the_same_prefix_hashes_the_same_whatever_the_newest_block() -> None:
    first = calls.prefix_hash(_params("Which is wider?", newest_picture=9))
    assert calls.prefix_hash(_params("Which is taller?", newest_picture=8)) == first
    # A breakpoint's lifetime is not content.
    assert calls.prefix_hash(_params(ttl="1h")) == first


def test_a_change_to_the_tools_system_or_a_cached_picture_changes_the_hash() -> None:
    first = calls.prefix_hash(_params())
    assert calls.prefix_hash(_params(system="Another system.")) != first
    assert calls.prefix_hash(_params(tool="look")) != first
    assert calls.prefix_hash(_params(cached_picture=2)) != first


def test_with_no_breakpoint_but_the_newest_no_message_is_in_the_prefix() -> None:
    def plain(question: str) -> dict[str, Any]:
        return call_params(
            provider="anthropic",
            model=SONNET,
            effort="low",
            thinking=thinking_config(SONNET, False),
            system="Neutral system.",
            tools=(),
            messages=[
                {"role": "user", "content": [{"type": "text", "text": "Earlier turn."}]},
                {"role": "assistant", "content": [{"type": "text", "text": "Earlier answer."}]},
                {"role": "user", "content": [{"type": "text", "text": question}]},
            ],
            final=False,
            max_tokens=100,
        )

    assert calls.prefix_hash(plain("One?")) == calls.prefix_hash(plain("Two?"))
    no_messages = {"tools": [], "system": plain("One?")["system"], "messages": []}
    assert calls.prefix_hash(plain("One?")) == calls.prefix_hash(no_messages)


def test_a_picture_is_hashed_by_its_bytes_never_kept() -> None:
    canonical = calls._canonical(_image(1))
    data = canonical["source"]["data"]
    assert data.startswith("sha256:") and _png(1) not in json.dumps(canonical)


# ---- the phase follows the work into pools -------------------------------------------------------


def test_a_context_pool_carries_the_phase_and_tags_a_plain_pool_loses_them() -> None:
    timings = Timings()
    with timings.phase("advocate"), calls.tagged(page="p3"):
        with ContextPool(max_workers=2) as pool:
            kept = list(pool.map(lambda _n: (timings.current(), dict(calls.current_tags())), [1]))
        with ThreadPoolExecutor(max_workers=1) as plain:
            lost = plain.submit(timings.current).result()
        done = run_ordered([1, 2], lambda _n: timings.current(), workers=2)
        holder: list[Any] = []
        thread = threading.Thread(target=in_context(lambda: holder.append(timings.current())))
        thread.start()
        thread.join()
    assert kept == [(("advocate", None), {"page": "p3"})]
    assert lost is None
    assert [d.value for d in done] == [("advocate", None), ("advocate", None)]
    assert holder == [("advocate", None)]
    assert timings.current() is None


def test_a_phase_opened_in_a_pool_task_never_reaches_the_submitter() -> None:
    timings = Timings()
    seen: list[Any] = []

    def task(_n: int) -> None:
        with timings.phase("inner"):
            seen.append(timings.current())

    with timings.phase("outer"):
        with ContextPool(max_workers=1) as pool:
            list(pool.map(task, [1]))
        assert timings.current() == ("outer", None)
    assert seen == [("inner", None)]


def test_a_call_made_in_a_pool_worker_is_logged_under_the_submitters_phase(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(helper_answer=lambda _params: reply(text("ok"), model=SONNET.id))
    harness = Harness(tmp_path, provider)
    path = _logged(harness, tmp_path)
    engine = harness.engine

    def ask(_n: int) -> str:
        return engine._call(
            model=SONNET,
            effort="low",
            thinking=thinking_config(SONNET, False),
            system="Neutral test system.",
            tools=(),
            messages=[{"role": "user", "content": [{"type": "text", "text": "Read the label."}]}],
            final=False,
            max_tokens=100,
            prompt_id="review-assistant-chat-advocate-anthropic-chat-v20",
            display=None,
        ).text()

    with (
        engine.timings.phase("advocate"),
        calls.tagged(page="p12"),
        ContextPool(max_workers=2) as pool,
    ):
        assert list(pool.map(ask, [1, 2])) == ["ok", "ok"]

    lines = _lines(path)
    assert len(lines) == 2
    assert {line["phase"] for line in lines} == {"advocate"}
    assert {(line["kind"], line["page"]) for line in lines} == {("advocate", "p12")}
    # The phase's own cost line agrees: nothing went to "other".
    phases = {p["phase"]: p for p in engine.timings.as_dict(engine.spent)["phases"]}
    assert phases["advocate"]["calls"] == 2 and "other" not in phases


# ---- the file ------------------------------------------------------------------------------------


def test_lines_are_appended_never_rewritten(tmp_path: Path) -> None:
    log = CallLog.for_message(tmp_path, "c", "m")
    assert log.path == tmp_path / "calls" / "m.jsonl"
    assert log.append([{"n": 1}]) and log.append([{"n": 2}, {"n": 3}])
    assert [line["n"] for line in _lines(log.path)] == [1, 2, 3]


def test_a_log_that_cannot_be_written_never_stops_the_answer(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the calls folder should be", encoding="utf-8")
    harness = Harness(tmp_path, ScriptedProvider(lead=[reply(text("Still answered."))]))
    harness.engine.call_log = CallLog(blocked / "m.jsonl", chat_id="c", message_id="m")

    outcome = harness.run()

    assert outcome.text == "Still answered." and outcome.stopped is None
    assert harness.ledger.rows  # the Usage row is still recorded
    assert "call log could not be written" in caplog.text


# ---- the request is unchanged --------------------------------------------------------------------


def test_call_params_places_its_cache_breakpoints_as_before() -> None:
    zoom = {"name": "zoom", "description": "Zoom in.", "input_schema": {"type": "object"}}
    marker = {"type": "ephemeral"}
    for provider in ("anthropic", "openrouter"):
        params = call_params(
            provider=provider,  # type: ignore[arg-type]
            model=SONNET,
            effort="low",
            thinking=thinking_config(SONNET, False),
            system="Neutral system.",
            tools=[zoom],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "First."},
                        {"type": "text", "text": "Second."},
                    ],
                }
            ],
            final=False,
            max_tokens=100,
        )
        assert params["tools"] == [{**zoom, "cache_control": marker}]
        assert params["system"] == [
            {"type": "text", "text": "Neutral system.", "cache_control": marker}
        ]
        assert params["messages"] == [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "First."},
                    {"type": "text", "text": "Second.", "cache_control": marker},
                ],
            }
        ]
        assert params["tool_choice"] == {"type": "auto"}
        assert params["output_config"] == {"effort": "low"}
        assert params["max_tokens"] == 100
        if provider == "anthropic":
            assert params["model"] == "claude-sonnet-5-5"
            assert "extra_body" not in params
        else:
            assert params["model"] == "anthropic/claude-sonnet-5.5"
            assert params["extra_body"]["provider"]["only"] == ["google-vertex/global"]
            assert params["extra_body"]["provider"]["allow_fallbacks"] is False


def test_an_answer_sends_the_same_requests_with_the_log_on_or_off(tmp_path: Path) -> None:
    def sent(log: bool) -> list[str]:
        provider = ScriptedProvider(lead=[server_error(), reply(text("Same answer."))])
        harness = Harness(tmp_path / ("on" if log else "off"), provider)
        if log:
            _logged(harness, tmp_path / "on")
        harness.run()
        return provider.calls

    assert sent(log=True) == sent(log=False)


# ---- the simple check's compare calls ------------------------------------------------------------


def test_every_compare_try_is_told_with_its_host_split_and_failure() -> None:
    answer = json.dumps(
        {
            "work_item": "p7.v1",
            "pairs": [],
            "other_items": [],
            "unaccounted_labels": ["p7.D3", "p7.D4", "p7.D5"],
        }
    )
    events = _events(answer)
    events[0]["message"]["provider"] = "Google"
    events[0]["message"]["usage"]["cache_creation"] = {
        "ephemeral_5m_input_tokens": 0,
        "ephemeral_1h_input_tokens": 0,
    }
    provider = CompareProvider([server_error(), events])
    tried: list[Tried] = []

    compare_item(
        _item(),
        provider=provider,
        source=_pictures,
        system=SYSTEM,
        settings=CompareSettings(retries=2),
        sleep=lambda _s: None,
        on_attempt=tried.append,
    )

    failed, answered = tried[:2]
    assert failed.number == 1 and failed.error is not None and dict(failed.usage) == {}
    assert answered.number == 2 and answered.error is None
    assert answered.provider == "Google" and answered.stop_reason == "end_turn"
    assert answered.usage["input_tokens"] == 9000
    assert answered.usage["cache_creation"] == {
        "ephemeral_5m_input_tokens": 0,
        "ephemeral_1h_input_tokens": 0,
    }
    assert answered.params is provider.calls[1]


def test_a_compare_try_goes_in_the_chat_call_log(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ScriptedProvider())
    path = _logged(harness, tmp_path)
    tried = Tried(
        params=_params(),
        usage={"input_tokens": 9000, "cache_read_input_tokens": 2000, "output_tokens": 3000},
        asked_model="claude-sonnet-5-5",
        model="claude-sonnet-5-5",
        provider=None,
        stop_reason="end_turn",
        error=None,
        number=1,
        latency_ms=1500,
    )

    with harness.engine.timings.phase("compare"), calls.tagged(page="p7.v1"):
        fake_run: Any = SimpleNamespace(engine=harness.engine)
        simple_run._Run._log_try(fake_run, tried)

    (line,) = _lines(path)
    assert line["purpose"] == "simple-compare" and line["phase"] == "compare"
    assert line["page"] == "p7.v1" and line["latency_ms"] == 1500
    assert line["prefix_hash"] == calls.prefix_hash(_params())
    assert line["cost_usd"] is not None and line["queued_ms"] is None
