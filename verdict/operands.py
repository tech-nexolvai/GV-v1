"""What the verdict engine is allowed to compute on.

A `VerdictOperand` is a value that has already cleared the Evidence Gate: normalised,
corroborated, and sealed with a reference back to the evidence it came from. The engine
accepts nothing else.

**Why this lives in `verdict/` rather than `evidence/`.** `docs/DESIGN.md` §2 forbids
`verdict/` from importing `evidence/`, and `tests/test_verdict_isolation.py` fails the build if
it tries. So the operand contract is owned by the side that *consumes* it: `evidence/` builds
operands against this definition and hands them over. Pointing the dependency the other way
would mean the verdict engine importing the pipeline it is supposed to be isolated from.

The evidence states are from the architecture documents, unchanged: only `CORROBORATED` and
`HUMAN_CONFIRMED` may enter a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction

from units.measurement import Measurement, ensure_exact


class EvidenceStatus(StrEnum):
    """How well established a value is.

    The gate assigns this; the engine only reads it. Two of the five may enter a verdict, and
    the other three are the reason a check abstains rather than guesses.
    """

    RAW_CANDIDATE = "RAW_CANDIDATE"
    """One unverified extraction route produced it. Not evidence yet."""

    CORROBORATED = "CORROBORATED"
    """Independent routes agree and the semantic association is valid. May enter a verdict."""

    HUMAN_CONFIRMED = "HUMAN_CONFIRMED"
    """A reviewer explicitly confirmed the value and its evidence. May enter a verdict."""

    CONFLICTING = "CONFLICTING"
    """Readers, units or associations disagree.

    Never resolved by confidence or by preferring one reader. A disagreement about a number is
    exactly the case where guessing produces a confident wrong answer.
    """

    REJECTED = "REJECTED"
    """The candidate was found invalid."""


#: The only two states that may reach arithmetic. Everything else abstains.
QUALIFIED_STATUSES: frozenset[EvidenceStatus] = frozenset(
    {EvidenceStatus.CORROBORATED, EvidenceStatus.HUMAN_CONFIRMED}
)


@dataclass(frozen=True, slots=True)
class VerdictOperand:
    """One sealed input to a check.

    Frozen, and carrying its own provenance: the engine must be able to say not just what a
    value was but where it came from, or a finding cannot be audited afterwards.
    """

    name: str
    value: Measurement | Fraction | str | None
    status: EvidenceStatus
    source: str
    """`SHOP`, `ARCH`, `PRODUCT_SPEC`, `LITERAL` or `USER_INPUT`."""

    evidence_ref: str | None = None
    """Where to look on the drawing — a page and polygon reference, or the parameter set id
    for a value that appears on no drawing."""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a verdict operand must be named")
        if not isinstance(self.status, EvidenceStatus):
            raise TypeError("status must be an EvidenceStatus")
        ensure_exact(self.value, context=f"operand {self.name!r}")
        if not self.source.strip():
            raise ValueError(
                f"operand {self.name!r} must name its source. A value whose origin is unknown "
                "cannot be qualified, and an unqualified value cannot enter a verdict."
            )

    @property
    def is_qualified(self) -> bool:
        """True when this operand may enter a verdict at all."""
        return self.status in QUALIFIED_STATUSES

    @property
    def is_present(self) -> bool:
        """True when a value is actually here.

        Zero is present. `0 mm` is a real measurement — a flush edge, no gap — and treating it
        as absent would turn a real value into NOT_FOUND (ADR-0012). An empty string is absent:
        that is a failed extraction wearing a value's clothes.
        """
        if self.value is None:
            return False
        if isinstance(self.value, str):
            return bool(self.value.strip())
        return True
