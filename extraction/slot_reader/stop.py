"""A reading that stops cleanly at its first refusal (#1206).

**The problem.** Eight questions are in flight at once, and a finished one frees its spend
reservation. So when the Claude readers' budget ran out, the refusals landed scattered across the
pages: a question refused here, the next one asked and answered, another refused. Each refusal
became a quiet "no reader answer" hold, and the budget was never named.

**What this does.** A `ReadingStop` is shared by every batch of one reading. The first question
refused for the budget (`SpendCapExceeded`) or by the provider for the account
(`ProviderAccountRefused`, HTTP 402) trips it; from then on no question is sent. The questions in
flight finish normally. Every question refused or never sent is noted with its page, so the stage
can list exactly the pages left unread and say why.

Thread-safe: the reader threads trip it and note questions; the stage reads it after the batch.
Pure apart from its lock. Verification: `tests/extraction/slot_reader/test_reading_stop.py`.
"""

from __future__ import annotations

import threading
from typing import Literal

__all__ = ["ReadingStop", "StopCause"]

#: Why a reading stopped: its AI budget was reached, or the provider refused the account (402).
type StopCause = Literal["budget", "account"]


class ReadingStop:
    """Tripped by the first refusal; then no further question is sent."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cause: StopCause | None = None
        self._refused: set[tuple[str, str, int]] = set()
        self._unasked: set[tuple[str, str, int]] = set()

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._cause is not None

    @property
    def cause(self) -> StopCause | None:
        """The first refusal's cause; `None` while the reading has not stopped."""
        with self._lock:
            return self._cause

    def refused(self, key: str, model_id: str, page_index: int, cause: StopCause) -> None:
        """A question was refused (the budget, or the account): the reading stops here."""
        with self._lock:
            if self._cause is None:
                self._cause = cause
            self._refused.add((key, model_id, page_index))

    def not_asked(self, key: str, model_id: str, page_index: int) -> None:
        """A question was not sent because the reading had already stopped."""
        with self._lock:
            self._unasked.add((key, model_id, page_index))

    @property
    def refused_count(self) -> int:
        with self._lock:
            return len(self._refused)

    @property
    def unasked_count(self) -> int:
        with self._lock:
            return len(self._unasked)

    def pages_left(self) -> frozenset[int]:
        """Every page (by index) with a question refused or never sent: not fully read."""
        with self._lock:
            return frozenset(page for _key, _model, page in self._refused | self._unasked)
