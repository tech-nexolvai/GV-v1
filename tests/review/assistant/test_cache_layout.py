"""Where a call's prompt-cache breakpoints go, and the `shared` layout (rebuild plan Step 5).

No network, no paid call: every test builds the request the code would send and reads it.

- The `legacy` layout (the default) sends byte for byte today's request: pinned against a frozen
  copy of the old request builder (`legacy_call_params.py`) over a grid of calls.
- A caller says where the breakpoints go (`CachePlan`, `cache_mark`), each 5-minute or 1-hour;
  more than 4, or a 1-hour one after a 5-minute one, is refused before anything is sent.
- A wrap-up may keep `tool_choice` as it was (`final_choice="keep"`).
- Under `shared`: four kinds of specialist on one page pair send one tool list, one system
  prompt, one effort and ONE cached prefix (`call_log.prefix_hash`), warmed once; a kind is
  refused another kind's tool by code; a wrap-up keeps its tools, runs none of them, and a
  conversation's tools and system prompt never change; a one-off call has no breakpoint on its
  newest block; the warm-up signals at its reply's start, on OpenRouter too.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import threading
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import call_log, chat_request
from app.review.assistant.cache_layout import (
    SPECIALIST_KNOWLEDGE,
    SPECIALIST_TOOLS,
    job_text,
    kind_job,
    refused,
    specialist_effort,
    specialist_system,
)
from app.review.assistant.chat_engine import (
    WRAP_UP_NOT_RUN,
    WRAP_UP_TOOLS_NOTE,
    _Agent,
    _Share,
)
from app.review.assistant.chat_request import (
    BY_SHORT,
    LEGACY_CACHE,
    MODELS,
    SINGLE_SHOT,
    CachePlan,
    CacheRefused,
    breakpoints,
    cache_mark,
    call_params,
    schema_note,
    thinking_config,
)
from app.review.assistant.full_check_roster import (
    _PROVER_TOOLS,
    _TAG_TOOLS,
    PROVER_JOB,
    TAG_JOB,
    _Run,
)
from app.review.assistant.full_check_specialists import specialist_system as kind_system
from tests.review.assistant.chat_fakes import (
    SONNET,
    ScriptedProvider,
    reply,
    text,
    tool,
)
from tests.review.assistant.legacy_call_params import legacy_call_params
from tests.review.assistant.test_chat_engine import Harness

_SHAPE = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
_TOOLS = (
    {"name": "a", "description": "A.", "input_schema": {"type": "object"}},
    {"name": "b", "description": "B.", "input_schema": {"type": "object"}, "defer_loading": True},
    {"name": "c", "description": "C.", "input_schema": {"type": "object"}},
)
_PICTURE = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": "AA=="},
}


def _messages(turns: int) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [_PICTURE, {"type": "text", "text": "Compare the pages."}]}
    ]
    for turn in range(turns):
        messages.append(
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": f"t{turn}", "name": "a", "input": {}}],
            }
        )
        messages.append(
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": f"t{turn}", "content": "ok"}],
            }
        )
    return messages


@pytest.fixture(autouse=True)
def _routing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        chat_request,
        "_openrouter_routing",
        lambda: {"only": ["google-vertex/global"], "allow_fallbacks": False},
    )


@pytest.mark.parametrize("provider", ["anthropic", "openrouter"])
@pytest.mark.parametrize("short", ["opus", "sonnet", "haiku"])
@pytest.mark.parametrize("final", [False, True])
@pytest.mark.parametrize("shaped", [False, True])
@pytest.mark.parametrize("tools", [(), _TOOLS])
@pytest.mark.parametrize("turns", [0, 2])
def test_the_legacy_layout_sends_byte_for_byte_the_old_request(
    provider: str, short: str, final: bool, shaped: bool, tools: Any, turns: int
) -> None:
    model = BY_SHORT[short]
    given: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "effort": "medium",
        "thinking": thinking_config(model, True),
        "system": "SYSTEM",
        "tools": tools,
        "messages": _messages(turns),
        "final": final,
        "max_tokens": 4000,
        "context_management": {"edits": [{"type": "clear_tool_uses_20250919"}]} if turns else None,
        "output_format": _SHAPE if shaped else None,
    }
    new = call_params(**given)
    old = legacy_call_params(**given)
    assert json.dumps(new, sort_keys=False) == json.dumps(old, sort_keys=False)
    # The default plan is the legacy one, said explicitly too.
    assert call_params(**given, cache=LEGACY_CACHE, final_choice="none") == old


def _call(**changes: Any) -> dict[str, Any]:
    model = BY_SHORT["sonnet"]
    given: dict[str, Any] = {
        "provider": "anthropic",
        "model": model,
        "effort": "low",
        "thinking": thinking_config(model, True),
        "system": "SYSTEM",
        "tools": _TOOLS[:1],
        "messages": _messages(1),
        "final": False,
        "max_tokens": 1000,
    }
    return call_params(**{**given, **changes})


def test_a_single_shot_has_no_breakpoint_on_its_newest_block() -> None:
    params = _call(messages=_messages(0), tools=(), cache=SINGLE_SHOT)
    last = params["messages"][-1]["content"][-1]
    assert "cache_control" not in last
    assert breakpoints(params) == ["5m"]  # the system prompt's only
    assert "cache_control" in params["system"][0]


def test_a_caller_puts_breakpoints_where_it_says_and_none_elsewhere() -> None:
    marked = _messages(1)
    marked[0]["content"][0] = cache_mark(marked[0]["content"][0])
    params = _call(messages=marked, cache=CachePlan(tools=None, system="5m", newest="5m"))
    assert "cache_control" not in params["tools"][0]
    assert params["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert params["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert breakpoints(params) == ["5m", "5m", "5m"]


def test_a_one_hour_breakpoint_says_its_lifetime_and_must_come_first() -> None:
    marked = _messages(0)
    marked[0]["content"][0] = cache_mark(marked[0]["content"][0], "1h")
    params = _call(messages=marked, cache=CachePlan(tools=None, system="1h", newest="5m"))
    assert params["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert breakpoints(params) == ["1h", "1h", "5m"]
    late = _messages(0)
    late[0]["content"][0] = cache_mark(late[0]["content"][0], "1h")
    with pytest.raises(CacheRefused, match="before every 5-minute"):
        _call(messages=late, cache=CachePlan(tools=None, system="5m", newest=None))


def test_more_than_four_breakpoints_are_refused_before_anything_is_sent() -> None:
    marked = _messages(1)
    marked[0]["content"] = [cache_mark(block) for block in marked[0]["content"]]
    marked[1]["content"] = [cache_mark(block) for block in marked[1]["content"]]
    with pytest.raises(CacheRefused, match="at most 4"):
        _call(messages=marked, cache=CachePlan())


def test_an_unknown_lifetime_is_refused() -> None:
    with pytest.raises(CacheRefused):
        cache_mark({"type": "text", "text": "x"}, "2h")  # type: ignore[arg-type]


def test_a_wrap_up_may_keep_its_tool_choice() -> None:
    assert _call(final=True)["tool_choice"] == {"type": "none"}
    assert _call(final=True, final_choice="keep")["tool_choice"] == {"type": "auto"}
    assert _call(final=False, final_choice="keep")["tool_choice"] == {"type": "auto"}


def test_on_openrouter_the_reply_shape_may_go_in_the_first_message_not_the_system() -> None:
    plain = _call(provider="openrouter", cache=SINGLE_SHOT, schema_in="first_message")
    shaped = _call(
        provider="openrouter", output_format=_SHAPE, cache=SINGLE_SHOT, schema_in="first_message"
    )
    assert shaped["system"] == plain["system"]
    first = shaped["messages"][0]["content"]
    assert first[-1] == {"type": "text", "text": schema_note(_SHAPE)}
    # The same block on every turn: the first message never changes as the turns grow.
    later = _call(
        provider="openrouter",
        output_format=_SHAPE,
        messages=_messages(3),
        cache=SINGLE_SHOT,
        schema_in="first_message",
    )
    assert later["messages"][0] == shaped["messages"][0]


def test_the_layout_is_a_setting_and_legacy_by_default() -> None:
    from app.config import Settings, unknown_settings
    from app.review.assistant.chat_engine import EngineSettings
    from app.review.assistant.chat_service import MessageJob

    assert Settings.model_fields["assistant_cache_layout"].default == "legacy"
    assert unknown_settings({"GV_ASSISTANT_CACHE_LAYOUT": "shared"}) == []
    assert EngineSettings.__dataclass_fields__["cache_layout"].default == "legacy"
    assert MessageJob.__dataclass_fields__["cache_layout"].default == "legacy"


# ---- the `shared` layout in the engine (a scripted AI; no network) ---------------------------------

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


def _layout(harness: Harness, layout: str, **more: Any) -> None:
    engine = harness.engine
    engine.settings = dataclasses.replace(engine.settings, cache_layout=layout, **more)


def _page_pictures(harness: Harness) -> tuple[dict[str, Any], ...]:
    """Two page pictures as the specialists on one page pair get them (the same ids each)."""
    store = harness.workspace.pictures
    store.mkdir(parents=True, exist_ok=True)
    blocks: list[dict[str, Any]] = []
    for page in (3, 4):
        (store / f"page-{page}.png").write_bytes(_PNG)
        blocks.append({"type": "text", "text": f"Page {page} overview."})
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "gv_picture",
                    "picture_id": f"page-{page}",
                    "width": 1,
                    "height": 1,
                },
            }
        )
    return tuple(blocks)


def _specialists(harness: Harness, opening: tuple[dict[str, Any], ...]) -> list[_Agent]:
    """A numbers helper, a drawn-things helper, a tag reader and a verifier on the same pages,
    made with the roster's own prefix (`_Run._prefix`)."""
    run = _Run(harness.engine)
    engine = harness.engine
    sonnet = MODELS[SONNET]
    kinds = [
        ("numbers", kind_job("numbers"), None, None, "low"),
        ("drawn", kind_job("drawn"), None, None, "low"),
        ("tags", TAG_JOB, _TAG_TOOLS, run._tools(_TAG_TOOLS), "low"),
        ("prover", PROVER_JOB, _PROVER_TOOLS, run._tools(_PROVER_TOOLS), "medium"),
    ]
    agents = []
    for lens, job, allowed, tools, effort in kinds:
        given = run._prefix(
            model=sonnet,
            job=job,
            own_system=lambda lens=lens: engine._kind_system(
                "numbers" if lens in ("tags", "prover") else lens
            ),
            effort=effort,
            allowed=allowed,
            tools=tools,
        )
        agents.append(
            _Agent(
                call_id=f"{lens}-3",
                description=f"{lens} on pages 3-4",
                prompt=f"BRIEF for the {lens} helper on pages 3-4.",
                model=sonnet,
                pages=(3, 4),
                lens=lens,
                opening=opening,
                **given,
            )
        )
    return agents


