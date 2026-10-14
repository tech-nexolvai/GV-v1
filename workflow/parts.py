"""A suggested part becomes a drawing item only when it is confirmed (#852, #1244).

The computer may suggest that a box on an elevation is a cabinet, a filler or a countertop
(`PartProposal`). Only a decision on that suggestion (`PartConfirmation`) does anything with it:
confirming makes the `drawing_items` row and, when the person kept a code, its `item_identifiers`
row; withdrawing makes nothing.

**`confirm_part` is the only writer of either table.** Matching reads `drawing_items` today, and the
plan on #748 has the countertop checks read it too, so whatever writes it decides what a check is
about. Keeping that to one function is what "a suggestion is not a part" rests on, and
`tests/db/test_drawing_models.py` fails if any other module outside `tests/` constructs or inserts
into either table. Matching reads only the items `live_part_item_ids` names (#882), so a part
taken back stops being read.

**Who may call it (ADR-0021 and its amendment of 2026-10-14):**

- **a person**, through the Measure page (`app/evidence/parts.py`: `confirm_listed_part`,
  `add_part`), which refuses any actor that names code (`code:`);
- **code, for one narrow case only**: `confirm_part_by_code` confirms one of Bala's *countertop*
  suggestions when two independent judgments agree (`workflow/bala_items.py`: both AI row readers
  named the row, and code places it wholly inside one drawing a confirmation says is the vendor's,
  outlined by its printed overall that its pieces tile). It is recorded under `BALA_CONFIRMER`,
  audited as `EVIDENCE_QUALIFICATION`, never sets `corroborated`, and **a person always wins**: it
  writes nothing on a suggestion anyone has decided, where a person has decided a countertop at
  the same place on the same drawing, or where any live countertop already stands
  (`why_code_may_not_confirm`). A person undoes it by withdrawing it, like any decision.
  `release_contradicted_code_part` takes back only code's own item: when a later read no longer
  agrees, or when a person has since decided a countertop at the same place. Pieces (cabinets,
  fillers) are never confirmed by code (decision 2 of 2026-10-14).

A second guard in `tests/db/test_drawing_models.py` fails if `confirm_part` is called from anywhere
else.

**No extraction imports, on purpose**, as in `workflow/view_roles.py`: the decision is made through
the API (`app/api/drawing_parts.py`), and `tests/api/test_no_heavy_work.py` keeps `app/api/` away
from anything that reads a PDF. The stage that suggests parts (#868) passes plain values to
`record_part_proposal`.

**Where a confirmed part lies** (`PlacedPart`, `live_part`, `live_parts_on`) is read here too, for
everything decided from the parts: the run beneath each countertop (#893) and which reading is each
part's width (#913). It reads outlines and kinds, never a width.

Source: issues #852, #868, #882, #893, #913 and #1244; #748 plan, steps 2 to 6; ADR-0021.
Verification: tests/db/test_drawing_models.py, tests/workflow/test_part_proposals_route.py,
tests/api/test_drawing_parts.py, tests/workflow/test_countertop_runs.py,
tests/workflow/test_bala_automatic.py.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final, cast
from uuid import UUID

from sqlalchemy import Select, exists, select
from sqlalchemy.orm import Session, aliased

from app.audit.events import AuditCategory, emit
from app.models import (
    CountertopRunDecision,
    DrawingItem,
    ItemIdentifier,
    PartConfirmation,
    PartDecision,
    PartProposal,
)
from vocabulary.part_kinds import PartKind

__all__ = [
    "ALREADY_DECIDED",
    "ANOTHER_COUNTERTOP_STANDS",
    "A_PERSONS_RUN_BENEATH",
    "A_PERSONS_RUN_HERE",
    "A_PERSON_DECIDED_HERE",
    "BALA_CONFIRMER",
    "CODE_ACTOR_PREFIX",
    "CODE_IDENTIFIER_KIND",
    "PlacedPart",
    "by_code",
    "check_edge_tolerance",
    "confirm_part",
    "confirm_part_by_code",
    "current_decision",
    "live_part",
    "live_part_item_ids",
    "live_parts_on",
    "outline_box",
    "person_decided_countertop_here",
    "person_run_beneath",
    "persons_part_item_ids",
    "record_part_proposal",
    "release_contradicted_code_part",
    "why_code_may_not_confirm",
    "withdraw_part",
]

#: The `item_identifiers.kind` a confirmed code is stored under. A cabinet code names a model, and
#: `catalogue` is the kind that says so: every unit of that model carries it, so two parts sharing a
#: code is ordinary rather than a contradiction.
CODE_IDENTIFIER_KIND: Final = "catalogue"

#: How every name code decides under begins (`workflow/view_roles.py` uses the same): never a
#: person's, so the record says plainly that no one looked, and the Measure page refuses it.
CODE_ACTOR_PREFIX: Final = "code:"

#: Who a code confirmation of a part names (#1244, ADR-0021 amendment): Bala, on two independent
#: judgments agreeing (`workflow/bala_items.py`). The only code name `confirm_part` accepts.
BALA_CONFIRMER: Final = "code:bala-two-judgments/v1"


def by_code(actor: str) -> bool:
    """Whether a decision was made by code rather than a person."""
    return actor.startswith(CODE_ACTOR_PREFIX)


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
    find_existing: bool = True,
) -> PartProposal:
    """File one suggested part, or find the same suggestion already filed. **Writes no item.**

    The same suggestion is the same view, kind, extent, defining line, code, reason and suggester. A
    re-read that suggests it again finds the existing row rather than adding a second one for a
    person to decide on. Which reading the code came from is not part of that: the same code read
    again is a different reading, and the earlier suggestion already names one that holds it.

    `find_existing=False` files a new row every time, for a suggester that keeps its own record of
    what it filed: Bala links each suggestion to one countertop row and piece (`part_proposal_rows`,
    #1236), and finds its own earlier suggestions through that link instead.

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
    if find_existing:
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
    writes one, so such a row is one nobody confirmed. Code's own confirmation of a countertop
    (`confirm_part_by_code`, #1244) is a current decision like a person's, so its item is here
    until a person or a later read takes it back.

    Returns a query rather than running one, so the caller filters with it in its own statement.
    """
    later = aliased(PartConfirmation)
    return select(PartConfirmation.drawing_item_id).where(
        PartConfirmation.decision == PartDecision.CONFIRMED.value,
        ~exists().where(later.supersedes_id == PartConfirmation.id),
    )


def persons_part_item_ids() -> Select[tuple[UUID | None]]:
    """The live items a person confirmed: `live_part_item_ids` less code's own (#1244).

    Code's countertop is an item a reader may treat as a part, but not a person's word that it is
    one; the countertop checks are scoped only to a person's (`workflow/part_operands.py:
    countertop_scopes`, review of #1248).
    """
    return live_part_item_ids().where(~PartConfirmation.confirmed_by.startswith(CODE_ACTOR_PREFIX))


def confirm_part(
    session: Session,
    *,
    proposal: PartProposal,
    kind: PartKind,
    code: str | None,
    actor: str,
) -> PartConfirmation:
    """A person, or code through `confirm_part_by_code`, saying a suggested part is real. **The only
    code that writes `drawing_items` or `item_identifiers`.**

    Makes a new item on the suggestion's view, with the suggestion's extent and the generic type for
    `kind`, and leaves `corroborated` at its default of `False`: the confirmation row is the
    authority, as it is for a view's role. A `code` becomes a `catalogue` identifier exactly as given
    — never trimmed, never decoded into a width. `kind` and `code` are what the person confirmed, and
    may differ from what was suggested.

    The decision replaces whichever one was current, so a correction is a new row and a new item, and
    the history of who said what stays.

    `actor` is the person, or `BALA_CONFIRMER` when `confirm_part_by_code` calls; any other name
    beginning `code:` is refused, so code has exactly one way in.
    """
    if not isinstance(kind, PartKind):
        raise TypeError("kind must be a PartKind")
    if code is not None and (not isinstance(code, str) or not code.strip()):
        raise ValueError("a code must be the text printed on the part, or None for no code")
    _require_actor(actor)
    if by_code(actor) and actor != BALA_CONFIRMER:
        raise ValueError(
            f"code confirms a part only as {BALA_CONFIRMER!r}, through confirm_part_by_code"
        )

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
    decision that made it has been replaced. Code withdraws only its own item, through
    `release_contradicted_code_part`, as `BALA_CONFIRMER`: with code's name, this refuses unless the
    current decision is code's own confirmation (review of #1248), and refuses when a person has
    confirmed or withdrawn a run beneath its item (re-review of #1248).
    """
    _require_actor(actor)
    if by_code(actor) and actor != BALA_CONFIRMER:
        raise ValueError(f"code withdraws a part only as {BALA_CONFIRMER!r}")
    replaced = current_decision(session, proposal)
    if by_code(actor) and not _codes_own_confirmation(replaced):
        raise ValueError(
            "code withdraws only its own current confirmation; a person's decision, or no "
            "decision, is never code's to take back"
        )
    if by_code(actor) and person_run_beneath(session, replaced):
        raise ValueError(
            "code never takes back a countertop a person confirmed a run beneath: the run is a "
            "person's decision about it"
        )
    confirmation = PartConfirmation(
        part_proposal_id=proposal.id,
        supersedes_id=None if replaced is None else replaced.id,
        decision=PartDecision.WITHDRAWN.value,
        confirmed_by=actor,
    )
    return _record(session, confirmation, actor)


def confirm_part_by_code(
    session: Session, *, proposal: PartProposal, kind: PartKind, reason: str
) -> PartConfirmation | None:
    """Code confirming one of Bala's countertop suggestions because two judgments agree (#1244).

    The ADR-0021 amendment of 2026-10-14 (Anant, decision 1): the caller (`workflow/bala_items.py:
    confirm_automatic_countertops`) has found that both AI row readers named this row **and** code
    places it wholly inside one drawing a confirmation says is the vendor's, outlined by its printed
    overall that its pieces tile end to end. `reason` says so, in words. **Delegates to
    `confirm_part`**, which stays the only constructor of an item: recorded under `BALA_CONFIRMER`,
    audited as `EVIDENCE_QUALIFICATION`, with no code and `corroborated` left `False`.

    **Only a countertop.** Pieces stay one click for a person (decision 2): any other kind is
    refused.

    **A person always wins, and one place holds one countertop.** Nothing is written, and `None` is
    returned, whenever `why_code_may_not_confirm` gives a reason: the suggestion has any decision
    at all; a person's current decision stands on a countertop at the same place on the same
    drawing; or any live countertop, whoever made it, already stands there (code's own item from
    an earlier revision that shares the drawing; review of #1248).
    """
    if kind is not PartKind.COUNTERTOP:
        raise ValueError("code confirms only a countertop; a piece stays a person's click")
    if proposal.kind != PartKind.COUNTERTOP.value:
        raise ValueError("code confirms only a suggestion of a countertop")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a code confirmation needs the reason both judgments gave")
    if why_code_may_not_confirm(session, proposal) is not None:
        return None
    return confirm_part(session, proposal=proposal, kind=kind, code=None, actor=BALA_CONFIRMER)


#: Why code may not confirm a countertop suggestion (`why_code_may_not_confirm`), in plain words.
ALREADY_DECIDED: Final = "a decision already stands on it"
A_PERSON_DECIDED_HERE: Final = "a person decided a countertop at the same place"
A_PERSONS_RUN_HERE: Final = "a person confirmed a run beneath the countertop here"
ANOTHER_COUNTERTOP_STANDS: Final = "an earlier revision's countertop stands here"

#: Why code keeps its own item where it would otherwise take it back (re-review of #1248).
A_PERSONS_RUN_BENEATH: Final = "kept: a person confirmed a run beneath it"


def why_code_may_not_confirm(session: Session, proposal: PartProposal) -> str | None:
    """Why code may not confirm this countertop suggestion, or `None` when it may (#1244).

    In order: the suggestion has any decision (a person's, or code's own earlier one, kept or taken
    back: code never repeats itself); a person's current decision stands on a countertop at the
    same place on the same drawing (`person_decided_countertop_here`), on whatever suggestion it
    was made; a live countertop stands there that a person confirmed or withdrew a run beneath
    (code's own item, which code then keeps: `person_run_beneath`); or any other live countertop
    stands there. "There" is outlines overlapping from left to right on the same drawing. Code's
    other items of this revision's older readings are taken back before code confirms
    (`workflow/bala_items.py`), so the last one left is another revision's, on a drawing the two
    revisions share (review of #1248): code never writes onto another revision's records, and
    never makes two overlapping live countertops.
    """
    if _any_decision(session, proposal):
        return ALREADY_DECIDED
    if person_decided_countertop_here(session, proposal):
        return A_PERSON_DECIDED_HERE
    if _overlapping_countertops(session, proposal, live_only=True, persons_run=True):
        return A_PERSONS_RUN_HERE
    if _overlapping_countertops(session, proposal, live_only=True):
        return ANOTHER_COUNTERTOP_STANDS
    return None


def release_contradicted_code_part(
    session: Session, *, proposal: PartProposal, judgments_agree: bool
) -> PartConfirmation | None:
    """Take back code's own item when a later read no longer agrees (#1244); mirrors
    `workflow/view_roles.py:release_contradicted_code_role`.

    The caller says whether the two judgments still agree on this suggestion's row
    (`judgments_agree`): `False` when a later read of the same run disagrees, when a newer
    slot-reader run has replaced the row, or when a person has since decided a countertop at the
    same place (`person_decided_countertop_here`: a person always wins). Only then, **only when
    the current decision is code's own confirmation**, and **never when a person has confirmed or
    withdrawn a run beneath its item** (`person_run_beneath`, re-review of #1248: the run is a
    person's decision about this countertop, and taking the item back would leave the run on a
    dead item and hide its check), is it withdrawn, as `BALA_CONFIRMER`; a person's decision, of
    either kind, is never touched. Returns the withdrawal, or `None` when nothing was withdrawn.
    """
    if judgments_agree:
        return None
    current = current_decision(session, proposal)
    if not _codes_own_confirmation(current) or person_run_beneath(session, current):
        return None
    return withdraw_part(session, proposal=proposal, actor=BALA_CONFIRMER)


def person_run_beneath(session: Session, decision: PartConfirmation | None) -> bool:
    """Whether a person has confirmed or withdrawn a run beneath the item this decision made (re-review
    of #1248). Any run decision of a person's counts, current or not, as in
    `workflow/part_operands.py:countertop_scopes`: it is a person's word about this countertop."""
    if decision is None or decision.drawing_item_id is None:
        return False
    return (
        session.execute(
            select(CountertopRunDecision.id)
            .where(
                CountertopRunDecision.countertop_item_id == decision.drawing_item_id,
                ~CountertopRunDecision.confirmed_by.startswith(CODE_ACTOR_PREFIX),
            )
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _codes_own_confirmation(decision: PartConfirmation | None) -> bool:
    return (
        decision is not None
        and decision.confirmed_by == BALA_CONFIRMER
        and decision.decision == PartDecision.CONFIRMED.value
    )


def _any_decision(session: Session, proposal: PartProposal) -> bool:
    return (
        session.execute(
            select(PartConfirmation.id)
            .where(PartConfirmation.part_proposal_id == proposal.id)
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def person_decided_countertop_here(session: Session, proposal: PartProposal) -> bool:
    """Whether a person's current decision stands on a countertop suggestion on the same drawing
    whose outline overlaps this one from left to right: confirmed (a live countertop there, added
    or suggested) or withdrawn (a person said it is not one, or undid code's).

    Overlap, not equality: a re-read may draw the row a little elsewhere, and a person's countertop
    on the same stretch of the drawing is the same countertop as far as code may tell. Two
    countertops side by side on one drawing do not overlap and do not block each other. Code asks
    this before confirming, and again of each of its own items on every match, so a person's
    decision made after code's item (a countertop added over it, or an older suggestion decided
    while code confirmed a newer one) takes code's item back (review of #1248).
    """
    return _overlapping_countertops(session, proposal, live_only=False, persons_only=True)


def _overlapping_countertops(
    session: Session,
    proposal: PartProposal,
    *,
    live_only: bool,
    persons_only: bool = False,
    persons_run: bool = False,
) -> bool:
    """Whether another suggestion on the same drawing, overlapping this one from left to right,
    carries a current decision about a countertop: a confirmation of one (`live_only`), or any
    decision on a countertop suggestion; only a person's with `persons_only`; only one whose item
    a person confirmed or withdrew a run beneath with `persons_run`."""
    left, _top, right, _bottom = outline_box(proposal.extent)
    later = aliased(PartConfirmation)
    query = select(PartProposal.extent).join(
        PartConfirmation, PartConfirmation.part_proposal_id == PartProposal.id
    )
    query = query.where(
        PartProposal.drawing_view_id == proposal.drawing_view_id,
        PartProposal.id != proposal.id,
        ~exists().where(later.supersedes_id == PartConfirmation.id),
    )
    if live_only:
        query = query.where(
            PartConfirmation.decision == PartDecision.CONFIRMED.value,
            PartConfirmation.kind == PartKind.COUNTERTOP.value,
        )
    else:
        query = query.where(
            (PartProposal.kind == PartKind.COUNTERTOP.value)
            | (PartConfirmation.kind == PartKind.COUNTERTOP.value)
        )
    if persons_only:
        query = query.where(~PartConfirmation.confirmed_by.startswith(CODE_ACTOR_PREFIX))
    if persons_run:
        query = query.where(
            PartConfirmation.drawing_item_id.in_(
                select(CountertopRunDecision.countertop_item_id).where(
                    ~CountertopRunDecision.confirmed_by.startswith(CODE_ACTOR_PREFIX)
                )
            )
        )
    for extent in session.execute(query).scalars():
        other_left, _, other_right, _ = outline_box(extent)
        if other_left < right and left < other_right:
            return True
    return False


def _require_actor(actor: str) -> None:
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("a decision on a part needs the person who made it")


# ---------------------------------------------------------------------------
# Where a confirmed part lies
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlacedPart:
    """A confirmed part as the decisions made from it read it: which item, what kind, which drawing,
    and the box around its outline in stored page space, where `y` grows down the page.

    A part a person added has a two-point outline (#882): its box is that line, `top == bottom`.
    """

    item_id: UUID
    kind: PartKind
    view_id: UUID
    left: Decimal
    right: Decimal
    top: Decimal
    bottom: Decimal

    @classmethod
    def from_extent(
        cls, *, item_id: UUID, kind: PartKind, view_id: UUID, extent: Mapping[str, object]
    ) -> PlacedPart:
        """The box around a stored outline, read exactly by `outline_box`."""
        left, top, right, bottom = outline_box(extent)
        return cls(
            item_id=item_id,
            kind=kind,
            view_id=view_id,
            left=left,
            right=right,
            top=top,
            bottom=bottom,
        )


def outline_box(extent: Mapping[str, object]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """`(left, top, right, bottom)` of a stored outline, `{"space": "stored", "points": [[x, y],
    ...]}`, read exactly, `y` growing down the page.

    Refuses an outline in any other space, with no points, or with a point that is not a finite
    number, rather than placing the part somewhere it is not. A part a person added has a two-point
    outline (#882), so its box has no height. What is decided from the parts reads this, and so does
    the picture of each one (#897).
    """
    if extent.get("space") != "stored":
        raise ValueError("a part's outline must be in stored page space")
    raw = extent.get("points")
    if not isinstance(raw, list) or not raw:
        raise ValueError("a part's outline must have points")
    points: list[tuple[Decimal, Decimal]] = []
    for point in cast(list[object], raw):
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError("a part's outline holds a point that is not an x and a y")
        try:
            x, y = Decimal(str(point[0])), Decimal(str(point[1]))
        except InvalidOperation as error:
            raise ValueError("a part's outline holds a point that is not a number") from error
        if not (x.is_finite() and y.is_finite()):
            raise ValueError("a part's outline holds a point that is not a finite number")
        points.append((x, y))
    return (
        min(x for x, _ in points),
        min(y for _, y in points),
        max(x for x, _ in points),
        max(y for _, y in points),
    )


def check_edge_tolerance(tolerance: object) -> Decimal:
    """The tolerance, refused unless it is an exact, finite, non-negative number.

    A float would let binary rounding decide whether two ends drawn on the page meet. NaN fails
    every comparison and infinity passes every one, so neither loosens the tests: both remove them.
    """
    if not isinstance(tolerance, Decimal):
        raise TypeError("edge_tolerance must be a Decimal")
    if not tolerance.is_finite():
        raise ValueError("edge_tolerance must be a finite number")
    if tolerance < 0:
        raise ValueError("edge_tolerance cannot be negative")
    return tolerance


def across(part: PlacedPart) -> tuple[Decimal, Decimal, Decimal, Decimal, str]:
    """Left end, then right end, then top and bottom; the item id breaks only exact ties, so the
    order never depends on which part was listed or clicked first."""
    return part.left, part.right, part.top, part.bottom, str(part.item_id)


def _live_parts_query() -> Select[tuple[DrawingItem, str | None]]:
    """Each item a person's current decision confirmed, with the kind they confirmed. A
    confirmation always names its kind (`part_confirmation_decision_shape`)."""
    later = aliased(PartConfirmation)
    return (
        select(DrawingItem, PartConfirmation.kind)
        .join(PartConfirmation, PartConfirmation.drawing_item_id == DrawingItem.id)
        .where(
            PartConfirmation.decision == PartDecision.CONFIRMED.value,
            ~exists().where(later.supersedes_id == PartConfirmation.id),
        )
    )


def _placed(item: DrawingItem, kind: str | None) -> PlacedPart:
    if kind is None:
        raise ValueError("a confirmed part names the kind it was confirmed as")
    return PlacedPart.from_extent(
        item_id=item.id, kind=PartKind(kind), view_id=item.drawing_view_id, extent=item.extent
    )


def live_part(session: Session, item_id: UUID) -> PlacedPart | None:
    """The confirmed part this item is, while a person stands by it; `None` for any other id."""
    row = session.execute(_live_parts_query().where(DrawingItem.id == item_id)).one_or_none()
    return None if row is None else _placed(*row)


def live_parts_on(session: Session, view_id: UUID) -> tuple[PlacedPart, ...]:
    """Every confirmed part on one drawing that a person stands by, left to right."""
    rows = session.execute(_live_parts_query().where(DrawingItem.drawing_view_id == view_id))
    return tuple(sorted((_placed(*row) for row in rows), key=across))


def _record(session: Session, confirmation: PartConfirmation, actor: str) -> PartConfirmation:
    session.add(confirmation)
    session.flush()
    # Audited in the same transaction, like every reviewer action: whether a part exists is a
    # person's call, and the audit trail is where somebody looks for who made it. Code's decision
    # (#1244) is evidence qualification, as for code's view roles, never a review action.
    emit(
        session,
        category=(
            AuditCategory.EVIDENCE_QUALIFICATION if by_code(actor) else AuditCategory.REVIEW_ACTION
        ),
        actor=actor,
        target_id=confirmation.id,
        target_type="part_confirmation",
    )
    session.flush()
    return confirmation
