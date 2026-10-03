"""A suggested part becomes a drawing item only when a person confirms it (#852).

The computer may suggest that a box on an elevation is a cabinet, a filler or a countertop
(`PartProposal`). Only a person's decision on that suggestion (`PartConfirmation`) does anything with
it: confirming makes the `drawing_items` row and, when the person kept a code, its `item_identifiers`
row; withdrawing makes nothing.

**`confirm_part` is the only writer of either table.** Matching reads `drawing_items` today, and the
plan on #748 has the countertop checks read it too, so whatever writes it decides what a check is
about. Keeping that to one function, called for a person's decision, is what "a suggestion is not a
part" rests on, and `tests/db/test_drawing_models.py` fails if any other module outside `tests/`
constructs or inserts into either table.

**No extraction imports, on purpose**, as in `workflow/view_roles.py`: the decision will be made
through the API, and `tests/api/test_no_heavy_work.py` keeps `app/api/` away from anything that
reads a PDF.

Source: issue #852; #748 plan, step 2. Verification: tests/db/test_drawing_models.py.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from app.audit.events import AuditCategory, emit
from app.models import (
    DrawingItem,
    ItemIdentifier,
    PartConfirmation,
    PartDecision,
    PartProposal,
)
from vocabulary.part_kinds import PartKind

__all__ = ["CODE_IDENTIFIER_KIND", "confirm_part", "current_decision", "withdraw_part"]

#: The `item_identifiers.kind` a confirmed code is stored under. A cabinet code names a model, and
#: `catalogue` is the kind that says so: every unit of that model carries it, so two parts sharing a
#: code is ordinary rather than a contradiction.
CODE_IDENTIFIER_KIND: Final = "catalogue"


def current_decision(session: Session, proposal: PartProposal) -> PartConfirmation | None:
    """The decision on this suggestion that nothing has replaced, or `None` if nobody has decided.

    The schema allows at most one such row per suggestion (`PartConfirmation`), so this asks for one
    and would raise rather than pick if that ever stopped being true.
    """
    later = aliased(PartConfirmation)
    return session.execute(
        select(PartConfirmation).where(
            PartConfirmation.part_proposal_id == proposal.id,
            ~exists().where(later.supersedes_id == PartConfirmation.id),
        )
    ).scalar_one_or_none()


def confirm_part(
    session: Session,
    *,
    proposal: PartProposal,
    kind: PartKind,
    code: str | None,
    actor: str,
) -> PartConfirmation:
    """A person saying a suggested part is real. **The only code that writes `drawing_items` or
    `item_identifiers`.**

    Makes a new item on the suggestion's view, with the suggestion's extent and the generic type for
    `kind`, and leaves `corroborated` at its default of `False`: the confirmation row is the
    authority, as it is for a view's role. A `code` becomes a `catalogue` identifier exactly as given
    — never trimmed, never decoded into a width. `kind` and `code` are what the person confirmed, and
    may differ from what was suggested.

    The decision replaces whichever one was current, so a correction is a new row and a new item, and
    the history of who said what stays.
    """
    if not isinstance(kind, PartKind):
        raise TypeError("kind must be a PartKind")
    if code is not None and (not isinstance(code, str) or not code.strip()):
        raise ValueError("a code must be the text printed on the part, or None for no code")
    _require_actor(actor)

    replaced = current_decision(session, proposal)
    item = DrawingItem(
        drawing_view_id=proposal.drawing_view_id,
        item_type=kind.item_type.value,
        extent=proposal.extent,
    )
    session.add(item)
    session.flush()
    if code is not None:
        session.add(
            ItemIdentifier(
                drawing_item_id=item.id, kind=CODE_IDENTIFIER_KIND, value_as_printed=code
            )
        )
    confirmation = PartConfirmation(
        part_proposal_id=proposal.id,
        supersedes_id=None if replaced is None else replaced.id,
        decision=PartDecision.CONFIRMED.value,
        kind=kind.value,
        code_as_printed=code,
        drawing_item_id=item.id,
        confirmed_by=actor,
    )
    return _record(session, confirmation, actor)


def withdraw_part(session: Session, *, proposal: PartProposal, actor: str) -> PartConfirmation:
    """A person saying a suggestion is not a part, or no longer is. Writes no item.

    An item made by an earlier confirmation keeps its row and stops being current, because the
    decision that made it has been replaced.
    """
    _require_actor(actor)
    replaced = current_decision(session, proposal)
    confirmation = PartConfirmation(
        part_proposal_id=proposal.id,
        supersedes_id=None if replaced is None else replaced.id,
        decision=PartDecision.WITHDRAWN.value,
        confirmed_by=actor,
    )
    return _record(session, confirmation, actor)


def _require_actor(actor: str) -> None:
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("a decision on a part needs the person who made it")


def _record(session: Session, confirmation: PartConfirmation, actor: str) -> PartConfirmation:
    session.add(confirmation)
    session.flush()
    # Audited in the same transaction, like every reviewer action: whether a part exists is a
    # person's call, and the audit trail is where somebody looks for who made it.
    emit(
        session,
        category=AuditCategory.REVIEW_ACTION,
        actor=actor,
        target_id=confirmation.id,
        target_type="part_confirmation",
    )
    session.flush()
    return confirmation
