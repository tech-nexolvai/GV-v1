"""A message may lower its own per-answer cap (harness audit, 2026-10-19), through the app.

The paid runner's money stop (`eval/simple/run_simple.py`) pressed Stop when the running cost
reached its stop, but the calls already in flight still finished and were billed: the paid run of
2026-10-19 stopped at $2.88 and ended at $3.94, past its $0.80 margin. The server's runaway guard
already holds what every call in flight may cost (#1677) and starts no call that would pass the
answer's cap; `cap_usd` lets a run set that cap below the server's for one message, so the money
stop is held before the calls start rather than pressed after they are paid for.

- `cap_usd` lowers this answer's cap (it is the cap stored and shown); it never raises it.
- It must be above zero.
"""

from __future__ import annotations

from tests.api.test_assistant_chats import _named, _world
from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text

pytest_plugins = ("tests.app.postgres_fixture",)


def test_a_lower_cap_holds_this_answer(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = ScriptedProvider(lead=[])
    with _world(postgres_engine, tmp_path, provider=provider) as world:
        chat_id = world.chat()

        status, events = world.ask(chat_id, "Which items are wrong?", cap_usd=0.1)

    assert status == 200
    message = _named(events, "message_end")[0]["message"]
    # No call fits $0.10 (Opus' output allowance alone is more): none is made.
    assert provider.calls == []
    assert message["stopped"] == "cost_cap" and message["cap_hit"] == "message"
    assert message["message_cap_usd"] == "0.10"


def test_a_higher_cap_never_raises_the_servers(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = ScriptedProvider(lead=[reply(text("Fine."))])
    with _world(
        postgres_engine, tmp_path, provider=provider, settings={"assistant_message_cap_usd": 3}
    ) as world:
        chat_id = world.chat()

        status, events = world.ask(chat_id, "How wide is the sink on page 4?", cap_usd=500)

    assert status == 200
    assert _named(events, "message_end")[0]["message"]["message_cap_usd"] == "3.00"


def test_a_cap_must_be_above_zero(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with _world(postgres_engine, tmp_path, provider=ScriptedProvider(lead=[])) as world:
        chat_id = world.chat()

        status, _events = world.ask(chat_id, "Anything?", cap_usd=0)

    assert status == 422