def _answer_with(script: Callable[[Mapping[str, Any]], Any]) -> ScriptedProvider:
    return ScriptedProvider(helper_answer=script)


def _sent(provider: ScriptedProvider) -> list[dict[str, Any]]:
    return [json.loads(call) for call in provider.calls]


def _first_text(params: Mapping[str, Any]) -> str:
    content = params["messages"][0]["content"]
    return "\n".join(b["text"] for b in content if b.get("type") == "text")


def test_four_specialists_on_a_page_pair_send_one_prefix(tmp_path: Path) -> None:
    provider = _answer_with(lambda _params: reply(text("Report."), model=SONNET))
    harness = Harness(tmp_path, provider)
    _layout(harness, "shared")
    agents = _specialists(harness, _page_pictures(harness))
    harness.engine._launch(agents)
    sent = _sent(provider)
    warm = [p for p in sent if _first_text(p) == "Reply with OK."]
    # Each specialist's first call (a page helper may be sent back once to finish its list).
    work = [p for p in sent if "YOUR JOB:" in _first_text(p) and len(p["messages"]) == 1]
    assert len(warm) == 1 and len(work) == 4
    # One tool list and one system prompt for every kind, the warm-up's too.
    assert all(p["tools"] == work[0]["tools"] for p in [*work, *warm])
    assert all(p["system"] == work[0]["system"] for p in [*work, *warm])
    assert [t["name"] for t in work[0]["tools"]] == [t["name"] for t in SPECIALIST_TOOLS]
    # The same effort and thinking for every kind (the highest they ran at: medium).
    assert {json.dumps([p["output_config"], p["thinking"]]) for p in [*work, *warm]} == {
        json.dumps([work[0]["output_config"], work[0]["thinking"]])
    }
    assert work[0]["output_config"]["effort"] == "medium"
    # ONE cached prefix (tools, system, the shared pictures) for the four kinds.
    assert len({call_log.prefix_hash(p) for p in work}) == 1
    for params in work:
        first = params["messages"][0]["content"]
        # The pictures first, a breakpoint after them; the kind's job and brief last.
        assert first[3]["cache_control"] == {"type": "ephemeral"}
        assert first[-1]["text"].startswith("YOUR JOB: ")
        assert "cache_control" not in params["tools"][-1]
        assert "cache_control" in params["system"][0]
        assert breakpoints(params) == ["5m", "5m", "5m"]
    # Each kind is told its own job and tools.
    tags = next(p for p in work if TAG_JOB in _first_text(p))
    assert "YOUR TOOLS: bash_tool, view, zoom." in _first_text(tags)
    # The warm-up caches the tools and system only: no breakpoint on its one-word question.
    assert breakpoints(warm[0]) == ["5m"]


