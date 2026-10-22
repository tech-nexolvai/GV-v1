"""The full check's live progress line, worked out by code (#1518).

While a full check runs, the screen shows one line, not a log: "Checking page 12 of 60 · 8 wrong
so far · about 6 min left". This module works those numbers out from the findings and the
coverage and hands them on as a `progress` event: on each finding or coverage change, on each
phase change, and every few seconds while nothing changes (so the time left keeps moving). The
engine stores the last one as the message's `progress` block, so a reload shows it too.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from app.review.assistant.proof import proof_group

#: Seconds between progress events while nothing changes.
TICK_SECONDS: Final = 5.0

PHASES: Final = ("reading", "checking", "writing")

#: (live findings, coverage counts or None when coverage was never set, the set's page count).
Snapshot = Callable[[], tuple[Sequence[Mapping[str, Any]], Mapping[str, Any] | None, int]]


def progress_numbers(
    findings: Sequence[Mapping[str, Any]],
    coverage: Mapping[str, Any] | None,
    page_count: int,
    phase: str,
    elapsed: float,
) -> dict[str, Any]:
    """The progress line's numbers. Pages done: pages accounted for in the coverage (else pages
    with a finding); time left: the pages still to do at the pace so far (null before any page)."""
    if coverage is not None:
        total = int(coverage.get("total") or 0)
        done = total - int(coverage.get("not_opened") or 0)
    else:
        total = page_count
        done = len({int(f.get("page") or 0) for f in findings if f.get("page")})
    total = max(total, done)
    eta: int | None = None
    if done > 0 and elapsed > 0:
        eta = round((total - done) * elapsed / done)
    groups: dict[str, int] = {}
    for finding in findings:
        group = proof_group(finding)
        groups[group] = groups.get(group, 0) + 1
    return {
        "phase": phase,
        "pages_done": done,
        "pages_total": total,
        # The cloud drawing's own counts (#1619): Wrong only when code proved it, so the live
        # line never says more Wrong than the files will (`proof.proof_group`); what is not
        # proved yet is Not compared (`decision_so_far`, the old name kept for the screen).
        "wrong_so_far": groups.get("clouded", 0),
        "decision_so_far": groups.get("not_compared", 0),
        "right_so_far": groups.get("right", 0) + groups.get("dismissed", 0),
        # Decided (#1839): the architect's size breaks a GV rule, GV asks the architect.
        "rule_note_so_far": groups.get("rule_note", 0),
        "eta_seconds": eta,
    }


def with_work(
    numbers: dict[str, Any], work: tuple[int, int] | None, elapsed: float
) -> dict[str, Any]:
    """The simple full check's drawings (#1665): "Comparing drawing 12 of 36", and the time left at
    the pace of the drawings so far (not the pages')."""
    if work is None:
        return numbers
    done, total = work
    eta: int | None = None
    if 0 < done < total and elapsed > 0:
        eta = round((total - done) * elapsed / done)
    return {**numbers, "drawings_done": done, "drawings_total": total, "eta_seconds": eta}


class FullCheckProgress:
    """Publishes the progress numbers when they change, and on a timer while they do not."""

    def __init__(
        self,
        snapshot: Snapshot,
        publish: Callable[[dict[str, Any]], None],
        *,
        clock: Callable[[], float] = time.monotonic,
        every: float = TICK_SECONDS,
    ) -> None:
        self._snapshot = snapshot
        self._publish = publish
        self._clock = clock
        self._every = every
        self._started = clock()
        self._phase = "reading"
        self._last: dict[str, Any] | None = None
        self._last_at = self._started
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ticker: threading.Thread | None = None
        self._work: tuple[int, int] | None = None
        self._reading: tuple[int, int] | None = None

    def current(self) -> dict[str, Any]:
        findings, coverage, pages = self._snapshot()
        elapsed = self._clock() - self._started
        numbers = progress_numbers(findings, coverage, pages, self._phase, elapsed)
        if self._reading is not None and self._phase == "reading":
            numbers["reading_done"], numbers["reading_total"] = self._reading
        return with_work(numbers, self._work, elapsed)

    def set_reading(self, done: int, total: int) -> None:
        """The drawings' pages read so far, while the check waits for them (#1871): "Reading the
        drawings: 40 of 188 pages"."""
        self._reading = (done, total)
        self.changed(force=True)

    def set_work(self, done: int, total: int) -> None:
        """The simple full check's drawings compared so far, of the work list's (#1665)."""
        self._work = (done, total)
        self.changed(force=True)

    def changed(self, *, force: bool = False) -> None:
        """Publish when the numbers (other than the time left) changed, or when forced."""
        try:
            numbers = self.current()
        except Exception:  # noqa: BLE001 - progress never stops the check
            return
        with self._lock:
            last = self._last
            same = last is not None and {**last, "eta_seconds": None} == {
                **numbers,
                "eta_seconds": None,
            }
            if same and not force:
                return
            self._last, self._last_at = numbers, self._clock()
        self._publish(numbers)

    def phase(self, name: str) -> None:
        self._phase = name
        self.changed(force=True)

    def start(self) -> None:
        self.changed(force=True)
        self._ticker = threading.Thread(target=self._tick, name="chat-progress", daemon=True)
        self._ticker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ticker is not None:
            self._ticker.join(timeout=1)

    def _tick(self) -> None:
        while not self._stop.wait(min(1.0, self._every)):
            if self._clock() - self._last_at >= self._every:
                self.changed(force=True)
