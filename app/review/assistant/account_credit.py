"""The AI account's credit, held at each running call's worst case (2026-10-29).

OpenRouter refuses a call (402) when the balance cannot cover the worst cases of the calls running.
The La Quinta run of 2026-10-29 started its 9 page reviewers and the helpers together with about
$16 of credit and was refused at $5.45 spent: every call of the answer stopped, the check wasted.
GV's start rule (`plan_supervisor.CREDIT_SPARE_MICROS`: the fixed price and $1 spare) cannot see
calls running at once.

- **One per process** (`ACCOUNT_CREDIT`): every answer the worker runs spends the same account.
- **The balance** is read (OpenRouter's free, read-only credit check) at an answer's first call and
  again when the last read is older than `READ_EVERY_S`, so a top-up is seen while answers overlap.
  What a running call will cost is not in the balance yet; what each call cost is taken off when it
  ends, until the next read. A read that fails, or a provider with no balance to read (Anthropic's
  own API, a test's fake), holds nothing: calls run as before.
- **Each call** (`ChatEngine._call`, once it has its place in the model's limiter) is held at its
  worst case (no cache, its whole output allowance) until it ends. A call that does not fit next to
  the calls running now (`SPARE_MICROS` kept free) waits for one to end; one that would not fit
  even alone goes ahead (the provider then says so, as before).
- **OpenRouter's "can only afford" 402** (`chat_provider.failure_from`): a busy answer while other
  calls are running (their worst cases were in the way), the account out of credit when none is.
"""

from __future__ import annotations

import logging
import threading
from decimal import Decimal
from time import monotonic
from typing import Any, Final

__all__ = ["ACCOUNT_CREDIT", "READ_EVERY_S", "SPARE_MICROS", "AccountCredit"]

_log = logging.getLogger(__name__)

#: Kept free of the balance while calls run (millionths of $): the balance trails the spend.
SPARE_MICROS: Final = 1_000_000
#: The balance is read again at a call's start when the last read is older than this (seconds).
READ_EVERY_S: Final = 60.0
#: A waiting call looks again at least this often, and goes ahead after the longest wait (s).
_WAIT_S: Final = 2.0
_WAIT_MOST_S: Final = 900.0


class AccountCredit:
    """The balance and the worst cases of the calls running now (see the module)."""

    def __init__(self) -> None:
        self._changed = threading.Condition()
        self._left: int | None = None
        self._held = 0
        self._running = 0
        self._read_at: float | None = None
        self._reading = False

    def start(self, provider: Any, *, force: bool = False) -> None:
        """Read the balance when it was never read, or the last read is older than
        `READ_EVERY_S` (`force`: an answer's first call). One thread reads at a time."""
        check = getattr(provider, "credit", None)
        with self._changed:
            if not callable(check):
                if self._running == 0:
                    self._left = None  # no balance to read: never held
                return
            fresh = self._read_at is not None and monotonic() - self._read_at < READ_EVERY_S
            if self._reading or (fresh and not force):
                return
            self._reading = True
        left: int | None = None
        try:
            credit = check()
            usd = getattr(credit, "usd", None)
            if getattr(credit, "state", None) == "ok" and usd is not None:
                left = int(Decimal(str(usd)) * 1_000_000)
        except Exception:  # noqa: BLE001 - no balance read: calls run as before
            _log.warning("assistant chat: the AI account's credit could not be read")
        with self._changed:
            self._reading = False
            self._read_at = monotonic()
            self._left = left
            self._changed.notify_all()

    def hold(self, worst: int | None, cancel: threading.Event) -> int:
        """Hold `worst` (a call's worst case) once it fits next to the calls running now; every
        call counts as running until `done`. Returns what is held (0: no balance known)."""
        deadline = monotonic() + _WAIT_MOST_S
        amount = max(0, int(worst or 0))
        with self._changed:
            waited = False
            while (
                self._left is not None
                and self._held > 0
                and self._held + amount > self._left - SPARE_MICROS
                and not cancel.is_set()
                and monotonic() < deadline
            ):
                if not waited:
                    waited = True
                    _log.warning(
                        "assistant chat: a call waits for the account's credit (left $%.2f, "
                        "held $%.2f, this call up to $%.2f)",
                        self._left / 1_000_000,
                        self._held / 1_000_000,
                        amount / 1_000_000,
                    )
                self._changed.wait(_WAIT_S)
            held = amount if self._left is not None else 0
            self._held += held
            self._running += 1
            return held

    def done(self, held: int, cost: int) -> None:
        """A call ended: its hold let go, what it cost taken off the balance."""
        with self._changed:
            self._held = max(0, self._held - held)
            self._running = max(0, self._running - 1)
            if self._left is not None:
                self._left -= max(0, cost)
            self._changed.notify_all()

    def others_running(self) -> bool:
        """Whether a call other than the one asking is running now (the asking one is still
        counted: its `done` comes after its failure is read)."""
        with self._changed:
            return self._running > 1


ACCOUNT_CREDIT: Final = AccountCredit()
