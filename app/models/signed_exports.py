"""Immutable approval-bound publication inputs and complete output bundles."""

from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Immutable, TimestampedUUID


class ApprovalExportSnapshot(Base, TimestampedUUID, Immutable):
    __tablename__ = "approval_export_snapshots"

    approval_id: Mapped[UUID] = mapped_column(unique=True)
    package_revision_id: Mapped[UUID]
    canonical_json: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    deterministic_sha256: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        ForeignKeyConstraint(
            ["approval_id", "package_revision_id"],
            ["approvals.id", "approvals.package_revision_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "package_revision_id", name="uq_export_snapshot_id_revision"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="export_snapshot_hash"),
        CheckConstraint(
            "deterministic_sha256 ~ '^[0-9a-f]{64}$'", name="export_snapshot_facts_hash"
        ),
    )


class ApprovalExportAction(Base, TimestampedUUID, Immutable):
    __tablename__ = "approval_export_actions"

    snapshot_id: Mapped[UUID]
    package_revision_id: Mapped[UUID]
    review_action_id: Mapped[UUID]
    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_id", "package_revision_id"],
            ["approval_export_snapshots.id", "approval_export_snapshots.package_revision_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("snapshot_id", "review_action_id", name="uq_export_snapshot_action"),
        ForeignKeyConstraint(
            ["review_action_id", "package_revision_id"],
            ["review_actions.id", "review_actions.package_revision_id"],
            ondelete="RESTRICT",
        ),
    )


class ApprovalExportBundle(Base, TimestampedUUID, Immutable):
    __tablename__ = "approval_export_bundles"

    snapshot_id: Mapped[UUID] = mapped_column(unique=True)
    package_revision_id: Mapped[UUID]
    pdf_id: Mapped[UUID]
    workbook_id: Mapped[UUID]
    redline_id: Mapped[UUID]
    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_id", "package_revision_id"],
            ["approval_export_snapshots.id", "approval_export_snapshots.package_revision_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "pdf_id <> workbook_id AND pdf_id <> redline_id AND workbook_id <> redline_id",
            name="export_bundle_distinct_files",
        ),
        *(
            ForeignKeyConstraint(
                [column, "package_revision_id"],
                ["output_artifacts.id", "output_artifacts.package_revision_id"],
                ondelete="RESTRICT",
            )
            for column in ("pdf_id", "workbook_id", "redline_id")
        ),
    )


class ApprovalExportFailure(Base, TimestampedUUID, Immutable):
    """A failed publication attempt; no client data or exception message is recorded."""

    __tablename__ = "approval_export_failures"
    snapshot_id: Mapped[UUID]
    package_revision_id: Mapped[UUID]
    error_type: Mapped[str] = mapped_column(String(100))
    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_id", "package_revision_id"],
            ["approval_export_snapshots.id", "approval_export_snapshots.package_revision_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("error_type <> ''", name="export_failure_error_type"),
    )


class ApprovalExportRetry(Base, TimestampedUUID, Immutable):
    """A reviewer asked to publish the same frozen record again after a recorded failure (#1065).

    Nothing is re-signed: the row points at the existing snapshot, which pins the approval and its
    facts. Each publication request (the first, at sign-off, plus one per retry) ends in one bundle
    or one failure, so "every request so far has failed" is what makes a retry allowed, and a second
    press while a retry is still being prepared finds a request that has not failed yet.
    """

    __tablename__ = "approval_export_retries"
    snapshot_id: Mapped[UUID]
    package_revision_id: Mapped[UUID]
    requested_by: Mapped[str] = mapped_column(String(200))
    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_id", "package_revision_id"],
            ["approval_export_snapshots.id", "approval_export_snapshots.package_revision_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("requested_by <> ''", name="export_retry_requested_by"),
        Index("ix_approval_export_retries_snapshot_id", "snapshot_id"),
    )
