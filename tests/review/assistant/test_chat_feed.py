"""A running chat message's feed (#1479): replay, then live, bounded, closed once.

- A follower joining late gets every event so far, streamed words merged, then each new one.
- A follower joining after the end gets the replay and its stream ends.
- Past its size bound the feed keeps no more events for later followers, except the ending ones.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from app.review.assistant.chat_feed import MessageFeed


def _collect(feed: MessageFeed, after_first: Any = None) -> list[tuple[str, dict[str, Any]]]:
    async def go() -> list[tuple[str, dict[str, Any]]]:
        seen = []
        async for item in feed.follow():
            seen.append(item)
            if len(seen) == 1 and after_first is not None:
                threading.Thread(target=after_first).start()
        return seen

    return asyncio.run(go())


def test_a_late_follower_gets_the_words_so_far_merged_then_the_rest_live() -> None:
    feed = MessageFeed()
    feed.publish("message_start", {"message_id": "m"})
    feed.publish("thinking_delta", {"index": 0, "text": "Let me "})
    feed.publish("thinking_delta", {"index": 0, "text": "look."})
    feed.publish("usage", {"cost_usd": "0.01"})
    feed.publish("usage", {"cost_usd": "0.02"})
    feed.publish("text_delta", {"index": 1, "text": "Two "})
    feed.publish("text_delta", {"index": 2, "text": "items"})

    def rest() -> None:
        feed.publish("text_delta", {"index": 2, "text": " differ."})
        feed.publish("message_end", {"message": {"id": "m"}})
        feed.close()

    seen = _collect(feed, after_first=rest)

    assert seen == [
        ("message_start", {"message_id": "m"}),
        ("thinking_delta", {"index": 0, "text": "Let me look."}),
        ("usage", {"cost_usd": "0.02"}),
        ("text_delta", {"index": 1, "text": "Two "}),
        ("text_delta", {"index": 2, "text": "items"}),
        ("text_delta", {"index": 2, "text": " differ."}),
        ("message_end", {"message": {"id": "m"}}),
    ]
    # What a follower joining now would be replayed: the same answer, merged.
    assert feed.kept()[-2] == ("text_delta", {"index": 2, "text": "items differ."})


def test_a_follower_after_the_end_gets_the_replay_and_its_stream_ends() -> None:
    feed = MessageFeed()
    feed.publish("message_start", {"message_id": "m"})
    feed.publish("message_end", {"message": {"id": "m"}})
    feed.close()
    feed.publish("text_delta", {"index": 0, "text": "too late"})

    assert feed.closed
    assert [name for name, _ in _collect(feed)] == ["message_start", "message_end"]


def test_past_its_bound_the_feed_keeps_only_the_ending_events() -> None:
    feed = MessageFeed(max_bytes=200)
    feed.publish("message_start", {"message_id": "m"})
    feed.publish("step_end", {"index": 0, "step": {"output": "x" * 500}})
    feed.publish("error", {"message": "Stopped."})
    feed.publish("message_end", {"message": {"id": "m"}})

    assert [name for name, _ in feed.kept()] == ["message_start", "error", "message_end"]
