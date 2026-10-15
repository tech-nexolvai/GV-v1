"""Try again, up to three smarter ways, before an item waits for a person (#1309, phase 3).

**What it is.** When the line finder's two AIs pick different lines, or its gate stops the line
they chose (#1228), or the number reader's seal holds a label because the two AIs read it
differently, were unsure or could not read it, the reading may ask both AIs again, each time
changing something, and stop at the first try that settles the item:

* the line finder (Rekha): `tie-break-disputed-lines` (only the disputed lines, close up),
  `other-numbered-lines` (also the other lines where code sees printed numbers; its own switch),
  `each-line-alone` (each candidate line in a panel of its own);
* the number reader (Akshara): `closer-look-2x`, `closer-look-3x-upright` (a 3x close-up with a copy
  turned a quarter, so a label drawn sideways stands upright), `tighter-crop`.

**Nothing about safety changes.** A try asks *both* readers again and the item is settled only on
the new pair: a first-round answer and a try answer are never mixed. The same agreement, Rekha's
gate, `seal_label`, drawn-length veto and Nina's gate all still apply to the new pair. A try can only
turn a held item into one that passes those same checks; it can never weaken one.

**Off by default.** `GV_TRY_AGAIN_GATES` lists the gates to switch on (`TRY_AGAIN_GATES`); empty, no
question, packet, picture or run identity changes, byte for byte.

**A bounded allowance.** Every try call is reserved against the reading's own cap *and* against an
allowance of a tenth of it (`TryAgainGuard`), with its own `ReadingStop`: a try refused for the
allowance never stops the main reading; the item waits for a person with `allowance-used`.

**At most three tries, by structure.** A try's question id is the first question's id plus
`:again<n>` (`try_key`), `n` from 1 to 3; a key that is already a try is refused, so a try is never
tried again and a fourth is never built.

Source: issue #1309 · Verification: `tests/extraction/slot_reader/test_try_again.py`,
`tests/workflow/test_try_again.py`
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from extraction.slot_reader.anthropic import BatchSpendGuard, SpendCapExceeded, _Reservation

__all__ = [
    "AKSHARA_CHANGES",
    "AKSHARA_SEAL",
    "AKSHARA_TRY_CODES",
    "ALLOWANCE_SHARE",
    "MAX_TRIES",
    "REKHA_CHANGES",
    "REKHA_OTHER_LINES",
    "REKHA_PRINTED_NUMBERS",
    "REKHA_READERS_AGREE",
    "TRY_AGAIN_FLAG",
    "TRY_AGAIN_GATES",
    "TRY_ANSWER_FLAG",
    "TRY_FIRST_PICK_FLAG",
    "TRY_KEY_MARK",
    "AllowanceUsed",
    "TryAgainGuard",
    "TryOutcome",
    "TryRecord",
    "is_try_key",
    "parse_try_again_gates",
    "try_again_allowance",
    "try_key",
    "try_records_in",
]

#: The line finder's two gates, as Pariksha names them (`workflow/pariksha_run_check.py`).
REKHA_READERS_AGREE: Final = "rekha:two-readers-agree"
REKHA_PRINTED_NUMBERS: Final = "rekha:row-has-printed-numbers"
#: Showing the other lines where code sees printed numbers is its own switch (Anant, 2026-10-14).
REKHA_OTHER_LINES: Final = "rekha:other-numbered-lines"
#: The number reader's seal.
AKSHARA_SEAL: Final = "akshara:seal"
TRY_AGAIN_GATES: Final = frozenset(
    {REKHA_READERS_AGREE, REKHA_PRINTED_NUMBERS, REKHA_OTHER_LINES, AKSHARA_SEAL}
)

#: Up to three tries per item, each changing something, in this order (Anant, 2026-10-14).
MAX_TRIES: Final = 3
REKHA_CHANGES: Final = ("tie-break-disputed-lines", "other-numbered-lines", "each-line-alone")
AKSHARA_CHANGES: Final = ("closer-look-2x", "closer-look-3x-upright", "tighter-crop")
#: The seal holds a try may help with: the two readers' own disagreement or doubt. Never a hold of
#: code's own (crowded, edge, reviewer markup, stacked, not plain, odd fraction, mm/inch) and never
#: a missing reader: asking again cannot change what code saw.
AKSHARA_TRY_CODES: Final = frozenset({"readers-differ", "unsure", "unreadable"})

#: The try allowance: at most this share of the reading's AI cap (Anant, 2026-10-14, decision 4).
ALLOWANCE_SHARE: Final = Decimal(1) / Decimal(10)

#: A try's question id: the first question's id, then this, then the try's number (1 to 3).
TRY_KEY_MARK: Final = ":again"
_TRY_KEY: Final = re.compile(r":again[1-9][0-9]*$")

#: The candidate flags (no migration): one per try made, each answer a sealed try used, and each
#: line finder's first pick.
TRY_AGAIN_FLAG: Final = "try-again:"
TRY_ANSWER_FLAG: Final = "try-again-answer:"
TRY_FIRST_PICK_FLAG: Final = "try-again-first-pick:"

_AGENTS: Final = frozenset({"rekha", "akshara"})
_WORD: Final = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


def parse_try_again_gates(text: str) -> frozenset[str]:
    """The gates `GV_TRY_AGAIN_GATES` switches on: comma-separated, each one of `TRY_AGAIN_GATES`.

    Empty (the default) switches nothing on. An unknown name is refused, never ignored: a typo
    would otherwise leave a gate off while the admin believes it is on.
    """
    names = [name.strip() for name in text.split(",") if name.strip()]
    unknown = sorted(set(names) - TRY_AGAIN_GATES)
    if unknown:
        raise ValueError(
            "GV_TRY_AGAIN_GATES names unknown gates: "
            + ", ".join(unknown)
            + "; known: "
            + ", ".join(sorted(TRY_AGAIN_GATES))
        )
    return frozenset(names)


def try_again_allowance(cap_usd: Decimal) -> Decimal:
    """What every try of one reading may reserve together: a tenth of the reading's cap."""
    if not cap_usd.is_finite() or cap_usd < 0:
        raise ValueError("a reading's cap must be finite and not negative")
    return cap_usd * ALLOWANCE_SHARE


