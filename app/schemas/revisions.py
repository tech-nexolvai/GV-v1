"""Request and response shapes for a job's revisions (#1180).

A revision is the vendor's resubmission of the same job. These shapes start one, list them, and say
what changed between two. None of them carries a verdict: an outcome here is the stored result of a
check run, read back for display, never something a caller sends.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models import PackageState
from app.schemas.packages import SHA256_PATTERN, PresignedUpload
from app.schemas.visual_ui import ExactValueOut
from verdict.outcomes import Outcome

DrawingKind = Literal["architectural", "shop"]


class RevisionFile(BaseModel):
    """One re-issued drawing, declared by its bytes' hash before it is uploaded."""

    model_config = ConfigDict(extra="forbid")

    sha256: str = Field(
        pattern=SHA256_PATTERN,
        description="The SHA-256 of the file, lowercase hex. Hash it locally first, as for any upload.",
    )


class RevisionStart(BaseModel):
    """Start the next revision of a review: the vendor's new shop drawing, and optionally a new
    architect's drawing. A drawing not given is reused from the previous revision unchanged."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(
        ge=1,
        description=(
            "The newest revision the screen shows. If someone else started a revision since, the "
            "request is refused (409) rather than starting a second one. Send the newest revision's "
            "own number to finish uploading a revision that was started but not read yet."
        ),
    )
    shop: RevisionFile = Field(description="The re-issued shop drawing. Required.")
    architectural: RevisionFile | None = Field(
        default=None,
        description="A new architect's drawing. Leave it out to keep the previous one.",
    )
    note: str | None = Field(
        default=None,
        max_length=300,
        description="Why this revision was started, in the reviewer's words. Optional.",
    )


class RevisionUpload(BaseModel):
    """What to do for one drawing of the new revision."""

    kind: DrawingKind
    document_id: UUID
    status: Literal["upload", "already_stored", "unchanged"] = Field(
        description=(
            "`upload`: send the bytes to `ticket`, then confirm them at "
            "`/documents/{document_id}/confirm` as for any upload. `already_stored`: these exact "
            "bytes were uploaded before for this drawing; the revision already uses them, nothing "
            "to send. `unchanged`: the same file as the previous revision; it is reused."
        )
    )
    ticket: PresignedUpload | None = Field(
        default=None, description="Only when `status` is `upload`."
    )


class RevisionDrawing(BaseModel):
    """One drawing a revision holds."""

    kind: str
    document_id: UUID
    document_version_id: UUID
    sha256: str
    file_name: str | None
    page_count: int | None
    same_as_revision: int | None = Field(
        description=(
            "The earlier revision number holding these exact bytes for this drawing, when it was "
            "carried forward or uploaded again unchanged; null for a new file."
        )
    )


class RevisionOut(BaseModel):
    """One revision of a review."""

    revision_id: UUID
    revision_number: int
    state: PackageState
    created_at: datetime
    current: bool = Field(description="True for the newest revision, the one the review works on.")
    read_only: bool = Field(
        description=(
            "True for an earlier revision: it was replaced by a newer one, and nothing more can be "
            "recorded on it. It stays visible as it was."
        )
    )
    supersedes_revision_number: int | None
    superseded_by_revision_number: int | None
    signed_off_at: datetime | None = Field(description="When it was signed off, if it was.")
    signed_off_by: str | None
    drawings: tuple[RevisionDrawing, ...]


class RevisionsOut(BaseModel):
    package_id: UUID
    current_revision_number: int
    can_start_revision: bool = Field(
        description="Whether the caller may start (or finish uploading) a revision: the upload permission."
    )
    unfinished_revision: int | None = Field(
        description=(
            "The newest revision's number when it was started but its re-issued drawing was not "
            "uploaded and read yet; null otherwise. The screen offers to finish it."
        )
    )
    items: tuple[RevisionOut, ...] = Field(description="Oldest first.")


class RevisionStarted(BaseModel):
    """The new revision, and what to upload into it."""

    revision: RevisionOut
    uploads: tuple[RevisionUpload, ...]
    carried_values: int = Field(
        description=(
            "How many of the job's own values (for example a sink's cut-sheet size) were carried "
            "from the previous revision. Values read off the drawing are not carried: the drawing "
            "changed."
        )
    )
    resumed: bool = Field(
        default=False,
        description=(
            "True when this finished uploading a revision that was started earlier but never read, "
            "instead of starting another one."
        ),
    )
    next_step: str = Field(description="What the reviewer does next, in plain words.")


PageChange = Literal["added", "removed", "changed", "moved", "unchanged"]


class PageChangeOut(BaseModel):
    """One page of one drawing, compared between the two revisions by its content hash."""

    kind: str
    document_id: UUID
    file_name: str | None
    page_number: int = Field(
        description="The page's number in the newer revision (or the earlier, if removed)."
    )
    before_page_number: int | None = Field(
        default=None,
        description="Where the same page was in the earlier revision, when it was paired with one.",
    )
    change: PageChange = Field(
        description=(
            "`moved`: the same page bytes at another page number. Pages are paired by their content "
            "first, then by sheet number, then by position."
        )
    )


class DrawingChangeOut(BaseModel):
    """One drawing compared between the two revisions."""

    kind: str
    document_id: UUID
    file_name: str | None
    same_file: bool = Field(description="True when both revisions hold the exact same bytes.")
    compared: bool = Field(
        description=(
            "False when either side has not been read yet, so its pages are not known. The pages "
            "are then not compared rather than guessed."
        )
    )
    reason: str | None = Field(default=None, description="Why the pages were not compared.")
    pages: tuple[PageChangeOut, ...]


class CountertopSideOut(BaseModel):
    """One countertop row's stored result on one side."""

    outcome: Outcome | None = Field(
        description="The stored result of the last check run; null when no check has run on it."
    )
    needs_decision: bool
    printed_overall: ExactValueOut | None
    expected_total: ExactValueOut | None
    delta: ExactValueOut | None


class CountertopChangeOut(BaseModel):
    """One countertop row, by page and row label, before and after."""

    page_number: int
    label: str
    change: Literal["same", "changed", "new", "gone"]
    before: CountertopSideOut | None
    after: CountertopSideOut | None


class RevisionComparisonOut(BaseModel):
    """What changed between two revisions of a review."""

    package_id: UUID
    before_revision_number: int
    after_revision_number: int
    drawings: tuple[DrawingChangeOut, ...]
    pages_added: int
    pages_removed: int
    pages_changed: int
    pages_unchanged: int
    countertops: tuple[CountertopChangeOut, ...]
    countertops_read: bool = Field(
        description=(
            "False while the newer revision has no countertop rows yet (not read). The countertop "
            "list is then empty rather than reporting every row as gone."
        )
    )
    countertops_checked: bool = Field(
        description=(
            "False while the newer revision has no check results yet: the countertop list then "
            "shows only the earlier results."
        )
    )
