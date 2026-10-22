"""The set pack's labels read a page at a time in worker processes (#1871).

**Why.** A 188-page upload (2026-10-22) ran out of the labels step's 40 minutes before the drawn
pass even started: the labels' process read every page one after another (text, pasted pictures,
then the drawn ink). This reads pages at once in a few worker processes instead:

- **Each worker is a process of its own**, with its own PDF engine: two pages read at once can
  never meet inside pdfium (it is not thread-safe), and a page that crashes the engine kills only
  its worker. That page is read once more by a fresh worker; if it crashes that one too, the page
  says "page N could not be read: the PDF reader crashed" and every other page is used.
- **The set's time budgets are shared as before** (pictures, drawn ink, OCR, sweep): each page is
  given what is left when it starts and gives back what it used, so the set does the same work in
  less time (the budgets are worker seconds; the wall time is about that over the workers).
- **Each page is kept as it finishes** (`PAGES_FOLDER`, by the page's PDF bytes, its sides and the
  labels' version): a labels step stopped by its time limit, or a pack built again, resumes with
  the pages already read. A page the drawn pass did not finish (`not_run`, `partial`) is read again.
- **How many workers:** `WORKERS_SETTING`, else min(`MOST_WORKERS`, cores - 2), at least 1 (one:
  every page read in this process, in order, as tests do).
"""

from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing
import os
import pickle
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
#: Why a page that crashed two workers has no labels.
CRASHED: Final = "the PDF reader crashed"
#: A page's drawn pass is finished unless it says one of these (it is then read again on resume).
_UNFINISHED: Final = frozenset({"not_run", "partial"})


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
    pictures, whether it shows a pasted picture, and whether its unread ink is counted."""

    name: str
    pdf: str
    number: int
    halves: tuple[Any, ...] = ()
    pasted: tuple[Any, ...] = ()
    with_pictures: bool = False
    count_ink: bool = False
    health: tuple[tuple[str, Any], ...] | None = None
    """With the page's health check (`extraction/page_health`): its upload's listing entry."""
    upload: str = ""
    """The upload itself (the health check reads the original, not the drawings-only copy)."""


@dataclass(slots=True)
class Budgets:
    """The set's seconds left for each pass, shared by every page (worker seconds)."""

    pictures: float
    drawn: float
    ocr: float
    sweep: float

    def take(self, spent: Mapping[str, float]) -> None:
        for name in ("pictures", "drawn", "ocr", "sweep"):
            setattr(self, name, getattr(self, name) - float(spent.get(name, 0.0)))

    def given(self) -> dict[str, float]:
        return {
            "pictures": max(0.0, self.pictures),
            "drawn": max(0.0, self.drawn),
            "ocr": max(0.0, self.ocr),
            "sweep": max(0.0, self.sweep),
        }


@dataclass(slots=True)
class _Worker:
    process: Any
    connection: Connection
    task: int | None = None


