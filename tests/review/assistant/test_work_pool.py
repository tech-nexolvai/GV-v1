"""The bounded pool for code's $0 steps (`work_pool`): order kept, failures reported, stop stops."""

from __future__ import annotations

import contextvars
import threading
import time

import pytest

from app.review.assistant import work_pool
from app.review.assistant.work_pool import code_workers, run_ordered


def test_results_come_back_in_the_items_order_whatever_finishes_first() -> None:
    # The first items take longest, so they finish last.
    items = list(range(8))

    def work(n: int) -> int:
        time.sleep(0.02 * (8 - n))
        return n * n

    done = run_ordered(items, work, workers=4)
    assert [d.index for d in done] == items
    assert [d.value for d in done] == [n * n for n in items]
    assert all(d.ran and d.error is None for d in done)


def test_it_really_runs_items_at_once() -> None:
    running = 0
    most = 0
    lock = threading.Lock()

    def work(_n: int) -> None:
        nonlocal running, most
        with lock:
            running += 1
            most = max(most, running)
        time.sleep(0.05)
        with lock:
            running -= 1

    run_ordered(list(range(6)), work, workers=3)
    assert most == 3


def test_a_failing_item_is_reported_never_dropped() -> None:
    def work(n: int) -> int:
        if n == 2:
            raise ValueError("this spot could not be read")
        return n

    done = run_ordered(list(range(5)), work, workers=3)
    assert len(done) == 5
    assert [d.value for d in done] == [0, 1, None, 3, 4]
    assert isinstance(done[2].error, ValueError)
    assert done[2].ran


def test_one_worker_runs_on_the_callers_thread_in_order() -> None:
    seen: list[tuple[int, int]] = []

    def work(n: int) -> int:
        seen.append((n, threading.get_ident()))
        return n

    done = run_ordered([3, 1, 2], work, workers=1)
    assert [d.value for d in done] == [3, 1, 2]
    assert [n for n, _ in seen] == [3, 1, 2]
    assert {ident for _, ident in seen} == {threading.get_ident()}


def test_stop_stops_every_item_not_started() -> None:
    stop = threading.Event()
    started: list[int] = []

    def work(n: int) -> int:
        started.append(n)
        if n == 1:
            stop.set()  # the reviewer pressed Stop while item 1 ran
        time.sleep(0.05)
        return n

    begun = time.monotonic()
    done = run_ordered(list(range(40)), work, workers=2, stop=stop.is_set)
    assert time.monotonic() - begun < 1.5  # never waits for the 40 items' queue
    assert len(done) == 40
    ran = [d.index for d in done if d.ran]
    assert ran == sorted(started)
    assert len(ran) <= 4  # only what had already started ends its read
    # What did not run is a tail of the list (items start in order), each said so.
    assert ran == list(range(len(ran)))
    assert all(not d.ran and d.value is None and d.error is None for d in done[len(ran) :])


def test_stopped_before_it_begins_runs_nothing() -> None:
    done = run_ordered([1, 2, 3], lambda n: n, workers=2, stop=lambda: True)
    assert [d.ran for d in done] == [False, False, False]


def test_each_item_sees_the_callers_context() -> None:
    where: contextvars.ContextVar[str] = contextvars.ContextVar("where", default="none")
    where.set("provers")
    done = run_ordered([1, 2, 3, 4], lambda _n: where.get(), workers=2)
    assert [d.value for d in done] == ["provers"] * 4


def test_no_items_no_threads() -> None:
    assert run_ordered([], lambda n: n, workers=4) == []


def test_the_worker_count_is_modest_and_settable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(work_pool.WORKERS_SETTING, raising=False)
    monkeypatch.setattr(work_pool.os, "cpu_count", lambda: 32)
    assert code_workers() == work_pool.DEFAULT_MAX_WORKERS
    monkeypatch.setattr(work_pool.os, "cpu_count", lambda: 2)
    assert code_workers() == 1
    monkeypatch.setenv(work_pool.WORKERS_SETTING, "2")
    assert code_workers() == 2
    monkeypatch.setenv(work_pool.WORKERS_SETTING, "0")
    assert code_workers() == 1
    monkeypatch.setenv(work_pool.WORKERS_SETTING, "many")
    monkeypatch.setattr(work_pool.os, "cpu_count", lambda: 32)
    assert code_workers() == work_pool.DEFAULT_MAX_WORKERS
