"""A set's pages read at once, one page per task, in worker processes (#1871).

**Why.** A 188-page upload (2026-10-22) ran out of the labels step's 40 minutes before the drawn
pass even started: the labels' process read every page one after another (text, pasted pictures,
then the drawn ink), with time budgets shared by the whole set. Now each page is a task:

- **Each worker is a process of its own** (`spawn`), with its own PDF engine: two pages read at once
  never meet inside pdfium (it is not thread-safe), and a page that crashes the engine kills only
  its worker. That page is read once more by a fresh worker; if it crashes that one too, it says
  "page N could not be read: the PDF reader crashed" and every other page is used.
- **Time limits per page, not per set** (the reader's `limits`), and a page still running after
  `PAGE_LIMIT_S` is stopped (its worker killed): "page N could not be read: it took longer than
  ...". So a set of any size reads in about (pages x a page's time) / workers.
- **Kept as each page finishes** (`keep`, by the page's PDF bytes, its inputs and the code's
  version: the same page with the same code is never read twice), so a run stopped by a time limit
  or a restart resumes with the pages already read. A page the reader marks unfinished is read
  again.
- **Gentle on a shared machine:** at most `workers()` processes (`WORKERS_SETTING`, else
  min(`MOST_WORKERS`, cores - 2)); Tesseract one thread inside each (`OMP_THREAD_LIMIT=1`); a
  worker is replaced after `PAGES_PER_WORKER` pages or when it holds more than
  `WORKER_MEMORY_MB`; no new page starts while the machine has less than `MIN_FREE_MB` free (one
  worker always goes on).
- **The reading table** (`Table`): each page `waiting`, `reading`, `ready` or `failed` (with why),
  published whole on every change. The set pack writes it as `reading.json`; the full check waits
  for it (`full_check_plan.wait_for_reading`) and the screen shows its counts.

The interface is a task stream (`PageTask` in, one result per task out, `on_result` as each
finishes, the table as the status) so it can move onto a job queue without changing its callers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing
import os
import pickle
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from multiprocessing.connection import Connection, wait
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

__all__ = [
    "CRASHED",
    "MOST_WORKERS",
    "PAGES_FOLDER",
    "PAGE_LIMIT_S",
    "WORKERS_SETTING",
    "PageTask",
    "Table",
    "read_pages",
    "workers",
]

_log = logging.getLogger(__name__)

#: How many worker processes (`workers`); 1 reads every page in this process.
WORKERS_SETTING: Final = "GV_DIM_LABELS_WORKERS"
#: The most workers by default: the machine runs the API, the database and other checks too.
MOST_WORKERS: Final = 6
#: The pages kept as they finish, in the pack's folder.
PAGES_FOLDER: Final = ".dims-pages"
#: Why a page that crashed two workers has no result.
CRASHED: Final = "the PDF reader crashed"
#: The most seconds one page may take in a worker before it is stopped.
PAGE_LIMIT_S: Final = 900.0
#: A worker is replaced after this many pages (memory a long-lived reader keeps is given back).
PAGES_PER_WORKER: Final = 25
#: A worker holding more than this many MB is replaced after its page.
WORKER_MEMORY_MB: Final = 2500
#: No new page starts while the machine has less than this many MB free (one worker goes on).
MIN_FREE_MB: Final = 1500
#: The environment each worker sets before it reads anything.
_WORKER_ENVIRONMENT: Final = {"OMP_THREAD_LIMIT": "1", "OMP_NUM_THREADS": "1"}


def workers() -> int:
    """How many worker processes read the pages (`WORKERS_SETTING`, else the default)."""
    raw = os.environ.get(WORKERS_SETTING, "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            _log.warning("%s is not a whole number: %r", WORKERS_SETTING, raw)
    return max(1, min(MOST_WORKERS, (os.cpu_count() or 2) - 2))


@dataclass(frozen=True, slots=True)
class PageTask:
    """One page to read: its upload's name and file, its number, its drawings' sides and pasted
    pictures, whether it shows a pasted picture, whether its unread ink is counted, and (for the
    set pack) its upload's listing entry for the health check and the upload itself."""

    name: str
    pdf: str
    number: int
    halves: tuple[Any, ...] = ()
    pasted: tuple[Any, ...] = ()
    with_pictures: bool = False
    count_ink: bool = False
    health: tuple[tuple[str, Any], ...] | None = None
    upload: str = ""


