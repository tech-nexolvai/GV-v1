"""Immutable source files, document versions and their complete page manifest.

The storage row identifies exact bytes; a document version pins those same bytes, and every
page pins the content later extraction stages read. Unknown classification and revision data
remain null rather than being replaced with plausible defaults.

Source: backend proposal section 10.1, ``DESIGN_EXTRACTION.md`` sections 3.1 and 7,
ADR-0015, and issue #193.
Verification: ``tests/db/test_document_models.py``.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Immutable, TimestampedUUID
from vocabulary.page_types import PageType

#: ``PageType`` is re-exported, not redefined. This module owned an identical copy of the enum,
#: which is how one value quietly comes to mean two things; the single definition now lives in
#: ``vocabulary/``, the one package ``extraction/``, ``evidence/`` and ``app/`` may all import and
#: which imports nothing itself. ``app.models.document.PageType`` keeps working unchanged, and the
#: page-type check constraint below is still derived from the members, so it renders identically.
__all__ = [
    "Document",
    "DocumentKind",
    "DocumentVersion",
    "PackageRevisionDocument",
    "Page",
    "PageType",
    "SourceArtifact",
]


class DocumentKind(StrEnum):
    """Kinds of versioned source document accepted by the V1 ingestion path."""

    ARCHITECTURAL = "architectural"
    SHOP = "shop"
    SCHEDULE = "schedule"
    PRODUCT_SPEC = "product_spec"


DOCUMENT_KIND_VALUES = ", ".join(f"'{kind.value}'" for kind in DocumentKind)
PAGE_TYPE_VALUES = ", ".join(f"'{page_type.value}'" for page_type in PageType)
SHA256_PATTERN = "^[0-9a-f]{64}$"


class SourceArtifact(Base, TimestampedUUID, Immutable):
    """Persisted form of the storage layer's immutable ``StoredArtifact`` contract."""

    __tablename__ = "source_artifacts"

    storage_key: Mapped[str] = mapped_column(String(1000), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int]
    backend_version_id: Mapped[str | None] = mapped_column(String(500), default=None)

    __table_args__ = (
        CheckConstraint("storage_key <> ''", name="source_artifact_storage_key"),
        CheckConstraint(f"sha256 ~ '{SHA256_PATTERN}'", name="source_artifact_sha256"),
        CheckConstraint("size >= 0", name="source_artifact_size"),
        UniqueConstraint("id", "sha256"),
    )


