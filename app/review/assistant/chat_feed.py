"""The events of one running chat message, kept so a screen can come back to it (#1479).

claude.ai keeps answering when you close the tab, and shows the answer streaming when you come
back. A message here runs in its own thread until it ends (or the reviewer presses Stop); its
events go into this feed. Every screen that reads the message is one follower: the `POST` that sent
it, and `GET .../messages/{id}/events` from a reload or another tab. A follower first gets every
event so far (the replay), then each new one as it happens, until the feed closes.

**Bounded.** Streamed words and thinking arrive as many small deltas; consecutive deltas of the
same block are merged into one kept event, and a newer `usage` replaces the one before it, so what
is kept grows with the answer itself, not with how it was cut. Past `max_bytes` no more events are
kept for later followers (live followers still get them all); `message_end` and `error` are always
kept. A follower that joins after that sees a gap, which `message_end`'s stored message (the
authoritative answer) fills in.

Thread-safe: the engine and its helpers publish from several threads; each follower is an asyncio
queue on its own event loop, fed with `call_soon_threadsafe` under the feed's lock, so every
follower sees the events in the order they were kept.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable
from typing import Any, Final

__all__ = ["DEFAULT_MAX_BYTES", "MessageFeed"]

type Event = tuple[str, dict[str, Any]]

#: About what a long whole-set answer's events take, with plenty of room.
DEFAULT_MAX_BYTES: Final = 32 * 1024 * 1024
_DELTAS: Final = frozenset({"text_delta", "thinking_delta"})
_ALWAYS_KEPT: Final = frozenset({"message_end", "error"})


class MessageFeed:
    """One running message's events: kept for replay, and sent on to every follower."""

    def __init__(self, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self._lock = threading.Lock()
        self._events: list[Event] = []
        self._size = 0
        self._max_bytes = max_bytes
        self._closed = False
        self._followers: list[Callable[[Event | None], None]] = []

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def kept(self) -> list[Event]:
        """The events a follower joining now would be replayed."""
        with self._lock:
            return list(self._events)

    def publish(self, event: str, data: dict[str, Any]) -> None:
        """One event: kept for later followers, and sent to the ones following now."""
        with self._lock:
            if self._closed:
                return
            self._keep(event, data)
            for push in self._followers:
                push((event, data))

    def close(self) -> None:
        """The message has ended: every follower's stream ends after what it has been sent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            followers, self._followers = self._followers, []
            for push in followers:
                push(None)

    def _keep(self, event: str, data: dict[str, Any]) -> None:
        last = self._events[-1] if self._events else None
        if (
            event in _DELTAS
            and last is not None
            and last[0] == event
            and last[1].get("index") == data.get("index")
        ):
            text = str(data.get("text") or "")
            merged = {**last[1], "text": str(last[1].get("text") or "") + text}
            self._events[-1] = (event, merged)
            self._size += len(text)
            return
        if event == "usage" and last is not None and last[0] == "usage":
            self._events[-1] = (event, dict(data))
            return
        size = len(json.dumps(data, ensure_ascii=False))
        if self._size + size > self._max_bytes and event not in _ALWAYS_KEPT:
            return
        self._events.append((event, dict(data)))
        self._size += size

    async def follow(self) -> AsyncIterator[Event]:
        """Every event so far, then each new one, until the message ends (or the caller stops
        reading, which changes nothing for the message)."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Event | None] = asyncio.Queue()

        def push(item: Event | None) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                pass  # The follower's loop has closed: nothing is reading any more.

        with self._lock:
            replay = list(self._events)
            closed = self._closed
            if not closed:
                self._followers.append(push)
        try:
            for item in replay:
                yield item
            if closed:
                return
            while True:
                next_item = await queue.get()
                if next_item is None:
                    return
                yield next_item
        finally:
            with self._lock:
                if push in self._followers:
                    self._followers.remove(push)
