"""The architect reader's page notes, written and read without reading a drawing (#1163).

Why the architect reader read nothing on a page of the architect's own file, or what it left out:
no view found, a title that is no view, a stamp holding no drawing, ink left out of every view, a
view refused because the stored view of its number sits elsewhere, a page that could not be read.
Each is one append-only `ArchitectPageNote` under the run that read the page (`0082`), so nothing
the reader declined is lost when the stage's result is reduced to a page count.

**No extraction imports**, like `workflow/architect_pairing_records.py`: later phases and the API
read these (`architect_page_notes_for`), and `tests/api/test_no_heavy_work.py` keeps `app/api/`
away from anything that renders or reads a PDF. Imports: SQLAlchemy, `app.models`, the standard
library.

Source: issue #1163 · Verification: `tests/workflow/test_architect_file_reader.py`
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import PackageRevisionDocument, Page
from app.models.evidence import ARCHITECT_PAGE_NOTE_KINDS, ArchitectPageNote

__all__ = [
    "ARCHITECT_PAGE_NOTE_KINDS",
    "PAGE_UNREADABLE",
    "VIEW_REFUSED",
    "PageNoteRecord",
    "architect_page_notes_for",
    "record_architect_page_note",
]

#: Written by the stage, not the reader: a view refused by the region check, a page unreadable.
VIEW_REFUSED: Final = "view_refused"
PAGE_UNREADABLE: Final = "page_unreadable"

_TEXT_LIMIT: Final = 500


def record_architect_page_note(
    session: Session,
    *,
    extraction_run_id: UUID,
    document_version_id: UUID,
    page_id: UUID,
    kind: str,
    text: str,
) -> ArchitectPageNote:
    """Append one note. `kind` must be one of `ARCHITECT_PAGE_NOTE_KINDS`."""
    if kind not in ARCHITECT_PAGE_NOTE_KINDS:
        raise ValueError(f"kind must be one of {ARCHITECT_PAGE_NOTE_KINDS}, not {kind!r}")
    if not text.strip():
        raise ValueError("a page note needs its text")
    note = ArchitectPageNote(
        extraction_run_id=extraction_run_id,
        document_version_id=document_version_id,
        page_id=page_id,
        kind=kind,
        text=text[:_TEXT_LIMIT],
    )
    session.add(note)
    return note


@dataclass(frozen=True, slots=True)
class PageNoteRecord:
    """One stored note, with the page it is about."""

    document_version_id: UUID
    page_id: UUID
    page_index: int
    extraction_run_id: UUID
    kind: str
    text: str
    created_at: datetime


def architect_page_notes_for(
    session: Session, package_revision_id: UUID
) -> tuple[PageNoteRecord, ...]:
    """Every architect page note on the revision's documents, by document, page, then time."""
    rows = session.execute(
        select(ArchitectPageNote, Page.index)
        .join(Page, Page.id == ArchitectPageNote.page_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == ArchitectPageNote.document_version_id,
        )
        .where(PackageRevisionDocument.package_revision_id == package_revision_id)
        .order_by(
            ArchitectPageNote.document_version_id,
            Page.index,
            ArchitectPageNote.created_at,
            ArchitectPageNote.id,
        )
    ).all()
    return tuple(
        PageNoteRecord(
            document_version_id=note.document_version_id,
            page_id=note.page_id,
            page_index=page_index,
            extraction_run_id=note.extraction_run_id,
            kind=note.kind,
            text=note.text,
            created_at=note.created_at,
        )
        for note, page_index in rows
    )
