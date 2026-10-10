"""Which drawing on a sheet is the architect's and which the vendor's: suggested, then confirmed (#710).

A package is compared as one architectural drawing against one shop drawing. With two files, the
upload says which is which (`Document.kind`). With one combined sheet, each drawing on it becomes a
`DrawingView`, the sheet's own labels give a *suggestion* (`ViewRoleProposal`), and a confirmation
(`ViewRoleConfirmation`) sets `DrawingView.role`: a person's, or — since #1052 (decision D2), and
only behind `GV_ARCHITECT_READER_ENABLED` — code's, when the exact printed heading and the drawing's
own content agree (`confirm_view_role_by_code`). A person's confirmation always wins. Nothing here reads a role from where a
drawing sits, or from page order; the document kind counts only as one of code's two judgments on a
file uploaded as the architect's own drawings, beside the drawing's content (#1163,
`CODE_DOCUMENT_CONFIRMER`), never alone.

**No extraction imports, on purpose.** The confirmation is made through the API, and
`tests/api/test_no_heavy_work.py` keeps `app/api/` away from anything that renders or reads a PDF.
The stage that reads the sheet passes plain values in.

Source: issue #710. Verification: tests/workflow/test_view_roles.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.events import AuditCategory, emit
from app.models import (
    DrawingView,
    PackageRevisionDocument,
    Page,
    ViewRole,
    ViewRoleConfirmation,
    ViewRoleProposal,
)

__all__ = [
    "CODE_CONFIRMER",
    "CODE_CONFIRMERS",
    "CODE_CONTENT_CONFIRMER",
    "CODE_DOCUMENT_CONFIRMER",
    "CONTENT_VIEW_SOURCE",
    "PANEL_SOURCE",
    "RevisionView",
    "confirm_view_role",
    "confirm_view_role_by_code",
    "content_view_tag",
    "panel_tag",
    "record_content_view",
    "record_panel_view",
    "revision_views",
]

#: What made a suggestion. Versioned so a later, different reader writes different rows.
PANEL_SOURCE = "panel-heading-label/v1"

#: Who a code confirmation names (#1052, decision D2): code, from the exact printed heading and the
#: drawing's own content agreeing. Never a person's name, so the record says plainly no one looked.
CODE_CONFIRMER = "code:panel-heading+drawing-content/v1"

#: Who a code confirmation names on a page that prints no heading at all, where the role came from
#: the content of both drawings, each clearly one side's (#1052, `extraction/architect/views.
#: decide_without_headings`).
CODE_CONTENT_CONFIRMER = "code:content-of-both-drawings/v1"

#: Who a code confirmation names on the architect's own drawing set — an ARCHITECTURAL upload whose
#: bytes are not the shop file's — where the document's kind and the drawing's own content agree
#: (#1163, ADR-0020 decision 3, `extraction/architect/views.judge_by_document`).
CODE_DOCUMENT_CONFIRMER = "code:document-kind+drawing-content/v1"

#: Every name code confirms a role under: never a person's, so a reader of the record (sign-off's
#: "inputs changed" rule, `app/review/approval.py`) can tell no one looked.
CODE_CONFIRMERS = frozenset({CODE_CONFIRMER, CODE_CONTENT_CONFIRMER, CODE_DOCUMENT_CONFIRMER})
_CODE_CONFIRMERS = CODE_CONFIRMERS

#: What made the suggestion for a view drawn as the page's own content (#1163): its printed title
#: over its scale note, on a file uploaded as the architect's drawings.
CONTENT_VIEW_SOURCE = "content-view-title/v1"


def panel_tag(annotation_index: int) -> str:
    """The name a drawing view gets before its printed tag has been read.

    `DrawingView.tag` is meant to be what the sheet prints (`A`, `8`). On the client's sheets that tag
    is drawn as outlines no reader sees yet, so a view is named after the stamp it came from — stable
    across re-reads, unique on the page — until the printed tag can replace it.
    """
    return f"panel-{annotation_index}"


def content_view_tag(number: int) -> str:
    """The name of a view drawn as the page's own content (#1163): its page-local number, counted
    over the page's printed view titles top to bottom, then left to right — stable across re-reads
    of the same file, unique on the page, and never a pasted drawing's `panel-<n>`."""
    return f"view-{number}"