def _key(task: PageTask, version: str, digests: Mapping[str, str]) -> str:
    text = json.dumps(
        [
            version,
            digests.get(task.pdf, ""),
            task.number,
            task.halves,
            task.pasted,
            task.with_pictures,
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


def _keep(folder: Path | None, key: str, result: Any) -> None:
    if folder is None or getattr(result, "entry", {}).get("drawn") in _UNFINISHED:
        return
    try:
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / f".{key}.{uuid4().hex[:8]}.tmp"
        temporary.write_bytes(pickle.dumps(result))
        os.replace(temporary, folder / f"{key}.pkl")
    except Exception as failed:  # noqa: BLE001 - only a resume reads it again
        _log.warning("dimension labels: page not kept: %s", type(failed).__name__)


def _serve(connection: Connection, reader: str) -> None:
    """A worker: reads the pages it is sent with `reader` (`module:function`), one at a time,
    until it is sent `None`."""
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
        task, given = message
        try:
            result = read_page(task, given)
        except Exception as failed:  # noqa: BLE001 - the page says why; the worker goes on
            connection.send(("error", type(failed).__name__))
            continue
        connection.send(("done", result))


#: A page's states in the reading table (`Table`): not started, being read, read, could not be read.
WAITING: Final = "waiting"
READING: Final = "reading"
READY: Final = "ready"
FAILED: Final = "failed"


class Table:
    """The reading's status table: each page's state (`WAITING`, `READING`, `READY`, `FAILED`
    with why), published whole (`on_change`) each time one changes. The set pack writes it as
    `reading.json`; a full check starts on a page only when it is ready (`page_states`)."""

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
                _log.warning("dimension labels: reading table not written: %s", failed)

    def snapshot(self) -> dict[str, Any]:
        states = [row["state"] for row in self._rows]
        return {
            "total": len(states),
            "ready": states.count(READY),
            "failed": states.count(FAILED),
            "pages": [dict(row) for row in self._rows],
        }


def _state_of(result: Any) -> tuple[str, str | None]:
    entry = getattr(result, "entry", None) or {}
    error = entry.get("error")
    return (FAILED, str(error)) if error else (READY, None)


def read_pages(
    tasks: Sequence[PageTask],
    budgets: Budgets,
    read_page: Callable[[PageTask, Mapping[str, float]], Any],
    on_result: Callable[[int, Any], None],
    *,
    unreadable: Callable[[PageTask, str], Any],
    count: int | None = None,
    keep: Path | None = None,
    version: str = "",
    table: Table | None = None,
) -> None:
    """Read every task's page; `on_result(index, result)` as each one finishes (any order). A
    page that crashed two workers gets `unreadable(task, CRASHED)` as its result; a page whose
    reading raised gets `unreadable(task, <error>)`. `keep`: the folder the finished pages are
    kept in (and read back from, so a run resumes)."""
    table = table if table is not None else Table(tasks)
    told = on_result

    def on_result(index: int, result: Any) -> None:
        told(index, result)
        table.set(index, *_state_of(result))

    digests = {pdf: _digest(pdf) for pdf in {task.pdf for task in tasks}}
    keys = [_key(task, version, digests) for task in tasks]
    todo: list[int] = []
    for index, key in enumerate(keys):
        kept = _kept(keep, key)
        if kept is not None:
            on_result(index, kept)
        else:
            todo.append(index)
    count = workers() if count is None else max(1, count)
    if count <= 1 or len(todo) <= 1:
        for index in todo:
            table.set(index, READING)
            try:
                result = read_page(tasks[index], budgets.given())
            except Exception as failed:  # noqa: BLE001 - the page says why; the rest is read
                result = unreadable(tasks[index], type(failed).__name__)
            budgets.take(getattr(result, "spent", {}))
            _keep(keep, keys[index], result)
            on_result(index, result)
        return
    reader = f"{read_page.__module__}:{read_page.__qualname__}"
    _pool(
        tasks,
        todo,
        keys,
        budgets,
        on_result,
        unreadable,
        min(count, len(todo)),
        keep,
        reader,
        table,
    )


def _start(context: Any, reader: str) -> _Worker:
    parent, child = context.Pipe()
    process = context.Process(target=_serve, args=(child, reader), daemon=True)
    process.start()
    child.close()
    return _Worker(process, parent)


def _pool(
    tasks: Sequence[PageTask],
    todo: Sequence[int],
    keys: Sequence[str],
    budgets: Budgets,
    on_result: Callable[[int, Any], None],
    unreadable: Callable[[PageTask, str], Any],
    count: int,
    keep: Path | None,
    reader: str,
    table: Table,
) -> None:
    context = multiprocessing.get_context("spawn")
    queue = list(todo)
    crashes: dict[int, int] = {}
    pool = [_start(context, reader) for _ in range(count)]

    def give(worker: _Worker) -> None:
        if not queue:
            worker.task = None
            return
        index = queue.pop(0)
        worker.task = index
        table.set(index, READING)
        worker.connection.send((tasks[index], budgets.given()))

    try:
        for worker in pool:
            give(worker)
        while any(worker.task is not None for worker in pool):
            busy = [worker for worker in pool if worker.task is not None]
            ready = wait(
                [worker.connection for worker in busy]
                + [worker.process.sentinel for worker in busy]
            )
            for worker in busy:
                index = worker.task
                if index is None:
                    continue
                if worker.connection in ready:
                    try:
                        kind, value = worker.connection.recv()
                    except (EOFError, OSError):
                        kind, value = "crashed", None
                elif worker.process.sentinel in ready:
                    kind, value = "crashed", None
                else:
                    continue
                if kind == "crashed":
                    worker.connection.close()
                    worker.process.join(timeout=5)
                    crashes[index] = crashes.get(index, 0) + 1
                    _log.warning(
                        "dimension labels: a worker stopped on page %d of %s (exit %s)",
                        tasks[index].number,
                        tasks[index].name,
                        worker.process.exitcode,
                    )
                    replacement = _start(context, reader)
                    pool[pool.index(worker)] = replacement
                    if crashes[index] < 2:
                        queue.insert(0, index)  # once more, on a fresh worker
                        give(replacement)
                        continue
                    result = unreadable(tasks[index], CRASHED)
                    on_result(index, result)
                    give(replacement)
                    continue
                result = value if kind == "done" else unreadable(tasks[index], str(value))
                budgets.take(getattr(result, "spent", {}))
                if kind == "done":
                    _keep(keep, keys[index], result)
                on_result(index, result)
                give(worker)
    finally:
        for worker in pool:
            try:
                worker.connection.send(None)
            except (OSError, ValueError):
                pass
        for worker in pool:
            worker.process.join(timeout=10)
            if worker.process.is_alive():
                worker.process.kill()
