"""Where one answer's time and money went, phase by phase (#1512).

A live full check of 60 pages ran for over 15 minutes and nobody could say which part was slow. So
every message records, per phase, the seconds it took and what its model calls cost:

- **Seconds** are wall-clock time: the union of the intervals the phase ran in, so phases that run
  at the same time (the helpers; the second reads, third reads and critic) each show their own
  span, never a sum of parallel work, and a phase that ran in pieces (the lead's turns) shows the
  pieces added up.
- **Cost** is charged to the phase of the thread that made the call (`phase()` marks it), so a
  helper's calls, a second read and the lead's turns are told apart even when they overlap.
- **Helpers** get a row each: the pages they had, their model calls, seconds and cost, and why they
  stopped.

The engine stores the result in the trust block (`timings`), shows it under "How this was
checked", and logs it.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from time import monotonic
from typing import Any, Final

__all__ = ["PHASES", "PHASE_LABELS", "Timings"]

#: The phases, in the order they are shown.
PHASES: Final = (
    "seeding",
    "set_pack",
    "planning",
    "prewarm",
    "lead",
    "helpers",
    "lead_verification",
    "second_reads",
    "third_reads",
    "critic",
    "finish_checks",
    "cloud_drawing",
    "report",
)
PHASE_LABELS: Final = {
    "seeding": "GV Review's recorded differences made findings",
    "set_pack": "Waiting for the set pack's page map",
    "planning": "Helpers planned by code",
    "prewarm": "Prompt cache warmed",
    "lead": "Lead",
    "helpers": "Helpers",
    "lead_verification": "Lead checks and notes",
    "second_reads": "Second reads",
    "third_reads": "Third reads",
    "critic": "Critic",
    "finish_checks": "Finish checks by code",
    "cloud_drawing": "Cloud drawing",
    "report": "Check report",
}
_MICROS: Final = Decimal(1_000_000)


def _dollars(micros: int) -> str:
    return str((Decimal(micros) / _MICROS).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _union(intervals: list[tuple[float, float]]) -> float:
    total = 0.0
    end = None
    for start, stop in sorted(intervals):
        if end is None or start > end:
            total += stop - start
            end = stop
        elif stop > end:
            total += stop - end
            end = stop
    return total


@dataclass(slots=True)
class _Phase:
    intervals: list[tuple[float, float]] = field(default_factory=list)
    open: dict[int, float] = field(default_factory=dict)
    micros: int = 0
    calls: int = 0


@dataclass(slots=True)
class _Helper:
    number: int
    description: str
    pages: list[int]
    started: float
    seconds: float | None = None
    micros: int = 0
    calls: int = 0
    stopped: str | None = None


class Timings:
    """One message's phase clock (thread-safe)."""

    def __init__(self, clock: Any = monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._local = threading.local()
        self._started = clock()
        self._phases: dict[str, _Phase] = {}
        self._helpers: dict[int, _Helper] = {}
        self._entries = 0

    @contextmanager
    def phase(self, name: str, helper: int | None = None) -> Iterator[None]:
        """Marks the work in this block (on this thread) as `name`, and a helper's own calls."""
        stack: list[tuple[str, int | None]] = self._local.__dict__.setdefault("stack", [])
        with self._lock:
            entry = self._entries
            self._entries += 1
            self._phases.setdefault(name, _Phase()).open[entry] = self._clock()
        stack.append((name, helper))
        try:
            yield
        finally:
            stack.pop()
            with self._lock:
                phase = self._phases[name]
                started = phase.open.pop(entry)
                phase.intervals.append((started, self._clock()))

    def current(self) -> tuple[str, int | None] | None:
        stack: list[tuple[str, int | None]] = self._local.__dict__.get("stack", [])
        return stack[-1] if stack else None

    def charge(self, micros: int) -> None:
        """One model call's cost, to this thread's phase (and helper)."""
        where = self.current()
        name, helper = where if where is not None else ("other", None)
        with self._lock:
            phase = self._phases.setdefault(name, _Phase())
            phase.micros += micros
            phase.calls += 1
            if helper is not None and helper in self._helpers:
                self._helpers[helper].micros += micros
                self._helpers[helper].calls += 1

    def helper_started(self, number: int, description: str, pages: list[int]) -> None:
        with self._lock:
            self._helpers[number] = _Helper(number, description, list(pages), self._clock())

    def helper_ended(self, number: int, stopped: str | None) -> None:
        with self._lock:
            helper = self._helpers.get(number)
            if helper is not None:
                helper.seconds = self._clock() - helper.started
                helper.stopped = stopped

    def seconds(self, name: str) -> float:
        with self._lock:
            phase = self._phases.get(name)
            if phase is None:
                return 0.0
            now = self._clock()
            return _union([*phase.intervals, *((s, now) for s in phase.open.values())])

    def as_dict(self, total_micros: int) -> dict[str, Any]:
        """The timings as stored: every phase that ran, in order, and each helper."""
        now = self._clock()
        with self._lock:
            names: list[str] = [n for n in PHASES if n in self._phases]
            names += sorted(n for n in self._phases if n not in PHASES)
            phases = []
            for name in names:
                phase = self._phases[name]
                intervals = [*phase.intervals, *((s, now) for s in phase.open.values())]
                phases.append(
                    {
                        "phase": name,
                        "label": PHASE_LABELS.get(name, name.replace("_", " ").capitalize()),
                        "seconds": round(_union(intervals), 1),
                        "cost_usd": _dollars(phase.micros),
                        "calls": phase.calls,
                    }
                )
            helpers = [
                {
                    "number": h.number,
                    "description": h.description,
                    "pages": list(h.pages),
                    "calls": h.calls,
                    "seconds": round(now - h.started if h.seconds is None else h.seconds, 1),
                    "cost_usd": _dollars(h.micros),
                    "stopped": h.stopped,
                }
                for h in sorted(self._helpers.values(), key=lambda h: h.number)
            ]
        return {
            "total_seconds": round(now - self._started, 1),
            "cost_usd": _dollars(total_micros),
            "phases": phases,
            "helpers": helpers,
        }

    @staticmethod
    def log_line(timings: dict[str, Any]) -> str:
        """The timings in one log line: "total 312.4 s $4.12 · helpers 201.0 s $2.80 (93) · …"."""
        parts = [f"total {timings['total_seconds']} s ${timings['cost_usd']}"]
        parts += [
            f"{p['phase']} {p['seconds']} s ${p['cost_usd']} ({p['calls']})"
            for p in timings["phases"]
        ]
        parts += [
            f"helper {h['number']} pages {h['pages']} {h['seconds']} s ${h['cost_usd']} "
            f"({h['calls']}){' ' + h['stopped'] if h['stopped'] else ''}"
            for h in timings["helpers"]
        ]
        return " · ".join(parts)
