"""Code's spot reads in worker processes (`spot_pool`, 2026-10-23). No model, no PDF.

- With no workers the read runs on the caller's thread, as before; with workers it runs in a
  worker process and its answer, or the exception it raised, comes back the same.
- The same page (affinity) meets the same worker, so its parsed page is kept there.
- Workers that break never fail the check: every read from then on runs on the caller's thread.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from app.review.assistant import spot_pool

PARENT = "GV_SPOT_POOL_TEST_PARENT"


def die_outside_the_test() -> str:
    """Kills the worker process it runs in; on the test's own process it answers."""
    if str(os.getpid()) != os.environ.get(PARENT):
        os._exit(3)
    return "read here"


@pytest.fixture
def workers() -> Iterator[None]:
    os.environ[PARENT] = str(os.getpid())
    spot_pool.use_processes(2)
    try:
        yield
    finally:
        spot_pool.use_processes(0)
        os.environ.pop(PARENT, None)


def test_with_no_workers_a_read_runs_here_as_before() -> None:
    spot_pool.use_processes(0)
    assert spot_pool.processes() == 0
    assert spot_pool.run("os", "getpid") == os.getpid()
    with pytest.raises(ValueError):
        spot_pool.run("math", "sqrt", -1.0)


@pytest.mark.usefixtures("workers")
def test_a_read_runs_in_a_worker_and_answers_or_raises_the_same() -> None:
    assert spot_pool.processes() == 2
    assert spot_pool.run("operator", "add", 2, 3, affinity=("a.pdf", 1)) == 5
    worker = spot_pool.run("os", "getpid", affinity=("a.pdf", 1))
    assert worker != os.getpid()
    # The same page goes to the same worker every time.
    assert all(spot_pool.run("os", "getpid", affinity=("a.pdf", 1)) == worker for _ in range(5))
    with pytest.raises(ValueError):
        spot_pool.run("math", "sqrt", -1.0, affinity=("a.pdf", 2))
    # Each worker runs its engines with one thread.
    assert spot_pool.run("os", "getenv", "OMP_THREAD_LIMIT") == "1"


@pytest.mark.usefixtures("workers")
def test_workers_that_break_never_fail_the_check() -> None:
    found = spot_pool.run(__name__, "die_outside_the_test", affinity="p1")
    assert found == "read here"  # the worker died: read on this thread instead
    assert spot_pool.processes() == 0
    assert spot_pool.run("operator", "add", 1, 1) == 2
