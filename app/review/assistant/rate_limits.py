"""One rate limiter per AI model for the whole server: how many calls run at once, and the
provider's per-minute limits (requests, input tokens, output tokens).

A full check on a big drawing set starts many specialists at once. A bigger fixed number of
agents is not the answer (production pipeline research, 2026-10-22): the provider counts requests
and tokens per minute and per model, and answers "too many requests" (429) past them. So every
call to a model goes through that model's limiter:

- **At once.** At most `limit` calls (or agents, `agent_slot`) of a model run together. The limit
  starts at the configured most (`GV_ASSISTANT_CALLS_AT_ONCE`) and adapts: a "too many requests"
  answer halves it, and after `GROW_AFTER` calls answered in a row it grows by one again, up to the
  most (additive increase, multiplicative decrease).
- **Per minute.** Requests, input tokens and output tokens in the last 60 s stay under the tier's
  limits (`GV_ASSISTANT_RATE_TIER`: Anthropic's start / build / scale). Prompt cache reads do not
  count against the input limit (Anthropic's rule), so only fresh input is counted. A new call
  waits while a minute's count is at its limit.
- **An agent holds its slot.** A specialist (`agent_slot`) holds one slot of its model from its
  first call to its report, so specialists waiting for a slot hold no money; the calls it makes
  inside count against the per-minute limits only (`thread-local`, never twice for "at once").
- **A spending limit is never retried** (`chat_provider.failure_from` tells it apart): only a busy
  answer slows the limiter down.

Plain standard library: one `threading.Condition` per model. Waits poll the message's cancel flag.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic
from typing import Final

__all__ = [
    "GROW_AFTER",
    "TIERS",
    "Limits",
    "ModelLimiter",
    "limiter_for",
    "reset_limiters",
]

#: Anthropic's per-model limits by usage tier (fetched 2026-10-22): requests, input tokens and
#: output tokens a minute.
TIERS: Final[dict[str, tuple[int, int, int]]] = {
    "start": (1_000, 2_000_000, 400_000),
    "build": (5_000, 5_000_000, 1_000_000),
    "scale": (10_000, 10_000_000, 2_000_000),
}
#: Calls answered in a row before the "at once" limit grows by one again after a busy answer.
GROW_AFTER: Final = 20
_WINDOW_S: Final = 60.0
#: How long a waiting call sleeps at most before it looks at the cancel flag again (seconds).
_POLL_S: Final = 1.0


@dataclass(frozen=True, slots=True)
class Limits:
    """One model's limits: calls at once (the most), and per minute."""

    at_once: int
    requests_per_minute: int
    input_per_minute: int
    output_per_minute: int

    @classmethod
    def of(cls, tier: str, at_once: int) -> Limits:
        rpm, itpm, otpm = TIERS.get(tier, TIERS["start"])
        return cls(max(1, at_once), rpm, itpm, otpm)


