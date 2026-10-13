"""A reading that stops cleanly at its first refusal (#1206).

**The problem.** Eight questions are in flight at once, and a finished one frees its spend
reservation. So when the Claude readers' budget ran out, the refusals landed scattered across the
pages: a question refused here, the next one asked and answered, another refused. Each refusal
became a quiet "no reader answer" hold, and the budget was never named.

**What this does.** A `ReadingStop` is shared by every batch of one reading. The first question
refused for the budget (`SpendCapExceeded`) or by the provider for the account
(`ProviderAccountRefused`, HTTP 402), or a paid answer that took the spend past the budget, trips
it; from then on no question is sent. The questions in flight finish normally. Every question
refused or never sent is noted with its page and what kind of question it was, so the stage can
tell a page whose own reading is missing (listed as unread) from a page read in full whose later
questions were not asked (kept, with a hold that names the budget).

Thread-safe: the reader threads trip it and note questions; the stage reads it after the batch.
Pure apart from its lock. Verification: `tests/extraction/slot_reader/test_reading_stop.py`.
"""

from __future__ import annotations

import threading
from typing import Final, Literal

__all__ = ["CHAIN_KEY_MARK", "ReadingStop", "StopCause", "question_kind"]

#: Why a reading stopped: its AI budget was reached, or the provider refused the account (402).
type StopCause = Literal["budget", "account"]

#: The mark of a question about the `X"(N EQ)` chain read beside the chosen row (#1086), in its key
#: (`workflow/slot_reader._CHAIN_ROW_KEY`).
CHAIN_KEY_MARK: Final = "eq-chain:"

#: The page's own reading: which row is the countertop, and the dimensions printed on it. A page
#: missing any of these is not read; every other kind is a later question about a page read.
OWN_READING: Final = frozenset({"row", "dimension"})


def question_kind(job: object) -> str:
    """What a reader question (`CropJob`) asks, in words: `row`, `dimension`, `walls`,
    `stone ends`, `equal-share chain`, `architect pairing` or `architect match`."""
    if getattr(job, "row_question", False):
        return "row"
    if getattr(job, "wall_question", False):
        return "walls"
    if getattr(job, "counter_break_question", False):
        return "stone ends"
    if getattr(job, "arch_pair_question", False):
        return "architect pairing"
    if getattr(job, "arch_match_question", False):
        return "architect match"
    if CHAIN_KEY_MARK in str(getattr(job, "key", "")):
        return "equal-share chain"
    return "dimension"


class ReadingStop:
    """Tripped by the first refusal; then no further question is sent."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cause: StopCause | None = None
        self._refused: set[tuple[str, str, int, str]] = set()
        self._unasked: set[tuple[str, str, int, str]] = set()

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._cause is not None

    @property
    def cause(self) -> StopCause | None:
        """The first refusal's cause; `None` while the reading has not stopped."""
        with self._lock:
            return self._cause

    def trip(self, cause: StopCause) -> None:
        """Stop the reading with no question refused: a paid answer took the spend past the
        budget, so the next question could not be afforded."""
        with self._lock:
            if self._cause is None:
                self._cause = cause

    def refused(self, job: object, cause: StopCause) -> None:
        """A question was refused (the budget, or the account): the reading stops here."""
        with self._lock:
            if self._cause is None:
                self._cause = cause
            self._refused.add(_entry(job))

    def not_asked(self, job: object) -> None:
        """A question was not sent because the reading had already stopped."""
        with self._lock:
            self._unasked.add(_entry(job))

    @property
    def refused_count(self) -> int:
        with self._lock:
            return len(self._refused)

    @property
    def unasked_count(self) -> int:
        with self._lock:
            return len(self._unasked)

    def pages_left(self) -> frozenset[int]:
        """Every page (by index) whose own reading (row or dimension question) was refused or
        never sent: not read."""
        with self._lock:
            return frozenset(
                page
                for _key, _model, page, kind in self._refused | self._unasked
                if kind in OWN_READING
            )

    def later_gaps(self) -> dict[int, frozenset[str]]:
        """For each page, the kinds of later question (walls, stone ends, chain, architect)
        refused or never sent."""
        with self._lock:
            gaps: dict[int, set[str]] = {}
            for _key, _model, page, kind in self._refused | self._unasked:
                if kind not in OWN_READING:
                    gaps.setdefault(page, set()).add(kind)
            return {page: frozenset(kinds) for page, kinds in gaps.items()}


def _entry(job: object) -> tuple[str, str, int, str]:
    return (
        str(getattr(job, "key", "")),
        str(getattr(job, "model_id", "")),
        int(getattr(job, "page_index", 0)),
        question_kind(job),
    )
