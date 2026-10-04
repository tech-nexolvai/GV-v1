"""Which confirmed parts sit beneath each confirmed countertop: suggested, then decided by a person.

A person confirms a drawing's cabinets, fillers and countertops one at a time (#882). This module
says which of those cabinets and fillers make up the run beneath each countertop (#893, step 5 of
the plan on #748). **The computer only suggests a run; a person confirms, corrects or withdraws it.**

## The suggestion (`propose_run`), which writes nothing

For one confirmed countertop, the suggested run is every confirmed cabinet and filler that:

- is on **the same drawing** as the countertop. Two elevations can share a sheet, and a run built
  out of two drawings would sum parts of two different walls;
- lies **along the countertop**: its ends across the page lie within the countertop's own ends,
  give or take the stated tolerance. This is the test `extraction/model/assembly.py` makes (its
  `_within`), on the same stored page space;
- **sits below the countertop**: it reaches no higher up the page than the countertop's top edge,
  give or take the stated tolerance. Stored `y` grows down the page. This is the filter that leaves
  out a wall cabinet drawn above the top, which a person may have confirmed as a cabinet: a part's
  kind does not say base or wall.

The members are ordered left to right across the page by their own ends, never by the order a
person clicked them (`CAB-FILLER-001` compares two runs position by position, and #794 was a false
PASS from click order). Each carries the sentence that says why it was suggested.

**What "below" is measured on.** A part's outline is the one its suggestion recorded (#868): the
ends of the dimension that defined it, and how far that dimension's extension lines reach down the
page; a part a person added is the line between the two ends they gave. Those outlines say where a
part's dimension is drawn, not where the part is, and on a real drawing they overlap. On `AI_Set_2`
page 5 the overall is drawn above its row and its extension lines reach as far down as the row's,
so the row's outlines start at or below the overall's top but end level with its bottom: "wholly
below" would have left out all three parts of that run, so the test is the top edge. Its known
failure is an overall drawn *beneath* its row (`AI_Set_2` page 9): the row then reaches higher up
the page than the countertop's outline and is left out. That fails towards a shorter suggestion,
which says what it lacks, never towards an extra member, and a person adds what is missing.

**The countertop's extent is its own.** Nothing here widens it to its members or narrows it to
them: taking it from the run would make "the run reaches both ends" true by construction (the plan's
principle). The suggestion compares the run against it and says, in words, where the run falls
short of an end, leaves a gap, or overlaps; it does not repair any of them, because a part the
computer invented to close a gap is one a person would have to notice was invented.

**Widths never enter.** This module reads outlines and kinds, never a reading's value: a run says
which parts belong to which top, and what each part measures stays with its readings.

## The decision, which only a person makes

`confirm_countertop_run` is **the only code that writes a `countertop_runs` row**, and with
`withdraw_countertop_run` the only code that writes a `countertop_run_decisions` row; a guard in
`tests/db/test_drawing_models.py` fails if anything else constructs or inserts into either. The
person names the members; the writer orders them across the page itself, takes each suggested
member's sentence from the suggestion it recomputes with the same tolerance, and records the
tolerance with the run. A member the suggestion did not hold is written with a sentence saying a
person added it.

**One current decision per countertop**, held by the database (`CountertopRunDecision`). A
correction is a new run under a new `run_id` with a decision naming the one it replaces; a
withdrawal is a decision naming no run.

**What a reader reads** (`live_run_rows`): only the run the countertop's current decision confirmed,
and only while the countertop and every member are still parts a person stands by
(`workflow/parts.py:live_part_item_ids`). If a person takes back or corrects one member, the whole
run stops being read, never the rest of it: a run with a member missing summed as if it were whole
is short by that member. **No rule reads runs yet** (#748 step 7), and a guard in
`tests/db/test_drawing_models.py` keeps them out of everything but the models and their migrations,
this module, and the Measure page's listing and endpoints.

**No extraction imports, on purpose**, as in `workflow/parts.py`: the API reaches this, and
`tests/api/test_no_heavy_work.py` keeps `app/api/` away from anything that reads a PDF. That is why
the suggestion here repeats `extraction/model/assembly.py`'s span test rather than calling it.

Where a confirmed part lies (`PlacedPart`, `live_part`, `live_parts_on`) is read in
`workflow/parts.py`, which this module and the link between a reading and its part (#913) share.

Source: issue #893; #748 plan, step 5. Verification: tests/workflow/test_countertop_runs.py,
tests/db/test_drawing_models.py, tests/api/test_countertop_runs.py.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, cast
from uuid import UUID, uuid4

from sqlalchemy import Select, exists, select
from sqlalchemy.orm import Session, aliased

from app.audit.events import AuditCategory, emit
from app.models import (
    CountertopRun,
    CountertopRunDecision,
    PartDecision,
)
from vocabulary.part_kinds import PartKind
from workflow.parts import (
    PlacedPart,
    check_edge_tolerance,
    live_part,
    live_part_item_ids,
    live_parts_on,
)
from workflow.parts import across as _across

__all__ = [
    "MEMBER_KINDS",
    "RUN_PROPOSAL_SOURCE",
    "LeftOut",
    "PlacedPart",
    "ProposedMember",
    "RunProposal",
    "check_edge_tolerance",
    "confirm_countertop_run",
    "current_run_decision",
    "live_part",
    "live_parts_on",
    "live_run_rows",
    "propose_run",
    "withdraw_countertop_run",
]

#: What suggests runs, and its version, as `countertop_runs.proposal_source` records it. A later,
#: different suggester is a different source, so its runs never appear to agree with this one's.
RUN_PROPOSAL_SOURCE: Final = "workflow.countertop_runs;v1"

#: The kinds of part that may sit in a run beneath a countertop.
MEMBER_KINDS: Final = frozenset({PartKind.CABINET, PartKind.FILLER})

#: How many places the numbers in a sentence for a person are shown to. Display only: every
#: comparison is made on the exact stored values.
_SHOWN: Final = ".4f"


@dataclass(frozen=True, slots=True)
class ProposedMember:
    """One suggested member of a run, its place counting from `0` on the left, and why."""

    part: PlacedPart
    position: int
    signal: str


@dataclass(frozen=True, slots=True)
class LeftOut:
    """A cabinet or filler along the countertop that the suggestion left out, and why."""

    part: PlacedPart
    reason: str


@dataclass(frozen=True, slots=True)
class RunProposal:
    """The run suggested beneath one countertop. **A suggestion: nothing writes it.**"""

    countertop: PlacedPart
    members: tuple[ProposedMember, ...]
    left_out: tuple[LeftOut, ...]
    """Parts along the countertop but above its top, which the filter left out."""
    warnings: tuple[str, ...]
    """Where the suggested run does not cover the countertop as one unbroken row, in plain English.
    Said, never repaired."""
    edge_tolerance: Decimal


def propose_run(
    countertop: PlacedPart, parts: Sequence[PlacedPart], *, edge_tolerance: Decimal
) -> RunProposal:
    """Suggest the run beneath `countertop` from `parts`, the confirmed parts it may come from.

    Reads only what it is given and writes nothing. The tolerance is required and has no default:
    it is the stated `GV_RUN_EDGE_TOLERANCE`, in stored units. Raises `ValueError` if `countertop`
    is not a countertop, since a run beneath a cabinet is not a question with an answer.
    """
    tolerance = check_edge_tolerance(edge_tolerance)
    if countertop.kind is not PartKind.COUNTERTOP:
        raise ValueError("a run is suggested beneath a countertop, and this part is not one")

    along = [
        part
        for part in parts
        if part.item_id != countertop.item_id
        and part.view_id == countertop.view_id
        and part.kind in MEMBER_KINDS
        and _along(part, countertop, tolerance)
    ]
    below = sorted((part for part in along if _below(part, countertop, tolerance)), key=_across)
    above = sorted((part for part in along if not _below(part, countertop, tolerance)), key=_across)
    return RunProposal(
        countertop=countertop,
        members=tuple(
            ProposedMember(part=part, position=index, signal=_signal(part, countertop))
            for index, part in enumerate(below)
        ),
        left_out=tuple(LeftOut(part=part, reason=_above(part, countertop)) for part in above),
        warnings=_warnings(countertop, below, tolerance),
        edge_tolerance=tolerance,
    )


def _along(part: PlacedPart, countertop: PlacedPart, tolerance: Decimal) -> bool:
    """Whether the part's ends across the page lie within the countertop's, give or take."""
    return part.left >= countertop.left - tolerance and part.right <= countertop.right + tolerance


def _below(part: PlacedPart, countertop: PlacedPart, tolerance: Decimal) -> bool:
    """Whether the part reaches no higher up the page than the countertop's top, give or take.

    Stored `y` grows down the page, so higher up is smaller. The module docstring says why the top
    edge, and where this fails.
    """
    return part.top >= countertop.top - tolerance


def _span(part: PlacedPart) -> str:
    return f"{part.left:{_SHOWN}} to {part.right:{_SHOWN}}"


def _signal(part: PlacedPart, countertop: PlacedPart) -> str:
    return (
        f"A confirmed {part.kind.value} on the same drawing as the countertop. Its ends across the "
        f"page ({_span(part)}) lie within the countertop's ({_span(countertop)}), and it reaches no "
        f"higher up the page than the countertop's top ({part.top:{_SHOWN}} against "
        f"{countertop.top:{_SHOWN}}), so it is not drawn above the top as a wall cabinet is."
    )


def _above(part: PlacedPart, countertop: PlacedPart) -> str:
    return (
        f"Left out: this {part.kind.value} lies along the countertop ({_span(part)}) but reaches "
        f"higher up the page than the countertop's top ({part.top:{_SHOWN}} against "
        f"{countertop.top:{_SHOWN}}), as a wall cabinet drawn above the top does. If it is a base "
        "cabinet whose dimension is drawn above the countertop's, add it to the run."
    )


def _warnings(
    countertop: PlacedPart, members: Sequence[PlacedPart], tolerance: Decimal
) -> tuple[str, ...]:
    if not members:
        return (
            (
                "No confirmed cabinet or filler on this drawing lies along this countertop below "
                "its top. If the parts beneath it are confirmed, pick them; if not, confirm them "
                "first."
            ),
        )
    said: list[str] = []
    if members[0].left - countertop.left > tolerance:
        said.append(
            f"The run starts at {members[0].left:{_SHOWN}}, short of the countertop's left end at "
            f"{countertop.left:{_SHOWN}}: a part at the left may be missing."
        )
    for index in range(len(members) - 1):
        first, second = members[index], members[index + 1]
        if second.left - first.right > tolerance:
            said.append(
                f"There is a gap between parts {index + 1} and {index + 2} of the run "
                f"({first.right:{_SHOWN}} to {second.left:{_SHOWN}}): a part between them may be "
                "missing."
            )
        elif first.right - second.left > tolerance:
            said.append(
                f"Parts {index + 1} and {index + 2} of the run overlap across the page "
                f"({second.left:{_SHOWN}} to {first.right:{_SHOWN}}): they may be one part listed "
                "twice, or two rows."
            )
    if countertop.right - members[-1].right > tolerance:
        said.append(
            f"The run ends at {members[-1].right:{_SHOWN}}, short of the countertop's right end at "
            f"{countertop.right:{_SHOWN}}: a part at the right may be missing."
        )
    return tuple(said)


# ---------------------------------------------------------------------------
# The decision, and what a reader may read
# ---------------------------------------------------------------------------


def current_run_decision(
    session: Session, countertop_item_id: UUID
) -> CountertopRunDecision | None:
    """The decision on this countertop's run that nothing has replaced, or `None` if nobody has
    decided. The schema allows at most one, so this asks for one and would raise rather than pick.
    """
    later = aliased(CountertopRunDecision)
    return session.execute(
        select(CountertopRunDecision).where(
            CountertopRunDecision.countertop_item_id == countertop_item_id,
            ~exists().where(later.supersedes_id == CountertopRunDecision.id),
        )
    ).scalar_one_or_none()


def live_run_rows() -> Select[tuple[CountertopRun]]:
    """The `countertop_runs` rows a reader may read: each of a run that its countertop's current
    decision confirmed, whose countertop and every member a person still stands by.

    A withdrawn run, a corrected one and a run whose countertop or any member was taken back or
    corrected are not here at all, not partly. Returns a query rather than running one, so the
    caller filters and orders it in its own statement.
    """
    later = aliased(CountertopRunDecision)
    live = live_part_item_ids()
    current = select(CountertopRunDecision.run_id).where(
        CountertopRunDecision.decision == PartDecision.CONFIRMED.value,
        ~exists().where(later.supersedes_id == CountertopRunDecision.id),
        CountertopRunDecision.countertop_item_id.in_(live),
    )
    member = aliased(CountertopRun)
    broken = select(member.run_id).where(~member.member_item_id.in_(live))
    return select(CountertopRun).where(
        CountertopRun.run_id.in_(current), ~CountertopRun.run_id.in_(broken)
    )


def confirm_countertop_run(
    session: Session,
    *,
    countertop_item_id: UUID,
    member_item_ids: Collection[UUID],
    edge_tolerance: Decimal,
    actor: str,
) -> CountertopRunDecision:
    """A person saying which parts make up the run beneath one countertop. **The only code that
    writes `countertop_runs`.**

    The countertop must be a confirmed countertop a person stands by, and each member a confirmed
    cabinet or filler on the same drawing, each named once. The writer orders the members across
    the page itself, recomputes the suggestion with `edge_tolerance` to take each suggested member's
    sentence, and records the tolerance on every row. The decision replaces whichever one was
    current, so confirming again is how a person corrects a run.

    Raises `ValueError` for anything a person could not have meant: no person, no members, a member
    twice, or a part that is not what it is named as.
    """
    _require_actor(actor)
    tolerance = check_edge_tolerance(edge_tolerance)
    wanted = list(member_item_ids)
    if not wanted:
        raise ValueError("a run needs at least one part; withdraw it to say there is none")
    if len(set(wanted)) != len(wanted):
        raise ValueError("a run names each of its parts once")
    countertop = live_part(session, countertop_item_id)
    if countertop is None or countertop.kind is not PartKind.COUNTERTOP:
        raise ValueError("a run is confirmed beneath a confirmed countertop")
    on_drawing = {part.item_id: part for part in live_parts_on(session, countertop.view_id)}
    members = [on_drawing.get(item_id) for item_id in wanted]
    if any(part is None or part.kind not in MEMBER_KINDS for part in members):
        raise ValueError(
            "every part of a run is a confirmed cabinet or filler on the countertop's drawing"
        )
    proposal = propose_run(countertop, list(on_drawing.values()), edge_tolerance=tolerance)
    suggested = {member.part.item_id: member.signal for member in proposal.members}
    left_out = {entry.part.item_id for entry in proposal.left_out}

    replaced = current_run_decision(session, countertop.item_id)
    run_id = uuid4()
    decision = CountertopRunDecision(
        countertop_item_id=countertop.item_id,
        supersedes_id=None if replaced is None else replaced.id,
        decision=PartDecision.CONFIRMED.value,
        run_id=run_id,
        confirmed_by=actor,
    )
    _record(session, decision, actor)
    ordered = sorted((cast(PlacedPart, part) for part in members), key=_across)
    for position, part in enumerate(ordered):
        session.add(
            CountertopRun(
                run_id=run_id,
                countertop_item_id=countertop.item_id,
                position=position,
                member_item_id=part.item_id,
                signal=suggested.get(part.item_id) or _added(part, part.item_id in left_out),
                proposal_source=RUN_PROPOSAL_SOURCE,
                edge_tolerance=tolerance,
                confirmed_by=actor,
            )
        )
    session.flush()
    return decision


def withdraw_countertop_run(
    session: Session, *, countertop_item_id: UUID, actor: str
) -> CountertopRunDecision:
    """A person saying the suggested or confirmed run is not the one beneath this countertop.

    Writes no member. A run confirmed before stays in its rows and stops being read, because the
    decision that confirmed it has been replaced.
    """
    _require_actor(actor)
    replaced = current_run_decision(session, countertop_item_id)
    decision = CountertopRunDecision(
        countertop_item_id=countertop_item_id,
        supersedes_id=None if replaced is None else replaced.id,
        decision=PartDecision.WITHDRAWN.value,
        confirmed_by=actor,
    )
    return _record(session, decision, actor)


def _added(part: PlacedPart, was_left_out: bool) -> str:
    if was_left_out:
        return (
            f"Added to the run by a person. The suggestion left this {part.kind.value} out because "
            "it reaches higher up the page than the countertop's top."
        )
    return (
        f"Added to the run by a person. The suggestion did not hold this {part.kind.value}: it does "
        "not lie along the countertop within the stated tolerance."
    )


def _require_actor(actor: str) -> None:
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("a decision on a run needs the person who made it")


def _record(session: Session, decision: CountertopRunDecision, actor: str) -> CountertopRunDecision:
    session.add(decision)
    session.flush()
    # Audited in the same transaction, like every reviewer action: which parts sit beneath a
    # countertop is a person's call, and the audit trail is where somebody looks for who made it.
    emit(
        session,
        category=AuditCategory.REVIEW_ACTION,
        actor=actor,
        target_id=decision.id,
        target_type="countertop_run_decision",
    )
    session.flush()
    return decision