class ModelLimiter:
    """One model's calls at once and per minute (see the module's words)."""

    def __init__(
        self,
        model: str,
        limits: Limits,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.model = model
        self.limits = limits
        self.limit = limits.at_once
        self.in_flight = 0
        self.busy = 0
        self.most_in_flight = 0
        self.waited_s = 0.0
        self._ok_in_row = 0
        self._clock = clock
        self._cond = threading.Condition()
        self._requests: deque[float] = deque()
        self._tokens: deque[tuple[float, int, int]] = deque()

    # -- settings --

    def configure(self, limits: Limits) -> None:
        """New limits (the settings changed): the "at once" limit starts again from the most."""
        with self._cond:
            if limits != self.limits:
                self.limits = limits
                self.limit = limits.at_once
                self._ok_in_row = 0
                self._cond.notify_all()

    # -- the windows --

    def _trim(self, now: float) -> None:
        while self._requests and now - self._requests[0] >= _WINDOW_S:
            self._requests.popleft()
        while self._tokens and now - self._tokens[0][0] >= _WINDOW_S:
            self._tokens.popleft()

    def _minute_full(self, now: float) -> float | None:
        """Seconds until the oldest count leaves the minute, when a per-minute limit is reached;
        `None` when a call may start now."""
        self._trim(now)
        limits = self.limits
        full = len(self._requests) >= limits.requests_per_minute
        if not full and self._tokens:
            fresh = sum(entry[1] for entry in self._tokens)
            out = sum(entry[2] for entry in self._tokens)
            full = fresh >= limits.input_per_minute or out >= limits.output_per_minute
        if not full:
            return None
        starts = [self._requests[0]] if self._requests else []
        if self._tokens:
            starts.append(self._tokens[0][0])
        oldest = min(starts, default=now)
        return max(0.01, _WINDOW_S - (now - oldest))

    # -- slots --

    @staticmethod
    def _holding() -> int:
        return int(getattr(_AGENT, "held", 0))

    def acquire(
        self, cancel: threading.Event | None = None, *, slot: bool = True, request: bool = True
    ) -> bool:
        """Waits for room for one call (and, with `slot`, one of the "at once" places; `request`:
        the call counts as a request of the minute). Returns False when `cancel` was set while
        waiting (nothing taken)."""
        started = self._clock()
        with self._cond:
            while True:
                if cancel is not None and cancel.is_set():
                    return False
                now = self._clock()
                wait = self._minute_full(now)
                if wait is None and (not slot or self.in_flight < self.limit):
                    if slot:
                        self.in_flight += 1
                        self.most_in_flight = max(self.most_in_flight, self.in_flight)
                    if request:
                        self._requests.append(now)
                    self.waited_s += now - started
                    return True
                self._cond.wait(min(_POLL_S, wait if wait is not None else _POLL_S))

    def release(
        self,
        *,
        slot: bool = True,
        fresh_input: int = 0,
        output: int = 0,
        busy: bool = False,
        answered: bool = True,
    ) -> None:
        """A call ended: its fresh input and output tokens count for the minute; a busy answer
        halves the "at once" limit, `GROW_AFTER` answered calls in a row grow it by one."""
        with self._cond:
            if slot:
                self.in_flight = max(0, self.in_flight - 1)
            if fresh_input or output:
                self._tokens.append((self._clock(), max(0, fresh_input), max(0, output)))
            if busy:
                self.busy += 1
                self.limit = max(1, self.limit // 2)
                self._ok_in_row = 0
            elif answered:
                self._ok_in_row += 1
                if self._ok_in_row >= GROW_AFTER and self.limit < self.limits.at_once:
                    self.limit += 1
                    self._ok_in_row = 0
            self._cond.notify_all()

    @contextmanager
    def agent_slot(self, cancel: threading.Event | None = None) -> Iterator[bool]:
        """One of the "at once" places for a whole specialist: its own calls then count per
        minute only (`call`). Yields False when cancelled before a place was free."""
        if not self.acquire(cancel, slot=True, request=False):
            yield False
            return
        _AGENT.held = self._holding() + 1
        try:
            yield True
        finally:
            _AGENT.held = self._holding() - 1
            self.release(slot=True, answered=False)

    def call_slot(self) -> bool:
        """Whether a call on this thread takes an "at once" place of its own: not when a
        specialist on this thread holds a place (of any model: a tool's own call inside a
        specialist never waits for a place, so two kinds of specialists never wait on each
        other)."""
        return self._holding() == 0

    def snapshot(self) -> dict[str, float | int]:
        """For the timings line: the limit now, the most calls at once, busy answers, waits."""
        with self._cond:
            return {
                "limit": self.limit,
                "most_at_once": self.most_in_flight,
                "busy": self.busy,
                "waited_s": round(self.waited_s, 1),
            }


#: The specialist places this thread holds (any model): its calls take no place of their own.
_AGENT = threading.local()
_LIMITERS: dict[str, ModelLimiter] = {}
_LOCK = threading.Lock()


def limiter_for(model: str, limits: Limits) -> ModelLimiter:
    """The server's one limiter for `model`, with `limits` (set again when they changed)."""
    with _LOCK:
        found = _LIMITERS.get(model)
        if found is None:
            found = _LIMITERS[model] = ModelLimiter(model, limits)
            return found
    found.configure(limits)
    return found


def reset_limiters() -> None:
    """Forget every limiter (tests)."""
    with _LOCK:
        _LIMITERS.clear()