def test_the_legacy_layout_keeps_each_kinds_own_prompt_and_tools(tmp_path: Path) -> None:
    provider = _answer_with(lambda _params: reply(text("Report."), model=SONNET))
    harness = Harness(tmp_path, provider)
    agents = _specialists(harness, ())
    assert [a.system for a in agents[:2]] == [
        harness.engine._kind_system("numbers"),
        harness.engine._kind_system("drawn"),
    ]
    assert agents[0].tools is None and agents[2].tools == _Run(harness.engine)._tools(_TAG_TOOLS)
    assert [a.effort for a in agents] == ["low", "low", "low", "medium"]
    assert all(a.job is None and a.allowed is None and a.warm is None for a in agents)
    harness.engine._launch(agents)
    sent = [p for p in _sent(provider) if len(p["messages"]) == 1]
    assert len(sent) == 4  # no warm-up
    assert all("YOUR JOB:" not in _first_text(p) for p in sent)
    assert len({json.dumps(p["system"]) for p in sent}) == 2  # numbers/tags/prover, drawn


def test_a_kind_is_refused_a_tool_of_another_kind_by_code(tmp_path: Path) -> None:
    def script(params: Mapping[str, Any]) -> Any:
        if _first_text(params) == "Reply with OK.":
            return reply(text("OK"), model=SONNET)
        if len(params["messages"]) == 1 and TAG_JOB in _first_text(params):
            return reply(
                tool("t1", "report_finding", {"page": 3}),
                tool("t2", "bash_tool", {"command": "echo tags"}),
                stop="tool_use",
                model=SONNET,
            )
        return reply(text("Report."), model=SONNET)

    provider = _answer_with(script)
    harness = Harness(tmp_path, provider)
    _layout(harness, "shared")
    agents = _specialists(harness, ())
    harness.engine._launch(agents)
    second = next(
        p for p in _sent(provider) if TAG_JOB in _first_text(p) and len(p["messages"]) == 3
    )
    results = {r["tool_use_id"]: r for r in second["messages"][2]["content"]}
    assert results["t1"]["is_error"] is True
    assert "not one of your tools for this job" in results["t1"]["content"][0]["text"]
    assert results["t2"].get("is_error") is not True
    assert harness.sandbox.commands == ["echo tags"]
    assert refused(_TAG_TOOLS, "report_finding") and not refused(None, "report_finding")


