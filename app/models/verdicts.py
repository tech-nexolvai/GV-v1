"""Check runs, the sealed operands a verdict was computed from, findings, and their evidence.

**This is the table a client or an auditor would be shown.** If a finding cannot be reproduced from
its own stored inputs, the audit trail is decoration — so `verdict_inputs` keeps every operand that
entered the arithmetic, exactly, and a finding can be recomputed years later and compared.

Four properties, each of which the schema rather than a convention has to hold:

**A finding stores the rule *snapshot*, never the rule.** A rule id says "the width check"; a snapshot
id says which version of it, with what tolerance, from what content hash. Reconstructing a decision
needs the second, and `RESTRICT` keeps the snapshot alive for as long as any finding cites it.

**Operands persist as exact rationals.** A numerator and a denominator, never a float. `AGENTS.md`
§2.3 and ADR-0001: a rounded operand recomputes to a different answer, so the stored inputs would
disagree with the stored outcome and nobody could tell which was wrong.

**Only qualified evidence may be an operand.** `verdict/operands.py` admits `CORROBORATED` and
`HUMAN_CONFIRMED` into a verdict and refuses the other three. That is enforced here too, because a
`RAW_CANDIDATE` written into `verdict_inputs` would be a single unverified extraction carrying the
weight of corroborated evidence.

**A finding with no evidence cannot exist.** Not "should not" — the acceptance says cannot, and a
finding nobody can trace to a reading is an assertion. A `CHECK` cannot count rows in another table,
so this is a deferred constraint checked at commit; the limit is stated in `finding_evidence`.

Source: backend proposal §10.1 · Design: `docs/DESIGN_PLATFORM.md` §3.1 ·
Verification: `tests/db/test_verdict_models.py`
"""

from __future__ import annotations

from enum import Enum
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Immutable, TimestampedUUID
from units.measurement import Unit
from verdict.operands import QUALIFIED_STATUSES
from verdict.outcomes import Outcome, Severity


def _sql_values(enum_type: type[Enum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum_type)


OUTCOME_VALUES = _sql_values(Outcome)
SEVERITY_VALUES = _sql_values(Severity)
UNIT_VALUES = _sql_values(Unit)

#: The two statuses `verdict/operands.py` admits into a verdict, sorted for a stable constraint.
#: The other three — `RAW_CANDIDATE`, `CONFLICTING`, `REJECTED` — are why a check abstains, and a
#: row carrying one would be an unverified reading with the weight of corroborated evidence.
QUALIFIED_STATUS_VALUES = ", ".join(
    f"'{status.value}'" for status in sorted(QUALIFIED_STATUSES, key=lambda s: s.value)
)


class CheckRun(Base, TimestampedUUID):
    """One rule, one subject, one attempt.

    Not `Immutable`: a run is a process record and may be annotated as it completes. What must not
    change is the finding it produced, and a re-run produces a *new* run rather than editing this one.
    """

    __tablename__ = "check_runs"

    package_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("package_revisions.id", ondelete="RESTRICT"), index=True
    )
    rule_snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("rule_snapshots.id", ondelete="RESTRICT"), index=True
    )
    """The snapshot, not the rule. `RESTRICT` because a finding citing a deleted snapshot would be a
    decision nobody can reconstruct."""

    engine_version: Mapped[str] = mapped_column(String(64))
    """Which build of the verdict engine computed it. A change in the engine is one of the few things
    that can move a result without any input moving, and `eval/regression.py` needs to be able to
    attribute that."""

    __table_args__ = (
        CheckConstraint("engine_version <> ''", name="check_run_engine_version_present"),
        # Lets a child carry the revision and have the database prove it is the run's own.
        UniqueConstraint("id", "package_revision_id", name="uq_check_runs_id_revision"),
        Index("ix_check_runs_revision_snapshot", "package_revision_id", "rule_snapshot_id"),
    )