def is_try_key(key: str) -> bool:
    """Whether a question key is a try's (`<first key>:again<n>`)."""
    return TRY_KEY_MARK in key


def try_key(first: str, attempt: int) -> str:
    """The question id of try `attempt` (1 to 3) of the question `first`.

    Refuses a key that is already a try's, and a try number outside 1 to 3: a try is never tried
    again, and a fourth try cannot be built.
    """
    if is_try_key(first):
        raise ValueError(f"{first!r} is already a try; a try is never tried again")
    if isinstance(attempt, bool) or not 1 <= attempt <= MAX_TRIES:
        raise ValueError(f"a try is numbered 1 to {MAX_TRIES}, not {attempt!r}")
    return f"{first}{TRY_KEY_MARK}{attempt}"


class TryOutcome:
    """How one try went (`app.runs.reading_activity.TRY_AGAIN_RESULTS`)."""

    HELPED: Final = "helped"
    NOT_HELPED: Final = "not-helped"
    ALLOWANCE_USED: Final = "allowance-used"
    CANNOT_ZOOM: Final = "cannot-zoom"
    ALL: Final = frozenset({HELPED, NOT_HELPED, ALLOWANCE_USED, CANNOT_ZOOM})


@dataclass(frozen=True, slots=True)
class TryRecord:
    """One try of one item, as its candidate flag keeps it.

    `try-again:<agent>:<gate>:<because>:<change>:<outcome>`: the agent (`rekha`, `akshara`), the
    gate whose stop it answered (`two-readers-agree`, `row-has-printed-numbers`, `seal`), why the
    item stopped, what the try changed, and how it went. `attempt` is the try's number (1 to 3),
    the place of `change` in its agent's order.
    """

    agent: str
    gate: str
    because: str
    change: str
    outcome: str

    def __post_init__(self) -> None:
        if self.agent not in _AGENTS:
            raise ValueError(f"a try belongs to the line finder or the number reader: {self.agent}")
        changes = REKHA_CHANGES if self.agent == "rekha" else AKSHARA_CHANGES
        if self.change not in changes:
            raise ValueError(f"{self.change!r} is not one of the {self.agent} tries")
        if self.outcome not in TryOutcome.ALL:
            raise ValueError(f"{self.outcome!r} is not a try's outcome")
        for value in (self.gate, self.because):
            if not _WORD.fullmatch(value):
                raise ValueError(f"{value!r} is not a short code")

    @property
    def attempt(self) -> int:
        changes = REKHA_CHANGES if self.agent == "rekha" else AKSHARA_CHANGES
        return changes.index(self.change) + 1

    @property
    def helped(self) -> bool:
        return self.outcome == TryOutcome.HELPED

    @property
    def flag(self) -> str:
        return (
            f"{TRY_AGAIN_FLAG}{self.agent}:{self.gate}:{self.because}:{self.change}:{self.outcome}"
        )


