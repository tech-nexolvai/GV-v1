"""A small, bounded pool for code's own $0 steps, results kept in the order given (#1850).

A full check's code steps (the provers' `verify_value` / `read_number` reads, the finish's free
steps) each read one item at a time: OCR and page renders, about two seconds an item, so a big set
spent minutes on them while the machine's other cores waited. This pool runs such a step on a few
threads at once and changes nothing else:

- **The same results, in the same order.** `run_ordered` hands back one `Done` per item in the
  order the items were given, whatever order the threads finished in; the caller applies them in
  that order, as the one-at-a-time loop did. A step run here must not depend on another item's
  answer (each item's reads are its own).
- **Started in order.** The items start in the order given (the pool's queue is first in, first
  out), so a stop or a time budget checked when an item starts leaves out a tail of the list,
  as before.
- **Nothing dropped silently.** An item whose step raised is reported (`Done.error`), never left
  out; an item not started because the check was stopped says so (`Done.ran` is False).
- **Stop stops everything.** When `stop()` turns true, no further item starts and the pool does
  not wait for the queue; the items already running end their current read.
- **Modest.** At most `code_workers()` threads: `GV_ASSISTANT_CODE_WORKERS`, else
  min(`DEFAULT_MAX_WORKERS`, cores - 2), at least one (one is the old one-at-a-time loop, run on
  the caller's thread). OCR is the work: Tesseract runs as its own process per read and pdfium
  renders hold `chat_drawing.PDFIUM_LOCK`, so threads are enough and memory stays the process's.

Each item runs in a copy of the caller's context (`contextvars`), so a step that reads a context
variable sees what the caller set.
"""

from __future__ import annotations

import contextvars
import logging
import os
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Final

__all__ = ["DEFAULT_MAX_WORKERS", "Done", "code_workers", "run_ordered"]

_log = logging.getLogger(__name__)

#: The most threads by default; the machine may run other checks too.
DEFAULT_MAX_WORKERS: Final = 4
#: The setting that overrides it (1 runs one item at a time, on the caller's thread).
WORKERS_SETTING: Final = "GV_ASSISTANT_CODE_WORKERS"
#: How often a wait looks at `stop()` (seconds).
_STOP_POLL_S: Final = 0.5


@dataclass(frozen=True, slots=True)
class Done[R]:
    """One item's outcome: its answer, or the error its step raised, or not run (stopped)."""

    index: int
    value: R | None = None
    error: BaseException | None = None
    ran: bool = True


def code_workers() -> int:
    """How many threads code's steps may use (`WORKERS_SETTING`, else the default)."""
    raw = os.environ.get(WORKERS_SETTING, "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            _log.warning("%s is not a whole number: %r", WORKERS_SETTING, raw)
    return max(1, min(DEFAULT_MAX_WORKERS, (os.cpu_count() or 2) - 2))


def _one[T, R](index: int, item: T, work: Callable[[T], R], stop: Callable[[], bool]) -> Done[R]:
    if stop():
        return Done(index, ran=False)
    try:
        return Done(index, value=work(item))
    except Exception as failed:  # noqa: BLE001 - reported to the caller, never dropped
        return Done(index, error=failed)


def run_ordered[T, R](
    items: Sequence[T],
    work: Callable[[T], R],
    *,
    workers: int | None = None,
    stop: Callable[[], bool] = lambda: False,
    name: str = "gv-code",
) -> list[Done[R]]:
    """`work` on every item, at most `workers` at once; one `Done` per item, in the items' order."""
    count = code_workers() if workers is None else max(1, workers)
    count = min(count, len(items))
    if count <= 1:
        return [_one(index, item, work, stop) for index, item in enumerate(items)]
    pool = ThreadPoolExecutor(max_workers=count, thread_name_prefix=name)
    futures: list[Future[Done[R]]] = []
    try:
        for index, item in enumerate(items):
            context = contextvars.copy_context()
            futures.append(pool.submit(context.run, _one, index, item, work, stop))
        pending = set(futures)
        while pending:
            if stop():
                for future in pending:
                    future.cancel()
                break
            _done, pending = wait(pending, timeout=_STOP_POLL_S, return_when=FIRST_COMPLETED)
    finally:
        # A stop does not wait for the queue: what has not started is cancelled; what runs ends
        # its current read.
        pool.shutdown(wait=True, cancel_futures=True)
    results: list[Done[R]] = []
    for index, future in enumerate(futures):
        if future.cancelled():
            results.append(Done(index, ran=False))
            continue
        results.append(future.result())
    return results
