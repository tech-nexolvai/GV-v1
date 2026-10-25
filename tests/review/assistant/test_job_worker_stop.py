"""The worker's stop signal never hangs it (a SIGTERM that arrived while its loop held the stop
event's lock left a worker that ignored every later SIGTERM)."""

from __future__ import annotations

import signal
import threading

from app.review.assistant import job_worker


def test_a_stop_signal_while_the_loop_holds_the_events_lock_still_stops_the_worker() -> None:
    stopping = threading.Event()
    on_signal = job_worker.stop_handler(stopping)
    returned = threading.Event()

    def loop() -> None:
        # The main loop inside `stopping.wait()`: the handler runs on that same thread.
        with stopping._cond:  # type: ignore[attr-defined]
            on_signal(signal.SIGTERM, None)
        returned.set()

    threading.Thread(target=loop, daemon=True).start()
    assert returned.wait(5), "the handler waited for the lock its own thread holds"
    assert stopping.wait(5)


def test_a_stop_signal_sets_the_event() -> None:
    stopping = threading.Event()
    job_worker.stop_handler(stopping)(signal.SIGINT, None)
    assert stopping.wait(5)
