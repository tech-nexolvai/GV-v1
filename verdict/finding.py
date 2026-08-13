"""What a check produces: an outcome, and everything needed to defend it.

A finding is the unit a reviewer signs off and a vendor argues with. Months later the only
question that matters is *"what judged this drawing, and on what evidence?"* — so a finding
carries the rule snapshot id, the parameter set ids, every operand's evidence reference and the
full calculation trace, not merely a verdict.

Lives in `verdict/` for the same reason as `VerdictOperand`: it is produced here, and
`docs/DESIGN.md` §2 forbids `verdict/` importing the packages that consume it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from units.measurement import Measurement
from verdict.outcomes import Outcome, Severity, is_decision
from verdict.trace import CalculationTrace


@dataclass(frozen=True, slots=True)
class Finding:
    """One check's result, with its full provenance."""

    rule_id: str
    outcome: Outcome
    severity: Severity
    reason: str
    """Plain English, for the reviewer. Says what was compared and why it came out this way."""

    snapshot_id: str
    """The exact rule snapshot that produced this (ADR-0005). Without it, a re-run cannot be
    reproduced and the finding cannot be defended."""

    engine_version: str
    trace: CalculationTrace | None = None
    """Absent when the check abstained before any arithmetic ran — there is nothing to trace,
    and inventing an empty one would suggest a calculation happened."""

    delta: Measurement | None = None
    parameter_set_ids: tuple[str, ...] = ()
    """Every parameter set consulted, so the numbers behind the check are recoverable."""

    evidence_refs: tuple[str, ...] = ()
    """Where each operand came from on the drawing."""

    variant: str | None = None
    """The applicability variant that applied, e.g. `back_left_right`."""

    notes: tuple[str, ...] = field(default_factory=tuple)
    """Things a reviewer should see but that did not change the outcome — an overridden company
    standard, a requirement that was not exercised, a declared cross-unit allowance."""

    def __post_init__(self) -> None:
        if not self.rule_id.strip():
            raise ValueError("a finding must name the rule that produced it")
        if not self.snapshot_id.strip():
            raise ValueError(
                "a finding must record its rule snapshot id. Without it the verdict cannot be "
                "reproduced later, which is the whole reason snapshots exist (ADR-0005)."
            )
        if is_decision(self.outcome) and self.trace is None:
            raise ValueError(
                f"{self.outcome.value} without a calculation trace. A decision a reviewer "
                "cannot check by hand is not defensible; only an abstention may lack a trace."
            )

    @property
    def is_decision(self) -> bool:
        """True when the engine decided, rather than abstaining."""
        return is_decision(self.outcome)

    @property
    def is_critical_failure(self) -> bool:
        """A FAIL on a check whose severity blocks release."""
        return self.outcome is Outcome.FAIL and self.severity is Severity.CRITICAL

    def summary(self) -> str:
        """One line for a report."""
        head = f"{self.rule_id}: {self.outcome.value} [{self.severity.value}] — {self.reason}"
        return f"{head} ({self.snapshot_id[:15]}...)" if self.snapshot_id else head
