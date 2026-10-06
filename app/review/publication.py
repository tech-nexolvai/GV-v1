"""Read the exact approval binding without loading a renderer."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.review import Approval, ApprovedFinding


class UnapprovedContent(Exception):
    """Vendor mode was asked to render something no reviewer signed off.

    Raised rather than filtered. A vendor document quietly missing a finding is indistinguishable
    from one where that check passed, and the person who would have noticed is the reviewer whose
    approval was being worked around.
    """


@dataclass(frozen=True, slots=True)
class SignedOff:
    """One reviewer's sign-off, and exactly which findings it covered.

    `finding_ids` comes from `approved_findings`, whose composite foreign keys resolve each link
    against both the approval's revision and the finding's. An approval therefore cannot claim a
    finding from another package, which matters here because this is the record a vendor document
    cites.
    """

    approval_id: UUID
    package_revision_id: UUID
    approved_by: str
    approved_at: datetime
    finding_ids: frozenset[UUID]


def sign_off(session: Session, package_revision_id: UUID) -> SignedOff:
    """Read the approval for this package revision, or refuse.

    Refuses rather than returning `None`: every caller of this is about to decide whether content
    may leave, and an optional return invites the one line of code — `if approval:` — that turns a
    missing sign-off into a silent skip.

    A revision approved more than once takes the latest, which is the sign-off in force. Earlier
    ones stay in the table; `Approval` is immutable precisely so the history of who accepted what
    survives a re-review.

    Two approvals sharing the newest timestamp raise rather than resolve. `created_at` is generated
    per row, so a tie needs a clock coarse enough to stamp two flushes identically — but if one ever
    happens there is no fact that says which sign-off is in force, and the available tiebreak is a
    random UUID. Picking by UUID would attribute a vendor document to whichever approver's id sorted
    higher, which is a decision dressed up as an ordering.
    """
    newest = session.scalars(
        select(Approval)
        .where(Approval.package_revision_id == package_revision_id)
        .order_by(Approval.created_at.desc())
        .limit(2)
    ).all()
    approval = newest[0] if newest else None

    if len(newest) == 2 and newest[0].created_at == newest[1].created_at:
        raise UnapprovedContent(
            f"package revision {package_revision_id} has two approvals recorded at "
            f"{newest[0].created_at.isoformat()} — {newest[0].approved_by} and "
            f"{newest[1].approved_by} — and nothing says which is in force. A vendor document names "
            "its approver, so guessing here would attribute it to a person who may not have been "
            "the one who signed."
        )

    if approval is None:
        raise UnapprovedContent(
            f"package revision {package_revision_id} has not been approved, so nothing about it "
            "may be sent to a vendor. ADR-0010: no computed dimension reaches a vendor without "
            "reviewer sign-off. Render the internal report for review first."
        )

    covered = session.scalars(
        select(ApprovedFinding.finding_id).where(ApprovedFinding.approval_id == approval.id)
    ).all()

    return SignedOff(
        approval_id=approval.id,
        package_revision_id=package_revision_id,
        approved_by=approval.approved_by,
        approved_at=approval.created_at,
        finding_ids=frozenset(covered),
    )
