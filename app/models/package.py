"""Projects, packages, revisions and their ordered state-event history.

Revisions supersede older rows rather than replacing them.  State events are immutable,
ordered records so later lifecycle work can reconstruct exactly what happened.

Source: backend proposal section 10.1, ``AGENTS.md`` sections 2.7 and 6, issue #192.
Verification: ``tests/db/test_package_models.py``.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Immutable, TimestampedUUID
from vocabulary.semantic_types import ProductType


class PackageState(StrEnum):
    """Persisted package lifecycle states fixed by ``DESIGN_PLATFORM.md`` section 5."""

    CREATED = "CREATED"
    UPLOADING = "UPLOADING"
    UPLOADED = "UPLOADED"
    INGESTING = "INGESTING"
    EXTRACTING = "EXTRACTING"
    MATCHING = "MATCHING"
    VALIDATING_EVIDENCE = "VALIDATING_EVIDENCE"
    RUNNING_CHECKS = "RUNNING_CHECKS"
    GENERATING_OUTPUTS = "GENERATING_OUTPUTS"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    NEEDS_INPUT = "NEEDS_INPUT"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


PACKAGE_STATE_VALUES = ", ".join(f"'{state.value}'" for state in PackageState)

#: The products a package may be for: the rulebook's own `ProductType` vocabulary (#994).
PRODUCT_TYPE_VALUES = ", ".join(f"'{product.value}'" for product in ProductType)


class Project(Base, TimestampedUUID):
    """The structural isolation boundary for all package data below it."""

    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(200))
    company_standards_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("parameter_sets.id", ondelete="RESTRICT"), default=None
    )
    """The company-standard parameter set this project starts from, if it has one (#303).

    This column has existed since migration 0003 with no foreign key, because the table it points at did
    not exist — the comment here used to say so. Migration 0027 adds the constraint additively.

    `RESTRICT`: deleting a parameter set that a project still names would leave the project pointing at
    nothing, and the numbers behind its findings are what that pointer is for."""


class Package(Base, TimestampedUUID):
    """One reviewable drawing package owned by exactly one project."""

    __tablename__ = "packages"

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    vendor: Mapped[str | None] = mapped_column(String(200), default=None)
    product_type: Mapped[str | None] = mapped_column(String(32))
    """What product the drawing set is for, as the reviewer chose it at upload (#994).

    No Python-side default on purpose: a package built without one leaves the column out of its
    `INSERT`, so code and tests that write a package against a schema from before 0073 still work.

    A `ProductType` value. The check run evaluates only the published rules for this product, and
    the readers are told it as context. **NULL means not stated** — every package created before
    #994 — and keeps the old behaviour: every product's rules run. Never inferred from the vendor,
    the file names or the drawing; the API requires it for a new package instead."""

    __table_args__ = (
        CheckConstraint(
            f"product_type IS NULL OR product_type IN ({PRODUCT_TYPE_VALUES})",
            name="package_product_type",
        ),
    )


class PackageRevision(Base, TimestampedUUID):
    """One historical package revision, optionally superseding its predecessor."""

    __tablename__ = "package_revisions"

    package_id: Mapped[UUID] = mapped_column(ForeignKey("packages.id", ondelete="RESTRICT"))
    revision_number: Mapped[int]
    state: Mapped[str] = mapped_column(String(32))
    supersedes_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("package_revisions.id", ondelete="RESTRICT"),
        default=None,
    )

    supersedes: Mapped[PackageRevision | None] = relationship(
        remote_side="PackageRevision.id",
        foreign_keys=[supersedes_id],
    )

    __table_args__ = (
        CheckConstraint(
            f"state IN ({PACKAGE_STATE_VALUES})",
            name="package_revision_state",
        ),
        UniqueConstraint("package_id", "revision_number"),
        # So `package_revision_documents` can resolve a revision *and* its package in one key
        # (ADR-0018). Without this the membership row could name a revision of one package and a
        # document of another, with every individual value true — proven against PostgreSQL before the
        # ADR was accepted, which is why the constraint is here rather than assumed.
        UniqueConstraint("id", "package_id", name="uq_package_revisions_id_package"),
    )


class PackageStateEvent(Base, TimestampedUUID, Immutable):
    """An append-only, explicitly ordered package lifecycle transition."""

    __tablename__ = "package_state_events"

    package_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("package_revisions.id", ondelete="RESTRICT")
    )
    sequence: Mapped[int]
    from_state: Mapped[str | None] = mapped_column(String(32), default=None)
    to_state: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str | None] = mapped_column(String(1000), default=None)

    workflow_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="RESTRICT"), default=None, index=True
    )
    """The workflow run that caused this transition, where one did (#210, C3.2).

    Nullable, and the null carries meaning: nothing ran. A reviewer approving a package and a revision's
    genesis event are both real transitions with no workflow behind them, so a non-nullable column would
    have to be filled with something untrue.

    A foreign key rather than an id in the `reason` text, because the question this table exists to
    answer — *"what happened to this package, and when?"* — is asked in a dispute, and an id inside
    prose cannot be joined, constrained, or found reliably.
    """

    __table_args__ = (
        CheckConstraint(
            f"from_state IS NULL OR from_state IN ({PACKAGE_STATE_VALUES})",
            name="package_event_from_state",
        ),
        CheckConstraint(
            f"to_state IN ({PACKAGE_STATE_VALUES})",
            name="package_event_to_state",
        ),
        # An event that names nobody is an event nobody can be asked about. `NOT NULL` alone permitted
        # `actor = ''`, which satisfies the schema and names no one (#210).
        CheckConstraint("actor <> ''", name="package_event_actor"),
        UniqueConstraint("package_revision_id", "sequence"),
    )


def package_product(package: Package) -> ProductType | None:
    """The product a package's drawing set is for, or `None` where it was never stated (#994).

    One reading of the column for every consumer — the check run, the readers and the report — so
    none of them can treat an unknown stored value as "every product". The CHECK constraint makes
    such a value unstorable; `ProductType(...)` raising here is the second line.
    """
    return None if package.product_type is None else ProductType(package.product_type)
