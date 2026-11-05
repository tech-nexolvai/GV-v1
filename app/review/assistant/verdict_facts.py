"""Facts and the verdict table: where an answer lands, decided by six rows (Phase 2, 2026-11-04).

**The problem this replaces.** Until now "Wrong / please check / Not compared / Right" was decided
by about 3,300 lines (`proof`, `classification`) with 25 named Not-compared reasons. The order of
the questions was wrong: the code asked "is there a reason not to compare?" before "do the two
values differ?", so an answer that held both values could still be filed as Not compared. On the
audited Portage flag run that misfiled 54 of 78 Not-compared answers: 16 were real differences and
38 were plain agreements.

**The separation.** A *fact* and a *verdict* are different things.

- A **fact** is one side's value as it was found: `Present` (the value, its unit, the page and box
  it sits in, who found it and how sure) or `Missing` (the one reason there is no value). The 25
  reasons are reasons a value is *absent*; they live on the fact and never on the verdict.
- **Comparability** is a property of the *pair* of facts, not of either side: two dimensions code
  proved measure different things, a dimension to a centre line, a size against a note in words, a
  reference that is not the architect's. It is decided before the table, and its reason is written
  on the pair. It is never used to hide a difference.
- A **verdict** is then a pure function of the two facts, comparability and the maths: `Same`,
  `Differs` or `NotCompared`. Nothing else can come out, and every verdict names the row that
  produced it, so any answer can be explained by pointing at one row.

**The invariant that closes the leak.** `NotCompared` can only be built from an `Absence`, and an
`Absence` can only be made from a `Missing` fact or from a comparability that failed. So an answer
with both values present and comparable *cannot* be filed as Not compared, whatever a caller does.

**The six rows** (hit policy "unique": for any combination of the inputs exactly one fires):

1. neither side has a value -> Not compared (nothing on either side);
2. the vendor has no value -> Not compared (the vendor's reason);
3. the reference has no value -> Not compared (the reference's reason);
4. both have values but they do not measure the same thing -> Not compared (the pair's reason);
5. both have values, they measure the same thing and the maths agrees -> Right;
6. both have values, they measure the same thing and the maths differs -> a problem.

Numbers are compared exactly, in inches (settled with GV: millimetres are ignored).

Pure: no I/O, no AI call, and it knows nothing about a finding's shape. `verdict_adapter` is what
turns a stored finding into these facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Literal

__all__ = [
    "ROWS",
    "Absence",
    "Comparable",
    "Differs",
    "Fact",
    "Missing",
    "NotCompared",
    "Outcome",
    "Present",
    "Row",
    "Same",
    "Side",
    "Unit",
    "VerdictError",
    "same_inches",
    "verdict",
]

#: What a value is measured in. `inches` is the only one that is compared as a number (settled
#: with GV: millimetres are ignored); `text` is an identifier (a tag, a sheet or revision number);
#: `words`
#: is a note or a description; `none` is a side with no unit of its own.
type Unit = Literal["inches", "text", "words", "none"]
#: Which side of the comparison a fact is: the vendor's shop drawing, or what it is held against
#: (the architect's drawing, GV's rule, the brand prototype, the vendor's own parts).
type Side = Literal["vendor", "reference"]
#: Which side an absence is on, for the Not-compared reason: one side, both, or the pair itself.
type AbsentSide = Literal["vendor", "reference", "both", "pair"]
#: How sure the value is: `code` read it on the drawing, `ai` read it, `unsure` means neither
#: route confirmed it.
type Trust = Literal["code", "ai", "unsure"]


class VerdictError(RuntimeError):
    """A verdict was built that the rules do not allow (the invariant above)."""


@dataclass(frozen=True, slots=True)
class Present:
    """One side's value as it was found on a drawing (or worked out from one)."""

    value: str
    """The value as it is printed or read ("22\"", "ERA-009-AB", "open knee space")."""

    unit: Unit = "none"
    inches: Decimal | None = None
    """The value in inches when it is a size, for the maths; `None` for text and words."""

    page: int | None = None
    box: tuple[float, float, float, float] | None = None
    """Where it sits on that page, when something placed it."""

    source: str = "unknown"
    """Whose drawing or rule it comes from: `vendor`, `architect`, `gv_rule`, `brand_prototype`,
    `vendor_parts` (the vendor's own string added up), `earlier_round`."""

    method: str = "unknown"
    """How it was found: `text` (printed text code read), `ocr`, `ai`, `sum`, `rule`."""

    trust: Trust = "unsure"

    @property
    def found(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class Missing:
    """One side has no value, and the one reason why. The reason is one of the 25 the review has
    always used (`classification.REASONS`): they describe an absent value, nothing else."""

    reason: str
    why: str = ""

    @property
    def found(self) -> bool:
        return False


type Fact = Present | Missing


@dataclass(frozen=True, slots=True)
class Comparable:
    """Whether the two facts measure the same thing. `ok` false carries the reason, on the pair."""

    ok: bool
    reason: str | None = None
    why: str = ""

    proved: bool = False
    """Code proved on the drawing that the two do not measure the same thing (the ends are
    unlike, an end is a centre line, the reference marks its size V.I.F., the item's own copies
    disagree). A pair that is only *not the architect's* is not proved: it is simply held against
    the wrong drawing. Nothing is ever red on an unproved pair either way."""

    def __post_init__(self) -> None:
        if not self.ok and not self.reason:
            raise VerdictError("a pair that is not comparable must say why")
        if self.ok and self.reason:
            raise VerdictError("a comparable pair has no reason")


#: Only this module may seal an `Absence`, so `NotCompared` cannot be built from nothing.
_SEALED: Final = object()


@dataclass(frozen=True, slots=True)
class Absence:
    """Why there was nothing to compare. It is the only way into `NotCompared`, and it can only be
    made from a `Missing` fact (`of_fact`) or from a comparability that failed (`of_pair`)."""

    side: AbsentSide
    reason: str
    why: str = ""
    seal: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.seal is not _SEALED:
            raise VerdictError("an Absence is made by of_fact or of_pair, never by hand")

    @staticmethod
    def of_fact(side: AbsentSide, fact: Fact) -> Absence:
        """The absence of one side's value; refuses a side that has one."""
        if not isinstance(fact, Missing):
            raise VerdictError(f"the {side} side has a value: it is not an absence")
        return Absence(side, fact.reason, fact.why, _SEALED)

    @staticmethod
    def of_pair(pair: Comparable) -> Absence:
        """The pair that does not measure the same thing; refuses a pair that does."""
        if pair.ok or pair.reason is None:
            raise VerdictError("the two facts are comparable: it is not an absence")
        return Absence("pair", pair.reason, pair.why, _SEALED)


@dataclass(frozen=True, slots=True)
class Same:
    """Compared, and the two values agree: Right."""


@dataclass(frozen=True, slots=True)
class Differs:
    """Compared, and the two values do not agree: a problem (red when code checked it)."""


@dataclass(frozen=True, slots=True)
class NotCompared:
    """Nothing was compared, and what was absent. Buildable only from an `Absence`."""

    absent: Absence

    def __post_init__(self) -> None:
        if not isinstance(self.absent, Absence):
            raise VerdictError("Not compared is built from an Absence, never from a bare reason")

    @property
    def reason(self) -> str:
        return self.absent.reason


type Outcome = Same | Differs | NotCompared

#: What a row asks of one input: `True`, `False`, or `None` for "whatever it is".
type Ask = bool | None


@dataclass(frozen=True, slots=True)
class Row:
    """One row of the table: what it asks of the four inputs, what comes out, and the row in plain
    English for the report."""

    id: str
    vendor_found: Ask
    reference_found: Ask
    comparable: Ask
    differs: Ask
    gives: Literal["same", "differs", "not_compared"]
    absent: AbsentSide | None
    words: str

    def fires(self, vendor: bool, reference: bool, comparable: bool, differs: bool) -> bool:
        """Whether this row covers one combination of the inputs."""
        asked = (self.vendor_found, self.reference_found, self.comparable, self.differs)
        given = (vendor, reference, comparable, differs)
        return all(ask is None or ask == is_so for ask, is_so in zip(asked, given, strict=True))


#: The table. Hit policy **unique**: for every combination of the four inputs exactly one row
#: fires (`tests/review/assistant/test_verdict_facts.py` walks the whole grid).
ROWS: Final[tuple[Row, ...]] = (
    Row(
        id="V1",
        vendor_found=False,
        reference_found=False,
        comparable=None,
        differs=None,
        gives="not_compared",
        absent="both",
        words="Neither drawing gives a value: not compared, with the reason there is none.",
    ),
    Row(
        id="V2",
        vendor_found=False,
        reference_found=True,
        comparable=None,
        differs=None,
        gives="not_compared",
        absent="vendor",
        words="The vendor's value is missing: not compared, with the reason it could not be read.",
    ),
    Row(
        id="V3",
        vendor_found=True,
        reference_found=False,
        comparable=None,
        differs=None,
        gives="not_compared",
        absent="reference",
        words="The architect (or GV's rule) gives nothing to hold it against: not compared.",
    ),
    Row(
        id="V4",
        vendor_found=True,
        reference_found=True,
        comparable=False,
        differs=None,
        gives="not_compared",
        absent="pair",
        words="Both values are there, but code proved they do not measure the same thing: "
        "not compared, with that reason on the pair.",
    ),
    Row(
        id="V5",
        vendor_found=True,
        reference_found=True,
        comparable=True,
        differs=False,
        gives="same",
        absent=None,
        words="Both values are there, they measure the same thing and they agree: Right.",
    ),
    Row(
        id="V6",
        vendor_found=True,
        reference_found=True,
        comparable=True,
        differs=True,
        gives="differs",
        absent=None,
        words="Both values are there, they measure the same thing and they do not agree: "
        "a problem (red when code checked it, otherwise please check).",
    ),
)


@dataclass(frozen=True, slots=True)
class Verdict:
    """A verdict and the row that produced it."""

    outcome: Outcome
    row: str

    @property
    def same(self) -> bool:
        return isinstance(self.outcome, Same)

    @property
    def differs(self) -> bool:
        return isinstance(self.outcome, Differs)

    @property
    def compared(self) -> bool:
        return not isinstance(self.outcome, NotCompared)

    @property
    def absent(self) -> Absence | None:
        return self.outcome.absent if isinstance(self.outcome, NotCompared) else None


def same_inches(vendor: Decimal, reference: Decimal) -> bool:
    """Whether two sizes in inches agree. Settled with GV: inches count and are compared
    exactly, millimetres are ignored. No tolerance, so 22" against 22 1/2" differs."""
    return vendor == reference


def verdict(vendor: Fact, reference: Fact, pair: Comparable, differs: bool) -> Verdict:
    """The verdict for one answer, by the table above.

    `differs` is the maths result on the two values and is read only when both sides have a value
    and the pair is comparable (rows V5 and V6). Raises `VerdictError` if the table ever has a gap
    or an overlap, which the exhaustive test makes impossible.
    """
    comparable = pair.ok
    fired = [row for row in ROWS if row.fires(vendor.found, reference.found, comparable, differs)]
    if len(fired) != 1:
        raise VerdictError(
            f"the table must fire exactly one row, {len(fired)} fired for "
            f"vendor={vendor.found} reference={reference.found} comparable={comparable} "
            f"differs={differs}"
        )
    row = fired[0]
    if row.gives == "same":
        return Verdict(Same(), row.id)
    if row.gives == "differs":
        return Verdict(Differs(), row.id)
    if row.absent == "pair":
        return Verdict(NotCompared(Absence.of_pair(pair)), row.id)
    if row.absent == "both":
        return Verdict(NotCompared(Absence.of_fact("both", vendor)), row.id)
    side: Fact = vendor if row.absent == "vendor" else reference
    return Verdict(NotCompared(Absence.of_fact(row.absent or "vendor", side)), row.id)
