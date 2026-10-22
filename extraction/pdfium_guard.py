"""The one door to pdfium: every call into the PDF engine holds one process-wide lock.

**Why.** pdfium (the PDF engine behind `pypdfium2`) is not thread-safe: two threads inside it at
once, even on two different files, share its font and glyph caches and its memory, and the process
dies with heap corruption and no Python traceback. That is how the live API died on 2026-10-22
while a 188-page upload was being checked: the plan check's request copied the set's PDFs through
the front door (`extraction/working_copy.py`, which renders every page twice to prove the copy
looks the same) while the new chat's message thread did the same, and the front door held no lock
(only some of `app/review/assistant/` held `chat_drawing.PDFIUM_LOCK`).

**How.** Importing this module wraps every pdfium function of `pypdfium2.raw` (the ctypes bindings
`pypdfium2`'s own classes call, looked up at call time) so that each call holds `PDFIUM_LOCK`. So
every pdfium call in the process is serialised, whoever makes it: this repo's code, `pypdfium2`'s
helpers (`PdfDocument`, `page.render`, `get_textpage`, ...) and any library that calls
`pypdfium2` for us. The lock is re-entrant, so the coarser `with PDFIUM_LOCK:` blocks the
assistant's code holds (a page opened, rendered and closed as one step) keep working and keep
other threads out for the whole step.

**Objects closed by the garbage collector** (a bitmap or page nobody closed) are closed by
`pypdfium2` from whatever thread happens to collect them, maybe one holding another lock (a log
handler's, say). Waiting there for `PDFIUM_LOCK` could deadlock against a thread that holds it and
wants that other lock, so such a close never waits: its Python bookkeeping is done at once and its
pdfium call is queued, then made under the lock by the next pdfium call of any thread (in order, so
a page is always closed before its document). An explicit `close()` holds the lock as usual.

**The rule** (`tests/test_pdfium_guard.py`): no module imports `pypdfium2` but this one; code
writes `from extraction.pdfium_guard import pdfium` (and `pdfium_raw` for the raw bindings). The
wrapping is installed before any pdfium object exists, so a close function captured when an object
is made is the wrapped one too.

Cost: a lock taken per call, well under a microsecond; the work inside pdfium was never safe to run
in parallel, so nothing that was correct gets slower.
"""

from __future__ import annotations

import atexit
import ctypes
import functools
import logging
import threading
from collections import deque
from collections.abc import Callable
from typing import Any, Final

import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pypdfium2.internal.bases as _bases  # type: ignore[import-untyped]
import pypdfium2.raw as pdfium_raw  # type: ignore[import-untyped]
import pypdfium2_raw.bindings as _bindings  # type: ignore[import-untyped]

__all__ = ["PDFIUM_LOCK", "guarded", "pdfium", "pdfium_raw", "queued_closes", "wrapped_functions"]

_log = logging.getLogger(__name__)

#: The one lock every pdfium call in this process holds (re-entrant: a thread that holds it for a
#: whole step may call pdfium inside it).
PDFIUM_LOCK: Final = threading.RLock()

#: The type of a C function of a loaded library (ctypes keeps it private).
_C_FUNCTION: Final[type] = ctypes._CFuncPtr  # type: ignore[attr-defined]
#: Marks a function this module wrapped.
_MARK: Final = "__gv_pdfium_guarded__"

#: Closes the garbage collector asked for, waiting for the lock: (close function, raw handle, its
#: arguments, the owner kept alive until then: it holds the parent, so a document outlives its
#: queued pages).
_QUEUED: Final[deque[tuple[Callable[..., Any], Any, tuple[Any, ...], dict[str, Any], Any]]] = (
    deque()
)
_DRAINING = False
#: How many pdfium calls are running (only the lock's holder can be inside one). A queued close is
#: never made from inside one (a callback pdfium makes into Python may let the collector run).
_DEPTH = 0

_ORIGINAL_CLOSE: Final = _bases._close_template


def guarded(function: object) -> bool:
    """Whether `function` is a pdfium function wrapped by this module."""
    return bool(getattr(function, _MARK, False))


def queued_closes() -> int:
    """How many collected objects wait to be closed (for the tests)."""
    return len(_QUEUED)


def _drain() -> None:
    """Make the queued closes, oldest first. The caller holds `PDFIUM_LOCK`."""
    global _DRAINING
    if _DRAINING or _DEPTH:
        return
    _DRAINING = True
    try:
        while _QUEUED:
            close, raw, args, kwargs, _owner = _QUEUED.popleft()
            try:
                close(raw, *args, **kwargs)
            except Exception:  # noqa: BLE001 - one object's close never stops another's
                _log.exception("pdfium: a collected object could not be closed")
    finally:
        _DRAINING = False


def _wrap(name: str, function: Any) -> Callable[..., Any]:
    @functools.wraps(function, assigned=("__name__", "__doc__"), updated=())
    def call(*args: Any) -> Any:
        global _DEPTH
        with PDFIUM_LOCK:
            if _QUEUED:
                _drain()
            _DEPTH += 1
            try:
                return function(*args)
            finally:
                _DEPTH -= 1

    setattr(call, _MARK, True)
    call.__wrapped__ = function
    call.__qualname__ = name
    return call


def _close_template(info: Any, owner: Any) -> None:
    """`pypdfium2`'s close of one object, never waiting for the lock when the garbage collector
    asked for it (see the module docstring)."""
    if info.state is not _bases._STATE.AUTO or not _bases.LIBRARY_AVAILABLE:
        with PDFIUM_LOCK:
            _drain()
            _ORIGINAL_CLOSE(info, owner)
        return
    # The collector: the bookkeeping `_close_template` does, now; the pdfium call, queued.
    parent = owner.parent
    if parent is not None and info.tracked:
        parent._kids.discard(owner.wref)
    _bases.ObjectTracker[owner.type].discard(owner.wref)
    _QUEUED.append((info.close_func, owner.raw, info.args, dict(info.kwargs), owner))
    if PDFIUM_LOCK.acquire(blocking=False):
        try:
            _drain()
        finally:
            PDFIUM_LOCK.release()


def _install() -> None:
    """Wrap every pdfium function in the bindings, once: the same wrapper in `pypdfium2_raw` and in
    `pypdfium2.raw` (which copied its names from the first); and route `pypdfium2`'s closes here."""
    with PDFIUM_LOCK:
        for module in (_bindings, pdfium_raw):
            for name, value in list(vars(module).items()):
                if guarded(value) or not isinstance(value, _C_FUNCTION):
                    continue
                if isinstance(value, type):
                    continue  # a callback type, not a function of the library
                done = getattr(_bindings, name, None)
                setattr(module, name, done if guarded(done) else _wrap(name, value))
        _bases._close_template = _close_template


def _drain_at_exit() -> None:
    with PDFIUM_LOCK:
        _drain()


def wrapped_functions() -> list[str]:
    """The names of `pypdfium2.raw`'s pdfium functions, all wrapped (for the tests)."""
    return sorted(name for name, value in vars(pdfium_raw).items() if guarded(value))


_install()
# Before `pypdfium2`'s own exit handler destroys the library (handlers run last registered first).
atexit.register(_drain_at_exit)