def test_a_wrap_up_keeps_its_tools_runs_none_and_never_changes_tools_or_system(
    tmp_path: Path,
) -> None:
    turns = {"n": 0}

    def script(params: Mapping[str, Any]) -> Any:
        if _first_text(params) == "Reply with OK.":
            return reply(text("OK"), model=SONNET)
        if kind_job("numbers") not in _first_text(params):
            return reply(text("Report."), model=SONNET)
        turns["n"] += 1
        if turns["n"] <= 3:
            # Two tool turns, then (at the turn limit) a wrap-up that still asks for a tool.
            return reply(
                tool(f"t{turns['n']}", "bash_tool", {"command": f"echo {turns['n']}"}),
                stop="tool_use",
                model=SONNET,
            )
        return reply(text("My report."), model=SONNET)

    provider = _answer_with(script)
    harness = Harness(tmp_path, provider, helper_turns=2)
    _layout(harness, "shared")
    agents = _specialists(harness, ())[:2]
    entries = harness.engine._launch(agents)
    numbers = [p for p in _sent(provider) if kind_job("numbers") in _first_text(p)]
    assert len(numbers) == 4
    # Thinking blocks are bound to the tools and system prompt: the same on every turn.
    assert all(p["tools"] == numbers[0]["tools"] for p in numbers)
    assert all(p["system"] == numbers[0]["system"] for p in numbers)
    # Append-only: each request's history starts with the one before it.
    plain = [call_log._canonical(p["messages"]) for p in numbers]
    for before, after in itertools.pairwise(plain):
        assert after[: len(before)] == before
    # The wrap-up (turn limit) keeps `tool_choice`; nothing it asked for ran.
    assert [p["tool_choice"] for p in numbers] == [{"type": "auto"}] * 4
    assert harness.sandbox.commands == ["echo 1", "echo 2"]
    last = numbers[-1]["messages"]
    answered = last[-1]["content"]
    assert answered[0]["tool_use_id"] == "t3" and answered[0]["is_error"] is True
    assert WRAP_UP_NOT_RUN in answered[0]["content"][0]["text"]
    assert answered[-1]["text"] == WRAP_UP_TOOLS_NOTE
    assert entries[0]["report"] == "My report."