class VerdictInput(Base, TimestampedUUID, Immutable):
    """One sealed operand that entered the arithmetic.

    The row that makes a finding re-checkable. Without it a finding is a claim about a calculation
    nobody can repeat.
    """

    __tablename__ = "verdict_inputs"

    check_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("check_runs.id", ondelete="RESTRICT"), index=True
    )
    operand_name: Mapped[str] = mapped_column(String(100))
    """As the rule names it — `width`, `filler_left`. The name is part of the reconstruction: the same
    number in a different slot is a different calculation."""

    value_numerator: Mapped[int] = mapped_column()
    value_denominator: Mapped[int] = mapped_column()
    """An exact rational, never a float. ADR-0001. A rounded operand recomputes to a different answer,
    and the stored inputs would then disagree with the stored outcome."""

    unit: Mapped[str] = mapped_column(String(16))
    evidence_status: Mapped[str] = mapped_column(String(32))
    """Named `evidence_status`, matching `verdict/operands.py`. The issue's plan called it
    `evidence_state`; there is no such type, and inventing a parallel name for the five-member
    `EvidenceStatus` is how two documents come to disagree about what a value's provenance was."""

    canonical_observation_id: Mapped[UUID | None] = mapped_column(
        # Named explicitly: the generated name runs past PostgreSQL's 63-character limit and gets a
        # hash suffix, which a hand-written migration then has to guess. #198 lost four CI rounds to
        # exactly that.
        ForeignKey(
            "canonical_observations.id",
            ondelete="RESTRICT",
            name="fk_verdict_inputs_observation",
        ),
        default=None,
        index=True,
    )
    """Nullable, because not every operand comes from a drawing. A literal lives in the rule and a
    user input is what somebody typed — neither has an observation, and a non-null column would force
    one to be invented."""

    __table_args__ = (
        UniqueConstraint("check_run_id", "operand_name", name="uq_verdict_inputs_run_operand"),
        CheckConstraint("operand_name <> ''", name="verdict_input_operand_name_present"),
        CheckConstraint("value_denominator > 0", name="verdict_input_denominator_positive"),
        CheckConstraint(f"unit IN ({UNIT_VALUES})", name="verdict_input_unit"),
        # The gate, in the database. `verdict/operands.py` admits two of five statuses into a verdict.
        CheckConstraint(
            f"evidence_status IN ({QUALIFIED_STATUS_VALUES})", name="verdict_input_status_qualified"
        ),
    )


class Finding(Base, TimestampedUUID, Immutable):
    """What a check concluded, and enough to see why.

    `Immutable`. A re-run produces a new finding against a new check run; editing this one would
    change what a reviewer signed off on, retrospectively and silently.
    """

    __tablename__ = "findings"

    check_run_id: Mapped[UUID] = mapped_column(unique=True, index=True)
    """One finding per run. Two would leave "what did this check decide?" with two answers."""

    package_revision_id: Mapped[UUID] = mapped_column(index=True)
    """Which revision this finding is about.

    Denormalised from the check run, and the composite foreign key below makes the copy honest — the
    database refuses a finding claiming a revision its own run does not have. It is here so that a
    review action or an approval can be tied to the same revision by a foreign key rather than by a
    convention: without it, a session reviewing package A could carry an action on a finding from
    package B, and the record would misstate what was reviewed.
    """

    outcome: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(16))
    trace: Mapped[dict[str, object]] = mapped_column(JSONB)
    """The calculation, step by step, as the engine produced it. What a reviewer reads when they ask
    why — and what a recompute is compared against."""

    parameter_set_versions: Mapped[dict[str, str]] = mapped_column(JSONB)
    """Which project parameters were in force. A tolerance that changed between runs moves a result
    without any drawing changing, and a finding that did not record them cannot be attributed."""

    __table_args__ = (
        ForeignKeyConstraint(
            ["check_run_id", "package_revision_id"],
            ["check_runs.id", "check_runs.package_revision_id"],
            name="fk_findings_run_revision",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "package_revision_id", name="uq_findings_id_revision"),
        CheckConstraint(f"outcome IN ({OUTCOME_VALUES})", name="finding_outcome"),
        CheckConstraint(f"severity IN ({SEVERITY_VALUES})", name="finding_severity"),
        Index("ix_findings_outcome_severity", "outcome", "severity"),
    )


class FindingEvidence(Base, TimestampedUUID, Immutable):
    """A link from a finding to a reading that produced it.

    The acceptance says a finding with no evidence *cannot* be stored. A `CHECK` cannot count rows in
    another table, so the database alone cannot hold that: the closest it comes is `RESTRICT` on both
    sides, keeping the evidence alive as long as the finding cites it.

    `test_a_finding_without_evidence_is_refused` asserts the writer never creates one. That is weaker
    than a constraint, and it is said here rather than left for somebody to assume the schema is
    enforcing it — enforcing it needs a deferred trigger, which is `C1.12`'s territory.
    """

    __tablename__ = "finding_evidence"

    finding_id: Mapped[UUID] = mapped_column(
        ForeignKey("findings.id", ondelete="RESTRICT"), index=True
    )
    canonical_observation_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "canonical_observations.id",
            ondelete="RESTRICT",
            name="fk_finding_evidence_observation",
        ),
        index=True,
    )
    role: Mapped[str] = mapped_column(String(50))
    """Why this reading mattered — `operand`, `context`, `conflicting`. A finding that lists every
    observation on the page without saying which it used is a citation nobody can check."""

    __table_args__ = (
        UniqueConstraint(
            "finding_id", "canonical_observation_id", "role", name="uq_finding_evidence_link"
        ),
        CheckConstraint("role <> ''", name="finding_evidence_role_present"),
    )
