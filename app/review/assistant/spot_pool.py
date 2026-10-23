"""Code's own spot reads in worker processes (2026-10-23), so a big set's final checks use the cores.

A full check's final checks by code read many spots again: the proving step's `verify_value` and
`read_number` on both values of every red candidate, the finish's drawn-ink pass on every value its
text route left pending. Each read is plain Python (the vendor's ink cut into strokes, fractions
found, pictures drawn) and OCR (RapidOCR and Tesseract). On code's thread pool (`work_pool`) they
waited for each other: Python holds one lock, and each OCR engine is one instance used one call at
a time. A 99-page set (2026-10-23) spent over an hour of its "final checks by code"
on one core. Here each read runs in a worker process of its own:

- **The same answer.** A worker runs the very function the thread ran (`run(module, name, ...)`
  calls `module.name(*args)`), with the same arguments; nothing about the read changes. Its
  answer, or the exception it raised, comes back to the caller as if it had been called here.
- **Started by the same loops.** The callers keep their own thread pools (`work_pool.run_ordered`:
  items started in order, a stop or a time budget looked at as each starts); each thread hands its
  read to a worker and waits. A stop therefore lets the reads already running end (seconds) and
  starts no other.
- **The same page meets the same worker** (`affinity`): a worker keeps the pages it parsed
  (`verify_value`'s page cache), so a page is not parsed again by every worker.
- **Gentle on a shared machine.** At most `use_processes(n)` workers (the server's
  `assistant_spot_processes`; 0: on the caller's thread, as before), each with one thread for
  Tesseract, OpenMP and ONNX Runtime (`_ENVIRONMENT`, RapidOCR's sessions set to one thread), and
  replaced after `TASKS_PER_WORKER` reads (the memory a long-lived reader keeps is given back).
- **Never a failed check.** Workers that cannot start or that break (a crash, a killed process)
  are given up and every read from then on runs on the caller's thread, said once in the log.

pdfium is not thread-safe and is used inside each process under its own lock, as before
(`chat_drawing.PDFIUM_LOCK`); two processes never share a PDF object.

Verification: `tests/review/assistant/test_spot_pool.py`.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import threading
from collections.abc import Callable, Hashable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from importlib import import_module
from pathlib import Path
from typing import Any, Final

__all__ = [
    "MOST_WORKERS",
    "TASKS_PER_WORKER",
    "processes",
    "run",
    "use_processes",
    "verify_answer_at",
]

_log = logging.getLogger(__name__)

#: The most workers, whatever the setting asks: the machine runs the API, the database and the
#: page readers too.
MOST_WORKERS: Final = 8
#: A worker is replaced after this many reads.
TASKS_PER_WORKER: Final = 400
#: Each worker's environment, set before it reads anything: one thread per engine.
_ENVIRONMENT: Final = {
    "OMP_THREAD_LIMIT": "1",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
}

_LOCK: Final = threading.Lock()
_WORKERS = 0
_POOLS: list[ProcessPoolExecutor] = []
_BROKEN = False


def use_processes(workers: int) -> None:
    """Spot reads in `workers` worker processes from now on (0: on the caller's thread). Workers
    of another number are shut down (their reads end first)."""
    global _WORKERS, _POOLS, _BROKEN
    workers = max(0, min(MOST_WORKERS, int(workers)))
    with _LOCK:
        if workers == _WORKERS and not _BROKEN:
            return
        _WORKERS, old, _POOLS, _BROKEN = workers, _POOLS, [], False
    for pool in old:
        pool.shutdown(wait=True)


def processes() -> int:
    """How many worker processes the spot reads use now (0: the caller's thread)."""
    with _LOCK:
        return 0 if _BROKEN else _WORKERS


def run(module: str, name: str, *args: Any, affinity: Hashable = None) -> Any:
    """`module.name(*args)` in a worker process (the one `affinity` names, so the same page meets
    the same worker), or on this thread when no workers are set or they broke. Returns what it
    returns; raises what it raises."""
    pool = _pool(affinity)
    if pool is not None:
        kept = import_module("app.review.assistant.verify_value").kept_folders()
        try:
            future = pool.submit(_call, module, name, args, kept)
        except BrokenProcessPool as broken:
            _give_up(broken)
        except RuntimeError:
            pass  # the workers were just shut down (the check ended): read here
        else:
            try:
                return future.result()
            except BrokenProcessPool as broken:
                _give_up(broken)
    return _call(module, name, args)


def _call(
    module: str, name: str, args: tuple[Any, ...], kept: Mapping[str, Path] | None = None
) -> Any:
    if kept:
        # In a worker: the reads are kept where the check keeps them (`verify_value`).
        keep = import_module("app.review.assistant.verify_value").keep_reads_in
        for pdf, folder in kept.items():
            keep(folder, [Path(pdf)])
    function: Callable[..., Any] = getattr(import_module(module), name)
    return function(*args)


def verify_answer_at(
    uploads: Path,
    pack: Path | None,
    name: str,
    page: int,
    box: Sequence[float],
    expected: str,
    side: str = "vendor",
) -> dict[str, Any]:
    """The proving tools' read of one spot (`ChatTools.verify_answer`, here so a worker process
    runs it without the chat's tools): `verify_value` at the spot (the drawing's text, else its
    drawn ink), and a spot it cannot read read again by `read_number` (#1746), whose answer
    replaces it only when it reads the number. Raises when the spot cannot be read at all."""
    module = import_module("app.review.assistant.verify_value")
    found = dict(
        module.verify_value(
            uploads / name,
            page,
            dict(zip(("x0", "y0", "x1", "y1"), box, strict=True)),
            expected,
            page_cache=None if pack is None else pack / "hires",
        ).as_dict()
    )
    if found["result"] == "cannot_read":
        try:
            numbers = import_module("app.review.assistant.read_number")
            request = numbers.Request(name, page, side, box=tuple(box), claim=expected)
            again = numbers.reader_for_pack(pack, uploads)(request)
            found["read_again"] = True
            if again.outcome == "read":
                found = {**again.as_verify(expected), "read_again": True}
            else:
                # Its machine reason too (#1805): a "readers disagree" or "cut off" is never
                # overridden by an older proof.
                found["read_number_code"] = again.code
                if again.reason:
                    found["words"] = f"{found['words']}; read_number: {again.reason}"
        except Exception as failed:  # noqa: BLE001 - the first answer stands
            _log.warning("assistant chat zoom failed: %s", type(failed).__name__)
    return found


def _pool(affinity: Hashable) -> ProcessPoolExecutor | None:
    global _POOLS
    with _LOCK:
        if _WORKERS <= 0 or _BROKEN:
            return None
        if not _POOLS:
            context = multiprocessing.get_context("spawn")
            try:
                _POOLS = [
                    ProcessPoolExecutor(
                        max_workers=1,
                        mp_context=context,
                        initializer=_start_worker,
                        initargs=(dict(_ENVIRONMENT),),
                        max_tasks_per_child=TASKS_PER_WORKER,
                    )
                    for _ in range(_WORKERS)
                ]
            except (OSError, ValueError) as failed:
                _log.warning("spot reads: no worker processes, reading here: %s", failed)
                _POOLS = []
                return None
        if affinity is None:
            _TURN[0] = (_TURN[0] + 1) % len(_POOLS)
            return _POOLS[_TURN[0]]
        return _POOLS[hash(affinity) % len(_POOLS)]


#: The worker a read with no affinity goes to next (in turn).
_TURN: Final[list[int]] = [0]


def _give_up(why: BaseException) -> None:
    """The workers broke: every read from now on runs on the caller's thread."""
    global _BROKEN, _POOLS
    with _LOCK:
        if _BROKEN:
            return
        _BROKEN, old, _POOLS = True, _POOLS, []
    _log.warning("spot reads: the worker processes stopped, reading here from now on: %s", why)
    for pool in old:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001 - a broken pool is left behind
            _log.debug("spot reads: a broken pool did not shut down")


def _start_worker(environment: dict[str, str]) -> None:
    """A worker's first step: one thread per engine, RapidOCR's sessions included."""
    os.environ.update(environment)
    try:
        rapid = import_module("rapidocr_onnxruntime")
    except ImportError:
        return
    made: Any = getattr(rapid, "RapidOCR", None)
    if made is None:
        return

    class OneThread(made):  # type: ignore[misc]
        def __init__(self, config_path: str | None = None, **kwargs: Any) -> None:
            kwargs.setdefault("intra_op_num_threads", 1)
            kwargs.setdefault("inter_op_num_threads", 1)
            super().__init__(config_path, **kwargs)

    setattr(rapid, "RapidOCR", OneThread)  # noqa: B010 - the package's own name, replaced here