def record_content_view(
    session: Session,
    *,
    page_id: UUID,
    number: int,
    stored_points: Sequence[tuple[Decimal, Decimal]],
    title: str | None,
    reason: str,
) -> DrawingView:
    """Find or create the view for one drawing printed as the page's own content (#1163).

    Its suggestion is the architect's — the file was uploaded as the architect's drawings — with the
    printed title as its heading. **Never sets the role**, exactly like `record_panel_view`.
    """
    tag = content_view_tag(number)
    view = session.execute(
        select(DrawingView).where(DrawingView.page_id == page_id, DrawingView.tag == tag)
    ).scalar_one_or_none()
    if view is None:
        view = DrawingView(
            page_id=page_id,
            tag=tag,
            region={"space": "stored", "points": [[str(x), str(y)] for x, y in stored_points]},
        )
        session.add(view)
        session.flush()
    heading = None if title is None else title[:200]
    latest = session.execute(
        select(ViewRoleProposal)
        .where(
            ViewRoleProposal.drawing_view_id == view.id,
            ViewRoleProposal.source == CONTENT_VIEW_SOURCE,
        )
        .order_by(ViewRoleProposal.created_at.desc(), ViewRoleProposal.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is None or (latest.proposed_role, latest.heading) != (ViewRole.ARCH.value, heading):
        session.add(
            ViewRoleProposal(
                drawing_view_id=view.id,
                proposed_role=ViewRole.ARCH.value,
                heading=heading,
                reason=reason[:500],
                source=CONTENT_VIEW_SOURCE,
            )
        )
        session.flush()
    return view


def record_panel_view(
    session: Session,
    *,
    page_id: UUID,
    annotation_index: int,
    stored_points: Sequence[tuple[Decimal, Decimal]],
    proposed_role: str | None,
    heading: str | None,
    reason: str,
) -> DrawingView:
    """Find or create the view for one drawing, and file what its label suggests.

    **Never sets the role.** A view created here has `role = NULL`, and a view that already has a
    confirmed role keeps it. A re-read that suggests the same thing again adds nothing.
    """
    if proposed_role is not None and proposed_role not in {role.value for role in ViewRole}:
        raise ValueError(f"proposed_role must be one of {[r.value for r in ViewRole]} or None")
    tag = panel_tag(annotation_index)
    view = session.execute(
        select(DrawingView).where(DrawingView.page_id == page_id, DrawingView.tag == tag)
    ).scalar_one_or_none()
    if view is None:
        view = DrawingView(
            page_id=page_id,
            tag=tag,
            region={"space": "stored", "points": [[str(x), str(y)] for x, y in stored_points]},
        )
        session.add(view)
        session.flush()

    latest = session.execute(
        select(ViewRoleProposal)
        .where(ViewRoleProposal.drawing_view_id == view.id, ViewRoleProposal.source == PANEL_SOURCE)
        .order_by(ViewRoleProposal.created_at.desc(), ViewRoleProposal.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is None or (latest.proposed_role, latest.heading) != (proposed_role, heading):
        session.add(
            ViewRoleProposal(
                drawing_view_id=view.id,
                proposed_role=proposed_role,
                heading=heading,
                reason=reason,
                source=PANEL_SOURCE,
            )
        )
        session.flush()
    return view


def confirm_view_role(
    session: Session, *, view: DrawingView, role: ViewRole, actor: str
) -> ViewRoleConfirmation:
    """A person saying which drawing this is. With `confirm_view_role_by_code`, the only code that
    sets `DrawingView.role`; a person's confirmation is always written and always wins.

    The confirmation is recorded first and the view carries the latest one, so a correction is a new
    row and the history of who said what stays.
    """
    if not isinstance(role, ViewRole):
        raise TypeError("role must be a ViewRole")
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("a confirmation needs the person who made it")
    confirmation = ViewRoleConfirmation(
        drawing_view_id=view.id, role=role.value, confirmed_by=actor
    )
    session.add(confirmation)
    view.role = role.value
    session.flush()
    # Audited in the same transaction, like every reviewer action: the role decides which side of
    # every comparison this drawing's items land on.
    emit(
        session,
        category=AuditCategory.REVIEW_ACTION,
        actor=actor,
        target_id=confirmation.id,
        target_type="view_role_confirmation",
    )
    session.flush()
    return confirmation


def confirm_view_role_by_code(
    session: Session,
    *,
    view: DrawingView,
    role: ViewRole,
    reason: str,
    confirmed_by: str = CODE_CONFIRMER,
) -> ViewRoleConfirmation | None:
    """Code deciding a drawing's role because two independent judgments agree (#1052, D2).

    Called only when the exact printed heading and the drawing's own content (`extraction/architect/
    views.py`) give the same role. Recorded exactly like a person's confirmation, append-only, with
    `confirmed_by` = `CODE_CONFIRMER` and an audit event naming it, so the record says code decided.

    **A person always wins.** Nothing is written when the view already has any confirmation — a
    person's, or code's from an earlier read — so code never overrides anyone and never repeats
    itself; and a person confirming afterwards writes a newer row, which the view then carries.
    Returns the new confirmation, or `None` when one already existed.

    `confirmed_by` names which code decided: `CODE_CONFIRMER` (heading and content agreeing),
    `CODE_CONTENT_CONFIRMER` (a page with no heading, the content of both drawings) or
    `CODE_DOCUMENT_CONFIRMER` (the architect's own file: its kind and the drawing's content
    agreeing, #1163). Nothing else: a person's confirmation goes through `confirm_view_role`.
    """
    if not isinstance(role, ViewRole):
        raise TypeError("role must be a ViewRole")
    if confirmed_by not in _CODE_CONFIRMERS:
        raise ValueError(f"confirmed_by must be one of {sorted(_CODE_CONFIRMERS)}")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a code confirmation needs the reason both judgments gave")
    existing = session.execute(
        select(ViewRoleConfirmation.id)
        .where(ViewRoleConfirmation.drawing_view_id == view.id)
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None or view.role is not None:
        return None
    confirmation = ViewRoleConfirmation(
        drawing_view_id=view.id, role=role.value, confirmed_by=confirmed_by
    )
    session.add(confirmation)
    view.role = role.value
    session.flush()
    emit(
        session,
        category=AuditCategory.EVIDENCE_QUALIFICATION,
        actor=confirmed_by,
        target_id=confirmation.id,
        target_type="view_role_confirmation",
    )
    session.flush()
    return confirmation


@dataclass(frozen=True, slots=True)
class RevisionView:
    """One drawing view of a revision, with its page, confirmed role and latest suggestion."""

    view: DrawingView
    page_index: int
    proposal: ViewRoleProposal | None


def revision_views(session: Session, package_revision_id: UUID) -> tuple[RevisionView, ...]:
    """Every drawing view on the revision's pages, in page order, each with its latest suggestion."""
    rows = session.execute(
        select(DrawingView, Page.index)
        .join(Page, Page.id == DrawingView.page_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == Page.document_version_id,
        )
        .where(PackageRevisionDocument.package_revision_id == package_revision_id)
        .order_by(Page.index, DrawingView.tag)
    ).all()
    result: list[RevisionView] = []
    for view, page_index in rows:
        proposal = session.execute(
            select(ViewRoleProposal)
            .where(ViewRoleProposal.drawing_view_id == view.id)
            .order_by(ViewRoleProposal.created_at.desc(), ViewRoleProposal.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        result.append(RevisionView(view=view, page_index=page_index, proposal=proposal))
    return tuple(result)