# ---- the reading table ---------------------------------------------------------------------------

#: A page's states in the reading table: not started, being read, read, could not be read.
WAITING: Final = "waiting"
READING: Final = "reading"
READY: Final = "ready"
FAILED: Final = "failed"


class Table:
    """The reading's status table: each page's state (`WAITING`, `READING`, `READY`, `FAILED`
    with why), published whole (`on_change`) each time one changes."""

    def __init__(
        self,
        tasks: Sequence[PageTask],
        on_change: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._rows = [{"file": task.name, "page": task.number, "state": WAITING} for task in tasks]
        self._on_change = on_change

    def set(self, index: int, state: str, error: str | None = None) -> None:
        row = self._rows[index]
        row["state"] = state
        if error:
            row["error"] = error
        if self._on_change is not None:
            try:
                self._on_change(self.snapshot())
            except Exception as failed:  # noqa: BLE001 - the table never stops the reading
                _log.warning("page reading: table not written: %s", failed)

    def snapshot(self) -> dict[str, Any]:
        states = [row["state"] for row in self._rows]
        return {
            "total": len(states),
            "ready": states.count(READY),
            "failed": states.count(FAILED),
            "pages": [dict(row) for row in self._rows],
        }


def _state_of(result: Any) -> tuple[str, str | None]:
    entry = getattr(result, "entry", None)
    error = entry.get("error") if isinstance(entry, Mapping) else getattr(result, "error", None)
    return (FAILED, str(error)) if error else (READY, None)


# ---- keeping finished pages ---------------------------------------------------------------------


def _key(task: PageTask, version: str, digests: Mapping[str, str]) -> str:
    text = json.dumps(
        [
            version,
            digests.get(task.pdf, ""),
            task.number,
            task.halves,
            task.pasted,
            task.with_pictures,
            task.count_ink,
            task.health is not None,
        ],
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:40]


def _digest(path: str) -> str:
    sha = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                sha.update(chunk)
    except OSError:
        return ""
    return sha.hexdigest()


def _kept(folder: Path | None, key: str) -> Any:
    if folder is None:
        return None
    try:
        return pickle.loads((folder / f"{key}.pkl").read_bytes())
    except Exception:  # noqa: BLE001 - not kept, or kept by other code: read again
        return None


def _keep(folder: Path | None, key: str, result: Any, finished: Callable[[Any], bool]) -> None:
    if folder is None or not finished(result):
        return
    try:
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / f".{key}.{uuid4().hex[:8]}.tmp"
        temporary.write_bytes(pickle.dumps(result))
        os.replace(temporary, folder / f"{key}.pkl")
    except Exception as failed:  # noqa: BLE001 - only a resume reads it again
        _log.warning("page reading: page not kept: %s", type(failed).__name__)


# ---- reading ------------------------------------------------------------------------------------


def read_pages(
    tasks: Sequence[PageTask],
    limits: Mapping[str, float],
    read_page: Callable[[PageTask, Mapping[str, float]], Any],
    on_result: Callable[[int, Any], None],
    *,
    unreadable: Callable[[PageTask, str], Any],
    count: int | None = None,
    keep: Path | None = None,
    version: str = "",
    table: Table | None = None,
    finished: Callable[[Any], bool] = lambda _result: True,
    page_limit_s: float = PAGE_LIMIT_S,
) -> None:
    """Read every task's page with `read_page(task, limits)` (`limits`: the reader's own time
    limits for one page); `on_result(index, result)` as each one finishes (any order). A page
    that crashed two workers gets `unreadable(task, CRASHED)`; one whose reading raised,
    `unreadable(task, <error>)`; one past `page_limit_s`, `unreadable(task, "it took longer than
    ...")`. `keep`: the folder finished pages are kept in (and read back from, so a run resumes);
    `finished`: whether a result may be kept (else it is read again next time)."""
    table = table if table is not None else Table(tasks)

    def done(index: int, result: Any) -> None:
        on_result(index, result)
        table.set(index, *_state_of(result))

    digests = {pdf: _digest(pdf) for pdf in {task.pdf for task in tasks}}
    keys = [_key(task, version, digests) for task in tasks]
    todo: list[int] = []
    for index, key in enumerate(keys):
        kept = _kept(keep, key)
        if kept is not None:
            done(index, kept)
        else:
            todo.append(index)
    count = workers() if count is None else max(1, count)
    if count <= 1 or len(todo) <= 1:
        for index in todo:
            table.set(index, READING)
            try:
                result = read_page(tasks[index], limits)
            except Exception as failed:  # noqa: BLE001 - the page says why; the rest is read
                result = unreadable(tasks[index], type(failed).__name__)
            else:
                _keep(keep, keys[index], result, finished)
            done(index, result)
        return
    _Pool(
        tasks,
        keys,
        dict(limits),
        f"{read_page.__module__}:{read_page.__qualname__}",
        done,
        unreadable,
        min(count, len(todo)),
        keep,
        table,
        finished,
        page_limit_s,
    ).run(todo)


def _serve(connection: Connection, reader: str) -> None:
    """A worker: reads the pages it is sent with `reader` (`module:function`), one at a time,
    until it is sent `None`."""
    os.environ.update(_WORKER_ENVIRONMENT)
    try:
        os.nice(10)
    except OSError:  # a platform without it: read at normal priority
        pass
    from importlib import import_module

    module, name = reader.split(":")
    read_page = getattr(import_module(module), name)
    while True:
        try:
            message = connection.recv()
        except EOFError:
            return
        if message is None:
            return
        task, limits = message
        try:
            result = read_page(task, limits)
        except Exception as failed:  # noqa: BLE001 - the page says why; the worker goes on
            connection.send(("error", type(failed).__name__))
            continue
        connection.send(("done", result))


def _rss_mb(pid: int) -> float | None:
    """A process's resident memory in MB (`ps`), `None` when it cannot be read."""
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
        return int(out) / 1024 if out.isdigit() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def free_mb() -> float | None:
    """The machine's memory free for new work, in MB (`MemAvailable` on Linux; on macOS free,
    inactive and purgeable pages), `None` when it cannot be read."""
    try:
        if sys.platform.startswith("linux"):
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
            return None
        out = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    size = 4096
    pages: dict[str, int] = {}
    for line in out.splitlines():
        if "page size of" in line:
            size = int("".join(ch for ch in line.split("page size of")[1] if ch.isdigit()) or 4096)
        elif ":" in line:
            name, _, value = line.partition(":")
            digits = value.strip().rstrip(".")
            if digits.isdigit():
                pages[name.strip()] = int(digits)
    wanted = ("Pages free", "Pages inactive", "Pages purgeable", "Pages speculative")
    return sum(pages.get(name, 0) for name in wanted) * size / (1024 * 1024)


@dataclass(slots=True)
class _Worker:
    process: Any
    connection: Connection
    task: int | None = None
    started: float = 0.0
    pages: int = 0


class _Pool:
    """The worker processes and the loop that hands them pages (`read_pages`)."""

    def __init__(
        self,
        tasks: Sequence[PageTask],
        keys: Sequence[str],
        limits: dict[str, float],
        reader: str,
        done: Callable[[int, Any], None],
        unreadable: Callable[[PageTask, str], Any],
        count: int,
        keep: Path | None,
        table: Table,
        finished: Callable[[Any], bool],
        page_limit_s: float,
    ) -> None:
        self.tasks, self.keys, self.limits, self.reader = tasks, keys, limits, reader
        self.done, self.unreadable, self.count = done, unreadable, count
        self.keep, self.table, self.finished, self.page_limit_s = (
            keep,
            table,
            finished,
            page_limit_s,
        )
        self.context = multiprocessing.get_context("spawn")
        self.queue: list[int] = []
        self.crashes: dict[int, int] = {}
        self.pool: list[_Worker] = []

    def _start(self) -> _Worker:
        parent, child = self.context.Pipe()
        process = self.context.Process(target=_serve, args=(child, self.reader), daemon=True)
        process.start()
        child.close()
        return _Worker(process, parent)

    def _stop(self, worker: _Worker, *, kill: bool = False) -> None:
        if kill:
            worker.process.kill()
        else:
            try:
                worker.connection.send(None)
            except (OSError, ValueError):
                pass
        worker.process.join(timeout=10)
        if worker.process.is_alive():
            worker.process.kill()
            worker.process.join(timeout=5)
        worker.connection.close()

    def _replace(self, worker: _Worker, *, kill: bool = False) -> _Worker:
        self._stop(worker, kill=kill)
        fresh = self._start()
        self.pool[self.pool.index(worker)] = fresh
        return fresh

    def _give(self, worker: _Worker) -> None:
        if not self.queue:
            worker.task = None
            return
        busy = sum(1 for w in self.pool if w.task is not None)
        free = free_mb()
        if busy and free is not None and free < MIN_FREE_MB:
            worker.task = None  # the machine is short of memory: wait for a page to finish
            return
        index = self.queue.pop(0)
        worker.task, worker.started = index, time.monotonic()
        worker.pages += 1
        self.table.set(index, READING)
        worker.connection.send((self.tasks[index], self.limits))

    def _after(self, worker: _Worker) -> _Worker:
        """The worker for the next page: this one, or a fresh one when it has read enough pages
        or holds too much memory."""
        rss = _rss_mb(worker.process.pid)
        if worker.pages >= PAGES_PER_WORKER or (rss is not None and rss > WORKER_MEMORY_MB):
            return self._replace(worker)
        return worker

    def _crashed(self, worker: _Worker, index: int, why: str | None) -> None:
        task = self.tasks[index]
        _log.warning(
            "page reading: a worker stopped on page %d of %s (%s)",
            task.number,
            task.name,
            why or f"exit {worker.process.exitcode}",
        )
        fresh = self._replace(worker, kill=True)
        if why is None:
            self.crashes[index] = self.crashes.get(index, 0) + 1
            if self.crashes[index] < 2:
                self.queue.insert(0, index)  # once more, on a fresh worker
                self._give(fresh)
                return
        self.done(index, self.unreadable(task, why or CRASHED))
        self._give(fresh)

    def run(self, todo: Sequence[int]) -> None:
        self.queue = list(todo)
        self.pool = [self._start() for _ in range(self.count)]
        try:
            for worker in self.pool:
                self._give(worker)
            while self.queue or any(w.task is not None for w in self.pool):
                busy = [w for w in self.pool if w.task is not None]
                if not busy:
                    # Every worker held back for memory: start one page anyway.
                    self.pool[0].task = None
                    free = free_mb()
                    _log.warning(
                        "page reading: little memory free (%s MB); one page at a time", free
                    )
                    index = self.queue.pop(0)
                    worker = self.pool[0]
                    worker.task, worker.started = index, time.monotonic()
                    worker.pages += 1
                    self.table.set(index, READING)
                    worker.connection.send((self.tasks[index], self.limits))
                    continue
                ready = wait(
                    [w.connection for w in busy] + [w.process.sentinel for w in busy], timeout=5.0
                )
                now = time.monotonic()
                for worker in busy:
                    index = worker.task
                    if index is None:
                        continue
                    if worker.connection in ready:
                        try:
                            kind, value = worker.connection.recv()
                        except (EOFError, OSError):
                            self._crashed(worker, index, None)
                            continue
                    elif worker.process.sentinel in ready:
                        self._crashed(worker, index, None)
                        continue
                    elif now - worker.started > self.page_limit_s:
                        minutes = round(self.page_limit_s / 60)
                        self._crashed(worker, index, f"it took longer than {minutes} minutes")
                        continue
                    else:
                        continue
                    if kind == "done":
                        _keep(self.keep, self.keys[index], value, self.finished)
                        self.done(index, value)
                    else:
                        self.done(index, self.unreadable(self.tasks[index], str(value)))
                    worker.task = None
                    self._give(self._after(worker))
                # Workers held back for memory start again when there is room.
                for worker in self.pool:
                    if worker.task is None and self.queue:
                        self._give(worker)
        finally:
            for worker in self.pool:
                self._stop(worker)