def try_records_in(flags: Iterable[str] | None) -> tuple[TryRecord, ...]:
    """The try records kept as flags on one candidate, in order; a malformed one is skipped."""
    found: list[TryRecord] = []
    for flag in flags or ():
        if not flag.startswith(TRY_AGAIN_FLAG):
            continue
        parts = flag.removeprefix(TRY_AGAIN_FLAG).split(":")
        if len(parts) != 5:
            continue
        try:
            found.append(TryRecord(*parts))
        except ValueError:
            continue
    return tuple(found)


class AllowanceUsed(SpendCapExceeded):
    """A try was refused before it was sent: the try allowance (or the reading's cap) is spent.

    A `SpendCapExceeded`, so the reader treats it exactly as a refused question: it abstains, and
    the try batch's own `ReadingStop` stops sending tries. The main reading's stop is never told.
    """


@dataclass(frozen=True, slots=True)
class _TryReservation:
    allowance: _Reservation
    shared: _Reservation | None


class TryAgainGuard(BatchSpendGuard):
    """Reserves every try call against the try allowance *and* the reading's shared guard.

    The allowance is a guard of its own (`try_again_allowance` of the reading's cap); the shared
    guard is the reading's (`read_slot_pages`), so a try's spend counts in the reading's cap, its
    meter and its ledger like any other call. Either refusing refuses the try (`AllowanceUsed`) and
    leaves the other as it was. Settled and released on both.
    """

    def __init__(self, allowance_usd: Decimal, rates: object, shared: BatchSpendGuard | None):
        super().__init__(allowance_usd, rates)
        self._shared = shared

    def reserve(self, model_id: str, input_tokens: int, output_limit: int) -> Any:
        try:
            allowance = super().reserve(model_id, input_tokens, output_limit)
        except SpendCapExceeded as error:
            raise AllowanceUsed("the try-again allowance would be exceeded") from error
        if self._shared is None:
            return _TryReservation(allowance, None)
        try:
            shared = self._shared.reserve(model_id, input_tokens, output_limit)
        except SpendCapExceeded as error:
            super().release(allowance)
            raise AllowanceUsed("the reading's AI cap would be exceeded by a try") from error
        return _TryReservation(allowance, shared)

    def release(self, reservation: Any) -> None:
        super().release(reservation.allowance)
        if self._shared is not None and reservation.shared is not None:
            self._shared.release(reservation.shared)

    def settle(self, reservation: Any, response: Mapping[str, Any]) -> None:
        super().settle(reservation.allowance, response)
        if self._shared is not None and reservation.shared is not None:
            self._shared.settle(reservation.shared, response)

    @property
    def exhausted(self) -> bool:
        return super().exhausted or (self._shared is not None and self._shared.exhausted)