def test_under_legacy_a_wrap_up_still_sends_no_tool_choice(tmp_path: Path) -> None:
    turns = {"n": 0}

    def script(params: Mapping[str, Any]) -> Any:
        turns["n"] += 1
        if turns["n"] <= 2:
            return reply(
                tool(f"t{turns['n']}", "bash_tool", {"command": "echo"}),
                stop="tool_use",
                model=SONNET,
            )
        return reply(text("My report."), model=SONNET)

    provider = _answer_with(script)
    harness = Harness(tmp_path, provider, helper_turns=2)
    agents = _specialists(harness, ())[:1]
    harness.engine._launch(agents)
    assert [p["tool_choice"] for p in _sent(provider)][-1] == {"type": "none"}


def test_a_one_off_call_under_shared_puts_nothing_on_its_newest_block(tmp_path: Path) -> None:
    provider = _answer_with(lambda _params: reply(text("{}"), model="claude-haiku-5-5"))
    for layout, newest in (("legacy", True), ("shared", False)):
        harness = Harness(tmp_path / layout, provider)
        _layout(harness, layout)
        content = [{"type": "text", "text": "Read the value."}]
        harness.engine._second_read_call(content, _Share(limit=10**9))
        params = _sent(provider)[-1]
        assert ("cache_control" in params["messages"][-1]["content"][-1]) is newest


def test_the_shared_system_prompt_carries_every_kinds_knowledge_and_no_kinds_job() -> None:
    shared = specialist_system()
    for name, _section in SPECIALIST_KNOWLEDGE:
        assert f'<knowledge file="{name}">' in shared
    for kind in ("numbers", "drawn", "both"):
        assert kind_job(kind) not in shared
        # Every knowledge section a kind had is in the shared prompt (files whole).
        assert "YOUR JOB: " in kind_system(kind)
    assert "YOUR JOB: " not in shared
    assert specialist_effort({"low", "medium"}) == "medium"
    assert job_text("x.", None).endswith("code refuses it.")


# ---- the warm-up's start and the wait before a place ----------------------------------------------


class _HeldStream:
    """A reply whose first event comes at once and the rest only when `release` is set."""

    def __init__(self, events: list[dict[str, Any]], release: threading.Event) -> None:
        self.events, self.release = events, release

    def __iter__(self) -> Iterator[dict[str, Any]]:
        yield self.events[0]
        self.release.wait(10)
        yield from self.events[1:]

    def close(self) -> None:
        self.release.set()


