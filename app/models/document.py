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
    """Logical document identity shared by all uploads of that document."""

    __tablename__ = "documents"

    package_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("package_revisions.id", ondelete="RESTRICT")
    )
    kind: Mapped[str] = mapped_column(String(32))

    __table_args__ = (CheckConstraint(f"kind IN ({DOCUMENT_KIND_VALUES})", name="document_kind"),)


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
        UniqueConstraint("source_artifact_id"),
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