class Document(Base, TimestampedUUID):
    """Logical document identity shared by all uploads of that document — across every revision.

    **The docstring used to be a claim the schema contradicted.** This row carried
    `package_revision_id`, so a drawing belonged to exactly one revision, and "identity shared by all
    uploads" could not be true of it. Nothing needed a drawing to exist in two revisions until
    supersede (#211), and then it did: a revision holding only the changed sheet runs its checks
    against a partial drawing set, and the drawings that were absent produce no failures — which reads
    as no problems (`AGENTS.md` §2.2). ADR-0018 moved identity here, to the package.

    Which revisions include which version of this document is recorded by `PackageRevisionDocument`,
    not by this row. That is what lets a superseding revision share a drawing with its predecessor
    without copying anything: one more link row, the same bytes, no second artifact.
    """

    __tablename__ = "documents"

    package_id: Mapped[UUID] = mapped_column(ForeignKey("packages.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(32))

    __table_args__ = (
        CheckConstraint(f"kind IN ({DOCUMENT_KIND_VALUES})", name="document_kind"),
        # So `package_revision_documents` can resolve a document *and* its package in one key. See
        # `PackageRevisionDocument`: resolving one side alone lets a row through whose every value is
        # true and whose combination is not.
        UniqueConstraint("id", "package_id", name="uq_documents_id_package"),
    )


class DocumentVersion(Base, TimestampedUUID, Immutable):
    """One immutable upload, pinned to the exact artifact bytes it represents."""

    __tablename__ = "document_versions"

    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"))
    source_artifact_id: Mapped[UUID]
    sha256: Mapped[str] = mapped_column(String(64))
    page_count: Mapped[int]

    __table_args__ = (
        ForeignKeyConstraint(
            ("source_artifact_id", "sha256"),
            ("source_artifacts.id", "source_artifacts.sha256"),
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"sha256 ~ '{SHA256_PATTERN}'", name="document_version_sha256"),
        CheckConstraint("page_count >= 0", name="document_version_page_count"),
        UniqueConstraint("document_id", "sha256"),
        # Kept, and ADR-0018 gives it the rationale it never had. Under the membership model a drawing
        # is carried into a later revision by one link row and no new version, so this never obstructs
        # — and it now means something: a set of bytes is registered as a document version exactly
        # once. Dropping it (the option not taken) would have permitted two versions of identical bytes
        # with nothing recording which of them a finding meant.
        UniqueConstraint("source_artifact_id"),
        # So a membership row can resolve a version *and* the document it belongs to in one key.
        UniqueConstraint("id", "document_id", name="uq_document_versions_id_document"),
    )


class PackageRevisionDocument(Base, TimestampedUUID):
    """Which document versions one package revision is composed of (ADR-0018, #366).

    An association table rather than a column on either side, for the reason `ApprovedFinding` is one:
    a revision's contents are a *set*, each member a foreign key to a server-side row. Before this,
    membership was implied by `Document.package_revision_id` — which meant a drawing could belong to
    only one revision, and a superseding revision could not include a sheet that had not changed.

    **Not `Immutable`, and the reason is a decision rather than an oversight.** A revision is *assembled*
    before it is worked: drawings arrive one at a time, and a mis-uploaded sheet re-uploaded a minute
    later is ordinary use, not tampering. So while the revision is in `CREATED`, `UPLOADING` or
    `UPLOADED`, its set may change. From `INGESTING` onward it is frozen, because from that point
    something has read it — and *"that drawing wasn't in the set we reviewed"* must not be a row anybody
    can edit afterwards (`AGENTS.md` §2.7). Changing a frozen set means superseding the revision, which
    is #211.

    A blanket `Immutable` would have been simpler and wrong in the other direction: correcting a typo
    before anything had run would have required a whole new package revision. The freeze is therefore a
    trigger that reads the revision's state — `gv_reject_frozen_revision_documents` in `0017` — rather
    than the marker that refuses every write. `ASSEMBLY_STATES` in `app/lifecycle/states.py` is the one
    place that list lives; a test asserts the migration's literal copy still matches it.
    """

    __tablename__ = "package_revision_documents"

    package_revision_id: Mapped[UUID] = mapped_column(index=True)
    package_id: Mapped[UUID] = mapped_column(index=True)
    """Resolved against **both** the revision and the document below.

    This column is the whole reason the constraints work, and it was nearly wrong. Resolving only the
    document side — checking that this document belongs to this package — admits a row in which every
    value is individually true and the combination is a lie: the document really does belong to
    package 2, and the revision really does belong to package 1. Prototyped against PostgreSQL while
    ADR-0018 was still in draft, and that insert succeeded. Naming the same `package_id` in the
    revision's key too is what refuses it.
    """

    document_id: Mapped[UUID] = mapped_column(index=True)
    document_version_id: Mapped[UUID] = mapped_column(index=True)
    """The exact version this revision includes. A finding cites a version, so this is what ties a
    revision's checks to the bytes they were run against."""

    __table_args__ = (
        ForeignKeyConstraint(
            ["package_revision_id", "package_id"],
            ["package_revisions.id", "package_revisions.package_id"],
            name="fk_revision_documents_revision_package",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["document_id", "package_id"],
            ["documents.id", "documents.package_id"],
            name="fk_revision_documents_document_package",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["document_version_id", "document_id"],
            ["document_versions.id", "document_versions.document_id"],
            name="fk_revision_documents_version_document",
            ondelete="RESTRICT",
        ),
        # At most one version of any drawing per revision. Without it a revision holds v1 and v2 of the
        # same sheet, its checks run against both, and no reader can say which version a finding meant.
        UniqueConstraint(
            "package_revision_id",
            "document_id",
            name="uq_revision_documents_one_version_per_document",
        ),
    )


class Page(Base, TimestampedUUID, Immutable):
    """One B6 page-manifest record plus ambiguity-preserving B11 revision fields."""

    __tablename__ = "pages"

    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="RESTRICT")
    )
    index: Mapped[int]
    content_hash: Mapped[str] = mapped_column(String(64))
    width_pt: Mapped[Decimal] = mapped_column(Numeric())
    height_pt: Mapped[Decimal] = mapped_column(Numeric())
    rotation: Mapped[int]
    has_vector_text: Mapped[bool]
    render_failed: Mapped[bool] = mapped_column(default=False)
    sheet_number: Mapped[str | None] = mapped_column(String(100), default=None)
    page_type: Mapped[str | None] = mapped_column(String(32), default=None)
    revision_label: Mapped[str | None] = mapped_column(String(100), default=None)
    revision_date_raw: Mapped[str | None] = mapped_column(String(100), default=None)
    revision_date_interpretations: Mapped[list[str] | None] = mapped_column(JSONB, default=None)
    revision_sequence_index: Mapped[int | None] = mapped_column(default=None)

    __table_args__ = (
        CheckConstraint("index >= 0", name="page_index"),
        CheckConstraint(f"content_hash ~ '{SHA256_PATTERN}'", name="page_content_hash"),
        CheckConstraint("width_pt > 0", name="page_width"),
        CheckConstraint("height_pt > 0", name="page_height"),
        CheckConstraint("rotation IN (0, 90, 180, 270)", name="page_rotation"),
        CheckConstraint(
            f"page_type IS NULL OR page_type IN ({PAGE_TYPE_VALUES})",
            name="page_type",
        ),
        CheckConstraint(
            "revision_sequence_index IS NULL OR revision_sequence_index >= 0",
            name="page_revision_sequence",
        ),
        UniqueConstraint("document_version_id", "index"),
    )