class _HeldProvider:
    name = "openrouter"

    def __init__(self) -> None:
        self.release = threading.Event()
        self.calls: list[dict[str, Any]] = []

    def open_stream(self, params: Mapping[str, Any]) -> _HeldStream:
        self.calls.append(dict(params))
        return _HeldStream(reply(text("OK"), model=SONNET), self.release)


def test_the_warm_up_signals_when_its_reply_starts_on_openrouter_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = _HeldProvider()
    harness = Harness(tmp_path, ScriptedProvider())
    harness.engine.provider = held  # type: ignore[assignment]
    _layout(harness, "shared")
    agent = _specialists(harness, ())[0]
    event = harness.engine.warm_prefix(agent.warm or "", agent)
    # Set at the reply's first event, while the warm-up is still answering.
    assert event.wait(5)
    assert not held.release.is_set()
    # The same prefix asked again soon after: the same warm-up, no second call.
    assert harness.engine.warm_prefix(agent.warm or "", agent) is event
    held.release.set()
    assert len(held.calls) == 1
    assert held.calls[0]["model"] == MODELS[SONNET].openrouter


def test_an_agent_waits_for_its_warm_up_before_its_place_and_a_stop_ends_the_wait(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, ScriptedProvider())
    agent = _specialists(harness, ())[0]
    agent.warm_event = threading.Event()
    harness.engine.cancel.set()
    harness.engine._await_warm(agent)  # returns at once: the check was stopped
    agent.warm_event.set()
    harness.engine.cancel.clear()
    harness.engine._await_warm(agent)


def test_a_marked_picture_keeps_its_breakpoint_when_its_bytes_are_put_back() -> None:
    ref = {"type": "image", "source": {"type": "gv_picture", "picture_id": "p"}}
    plain = chat_request.inline_pictures([{"role": "user", "content": [ref]}], lambda _id: b"x")
    assert set(plain[0]["content"][0]) == {"type", "source"}
    marked = chat_request.inline_pictures(
        [{"role": "user", "content": [cache_mark(ref)]}], lambda _id: b"x"
    )
    assert marked[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert marked[0]["content"][0]["source"]["data"] == "eA=="


def _png(width: int, height: int) -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(out, format="PNG")
    return out.getvalue()


def test_the_page_pictures_are_made_once_the_way_view_shows_them(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ScriptedProvider())
    folder = harness.workspace.set_folder / "pages" / "Shop_set"
    folder.mkdir(parents=True)
    for page in (3, 4):
        (folder / f"p-{page:03d}.png").write_bytes(_png(40, 30))
    engine = harness.engine
    group = engine.page_group("Shop set.pdf", (3, 4))
    assert [b["type"] for b in group] == ["text", "image", "text", "image"]
    viewed = engine.tools.view(
        {"path": "/mnt/user-data/set/pages/Shop_set/p-003.png"}, {"id": "x"}, 1568
    )
    assert group[0] == viewed.content[0]
    # The very same blocks (the same picture ids) for every specialist given them.
    assert engine.page_group("Shop set.pdf", [3, 4]) == group
    # A page that cannot be shown: none at all (the helper views its pages itself).
    assert engine.page_group("Shop set.pdf", (3, 9)) == []


def test_on_openrouter_a_reply_shape_leaves_the_shared_system_prompt_alone(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        name="openrouter", helper_answer=lambda _p: reply(text("{}"), model=SONNET)
    )
    harness = Harness(tmp_path, provider)
    _layout(harness, "shared")
    numbers, _drawn, tags, _prover = _specialists(harness, ())
    tags.output_format = {"type": "object", "properties": {}}
    harness.engine._launch([numbers, tags])
    first = [p for p in _sent(provider) if "YOUR JOB:" in _first_text(p)]
    shaped = next(p for p in first if TAG_JOB in _first_text(p))
    plain = next(p for p in first if TAG_JOB not in _first_text(p))
    assert shaped["system"] == plain["system"]
    assert shaped["messages"][0]["content"][-1]["text"] == schema_note(tags.output_format)
