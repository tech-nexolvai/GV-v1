"""The four outcomes a check can produce, and how serious a check is.

Two enums, no behaviour, no dependencies. This module is imported by `rules/`, `verdict/`
and `eval/` alike, so it stays trivially importable and standard-library only.

The values are persisted inside findings, so they are part of the data contract: renaming
one later would invalidate every stored finding. Treat them as fixed.

See `docs/DESIGN.md` §3.4, ADR-0003 and ADR-0004.
"""

from __future__ import annotations

from enum import StrEnum


class Outcome(StrEnum):
    """What a check concluded.

    Only ``PASS`` and ``FAIL`` are decisions. The other three are honest abstentions, and
    the distinction matters: abstaining costs reviewer time, whereas a wrong ``PASS`` can be
    manufactured. See :func:`is_decision`.
    """

    PASS = "PASS"
    """Validated operands satisfy the published rule and its tolerance."""

    FAIL = "FAIL"
    """Validated operands violate the published rule or its tolerance."""

    NOT_FOUND = "NOT_FOUND"
    """A required authoritative input, unit or approved match is absent.

    Never a default and never zero — a missing value is not a passing value.
    """

    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    """Evidence conflicts, the association is ambiguous, or judgement is needed.

    Also the outcome when a rule's applicability discriminator cannot be established: we do
    not guess which layout a drawing shows.
    """

    NO_APPLICABLE_RULE = "NO_APPLICABLE_RULE"
    """No published rule covers this item's scope (ADR-0004).

    Deliberately distinct from ``NOT_FOUND``. ``NOT_FOUND`` means the drawing did not give
    us a value, and sends a reviewer looking for a dimension. This means no rule exists for
    the layout at all, and sends them to the rulebook instead — the same abstention, but a
    different instruction.

    It is emphatically **not** a flavour of ``PASS``. Every client countertop check is
    currently scoped to walls on three sides, so an island countertop matches nothing; if
    that produced silence, a reviewer would reasonably conclude the package was checked and
    was fine. Silence is the most dangerous false PASS there is, because it looks like
    success and leaves nothing to audit.
    """


class Severity(StrEnum):
    """How much a wrong answer on this check costs.

    The primary release metric is the *critical* false-PASS rate, so without this the metric
    cannot be computed at all — nothing else records which checks are critical (D3).
    """

    CRITICAL = "CRITICAL"
    """A wrong PASS could be manufactured and cost money. Blocks release."""

    MAJOR = "MAJOR"
    """A real defect, but one normally caught downstream."""

    MINOR = "MINOR"
    """Cosmetic, or trivially corrected on site."""

    ADVISORY = "ADVISORY"
    """Reported as a warning, never as a failure.

    The client asked for exactly this: the ``CT009`` back-offset constraint says the program
    *"should throw warning"* rather than fail the package.
    """


#: Outcomes where the engine actually decided something.
DECISIVE_OUTCOMES: frozenset[Outcome] = frozenset({Outcome.PASS, Outcome.FAIL})

#: Outcomes where the system declined to decide. Honest abstention, not failure.
ABSTAINING_OUTCOMES: frozenset[Outcome] = frozenset(
    {Outcome.NOT_FOUND, Outcome.REVIEW_REQUIRED, Outcome.NO_APPLICABLE_RULE}
)


def is_decision(outcome: Outcome) -> bool:
    """True when the engine reached a verdict rather than abstaining.

    The false-PASS metric is computed over decisions only, and automation coverage is the
    proportion of checks that reached one. Both need this split, so it lives here beside the
    enum rather than being re-derived by each caller.
    """
    return outcome in DECISIVE_OUTCOMES


def is_abstention(outcome: Outcome) -> bool:
    """True when the system declined to decide, for any of the three reasons."""
    return outcome in ABSTAINING_OUTCOMES
