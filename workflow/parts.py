"""A suggested part becomes a drawing item only when a person confirms it (#852).

The computer may suggest that a box on an elevation is a cabinet, a filler or a countertop
(`PartProposal`). Only a person's decision on that suggestion (`PartConfirmation`) does anything with
it: confirming makes the `drawing_items` row and, when the person kept a code, its `item_identifiers`
row; withdrawing makes nothing.

**`confirm_part` is the only writer of either table.** Matching reads `drawing_items` today, and the
plan on #748 has the countertop checks read it too, so whatever writes it decides what a check is
about. Keeping that to one function, called for a person's decision, is what "a suggestion is not a
part" rests on, and `tests/db/test_drawing_models.py` fails if any other module outside `tests/`
constructs or inserts into either table. Matching reads only the items `live_part_item_ids` names
(#882), so a part a person took back stops being read.

**No extraction imports, on purpose**, as in `workflow/view_roles.py`: the decision is made through
the API (`app/api/drawing_parts.py`), and `tests/api/test_no_heavy_work.py` keeps `app/api/` away
from anything that reads a PDF. The stage that suggests parts (#868) passes plain values to
`record_part_proposal`.

Source: issues #852, #868 and #882; #748 plan, steps 2 to 4. Verification:
tests/db/test_drawing_models.py, tests/workflow/test_part_proposals_route.py,
tests/api/test_drawing_parts.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import Select, exists, select
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

__all__ = [
    "CODE_IDENTIFIER_KIND",
    "confirm_part",
    "current_decision",
    "live_part_item_ids",
    "record_part_proposal",
    "withdraw_part",
]

#: The `item_identifiers.kind` a confirmed code is stored under. A cabinet code names a model, and
#: `catalogue` is the kind that says so: every unit of that model carries it, so two parts sharing a
#: code is ordinary rather than a contradiction.
CODE_IDENTIFIER_KIND: Final = "catalogue"


def record_part_proposal(
    session: Session,
    *,
    drawing_view_id: UUID,
    kind: PartKind,
    extent: Sequence[tuple[Decimal, Decimal]],
    defining_line: tuple[tuple[Decimal, Decimal], tuple[Decimal, Decimal]],
    code_as_printed: str | None,
    code_candidate_id: UUID | None,
    reason: str,
    source: str,
    source_version: str,
) -> PartProposal:
    """File one suggested part, or find the same suggestion already filed. **Writes no item.**

    The same suggestion is the same view, kind, extent, defining line, code, reason and suggester. A
    re-read that suggests it again finds the existing row rather than adding a second one for a
    person to decide on. Which reading the code came from is not part of that: the same code read
    again is a different reading, and the earlier suggestion already names one that holds it.

    Coordinates are stored as text, as a view's region is, so they stay exact.
    """
    if not isinstance(kind, PartKind):
        raise TypeError("kind must be a PartKind")
    stored_extent: dict[str, object] = {
        "space": "stored",
        "points": [[str(x), str(y)] for x, y in extent],
    }
    stored_line: dict[str, object] = {
        "space": "stored",
        "points": [[str(x), str(y)] for x, y in defining_line],
    }
    existing = session.execute(
        select(PartProposal)
        .where(
            PartProposal.drawing_view_id == drawing_view_id,
            PartProposal.kind == kind.value,
            PartProposal.extent == stored_extent,
            PartProposal.defining_line == stored_line,
            PartProposal.code_as_printed.is_not_distinct_from(code_as_printed),
            PartProposal.reason == reason,
            PartProposal.source == source,
            PartProposal.source_version == source_version,
        )
        .order_by(PartProposal.created_at, PartProposal.id)
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    proposal = PartProposal(
        drawing_view_id=drawing_view_id,
        kind=kind.value,
        extent=stored_extent,
        code_as_printed=code_as_printed,
        code_candidate_id=code_candidate_id,
        defining_line=stored_line,
        source=source,
        source_version=source_version,
        reason=reason,
    )
    session.add(proposal)
    session.flush()
    return proposal


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


def live_part_item_ids() -> Select[tuple[UUID | None]]:
    """The `drawing_items` ids a reader may treat as parts: each made by a decision still current.

    A correction or a withdrawal replaces the decision that made an item, and the item keeps its row
    (no role holds `DELETE`), so a reader that took every row would keep reading a part a person had
    taken back. An item no decision made is not here either: outside tests, only `confirm_part`
    writes one, so such a row is one no person confirmed.

    Returns a query rather than running one, so the caller filters with it in its own statement.
    """
    later = aliased(PartConfirmation)
    return select(PartConfirmation.drawing_item_id).where(
        PartConfirmation.decision == PartDecision.CONFIRMED.value,
        ~exists().where(later.supersedes_id == PartConfirmation.id),
    )


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
