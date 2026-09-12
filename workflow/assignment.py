"""Which reading fills which rule field — proposed by a model, checked by this module.

**The point is the checking, not the proposing.** A reviewer currently says what every reading
means, one click each, because nothing else could. The objection to letting a model do it was that
the model would be guessing — and that objection was aimed at the wrong picture. A model shown a
crop and asked "what is this?" is guessing. A model shown what this pipeline already knows is
solving a constrained assignment problem, and the constraints are strong enough that most wrong
answers are not merely unlikely but *detectable*.

What the extraction layer already establishes, per reading, before any model runs:

* the exact value and unit, parsed and refused if ambiguous
* which sheet it was read from — recorded, not inferred
* which dimension line it annotates (`text_association`), and therefore that it annotates one at all
* that line's extent, its witness lines, and any chain or closure it belongs to (`dimension_lines`)

And per field, from the published rulebook: its name, its sheet, whether it takes one value or an
ordered run, and what the rule says it is for.

So the model is not asked what a number means in the abstract. It is asked to map a small set of
located readings onto a small set of named fields, and **every structural property of a correct
answer is one this module can verify** — which sheet, whether the reading is attached at all, how
many values the field takes, whether two fields claim the same reading, whether an ordered run is
actually a run on the drawing.

**What it is never given is the rule arithmetic.** `CT-WIDTH-001` checks that a countertop width
equals the sum of the cabinets and fillers. A model that knew the equation could assign readings so
that it balances, and the check would then confirm the balance — passing on every drawing, including
one with a real error in it. The field's *name* and *description* are safe; its formula is not, and
`AssignmentContext` has no place to put one.

**A refused proposal is not an error.** It falls back to the reviewer typing, exactly as narration
falls back to deterministic prose. The reviewer is the design, not the failure case.

Source: issue #589 · Verification: `tests/workflow/test_assignment.py`
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "AcceptedAssignment",
    "AssignmentContext",
    "AssignmentModel",
    "AssignmentRefused",
    "Field",
    "ProposedAssignment",
    "Reading",
    "guard_assignment",
]


@dataclass(frozen=True, slots=True)
class Field:
    """One quantity the published rulebook wants a value for."""

    key: str
    """`SOURCE:SEMANTIC_TYPE`, the same key `required-inputs` uses."""

    name: str
    """The rulebook's own readable name for it — `sink_cabinet_width`, not `CT004`."""

    source: str
    """Which sheet the rule takes it from. A reading off the other one cannot fill it."""

    many: bool
    """Whether it takes an ordered run of values rather than one."""

    description: str | None = None
    """What the published rule says the check is for. Context for the choice, never a formula —
    see the module docstring on why the arithmetic is withheld."""


@dataclass(frozen=True, slots=True)
class Reading:
    """One thing the extraction layer read, with everything it established about it."""

    candidate_id: str
    value: str
    source: str
    page: int

    line_key: str | None = None
    """Which dimension line this reading annotates, or `None` if it annotates none.

    `None` is not a detail. A reading attached to nothing is a number floating on a sheet — a title
    block, a revision note, a scale bar — and `text_association` refusing to attach it is a result,
    not a gap. It cannot fill a field, and `guard_assignment` says so rather than letting a model
    decide the point."""

    chain_key: str | None = None
    """Which chain of end-to-end dimensions its line belongs to, if any. What makes an ordered run
    checkable: the parts of a run are drawn as a chain, so a proposal claiming four readings are one
    field's ordered values can be tested against whether the drawing draws them that way."""

    order: int | None = None
    """Its position along that chain, low to high. `None` when it is in no chain."""

    geometry_available: bool = True
    """Whether this reading's page had any dimension line-work to be checked against.

    **`False` is not a weaker `line_key is None`; it is a different statement.** A reading that is
    unattached on a page with twelve dimension lines sits near none of them, and refusing it is this
    module doing its job. A reading unattached on a page with *no* line-work was never tested: the
    drawing is a scanned image, the detector had nothing to detect, and treating the two the same
    switched this step off entirely for that whole class of drawing on the strength of a check that
    never ran.

    So where it is `False` the attachment check abstains rather than refusing, the assignment is
    marked as having unverified placement, and the screen says so. Every other check still applies,
    and the reviewer still confirms every value before anything is saved."""


@dataclass(frozen=True, slots=True)
class AssignmentContext:
    """Everything a model is given. Deliberately everything, and deliberately no more."""

    fields: tuple[Field, ...]
    readings: tuple[Reading, ...]


@dataclass(frozen=True, slots=True)
class ProposedAssignment:
    """One field and the readings a model proposes fill it, in order."""

    field_key: str
    candidate_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AcceptedAssignment:
    """A proposal that survived every check that could be run."""

    assignments: tuple[ProposedAssignment, ...]

    unverified_placement: tuple[str, ...] = ()
    """The field keys whose readings could not be checked against the drawing's geometry, because
    the page had none. Reported rather than silently folded in: a caller that shows a value has to
    be able to say on what grounds it is there, and "the geometry agrees" and "there was no
    geometry" are different grounds for the same number in the same box."""


@dataclass(frozen=True, slots=True)
class AssignmentRefused:
    """Why the whole proposal was rejected, in terms a reviewer could act on.

    **The whole proposal, not the bad rows.** Keeping the parts that passed would present a partly
    model-assigned form as though a person had checked it, and the reviewer would have no way to
    tell which fields were which. Narration refuses as a batch for the same reason.
    """

    reason: str


class AssignmentModel(Protocol):
    """The only model capability this step can reach."""

    def propose(self, context: AssignmentContext) -> tuple[ProposedAssignment, ...]:
        """Map readings onto fields. Never called with the rule arithmetic — see the docstring."""


def guard_assignment(
    context: AssignmentContext, proposals: Sequence[ProposedAssignment]
) -> AcceptedAssignment | AssignmentRefused:
    """Accept a proposal only if every structural property of a correct one holds.

    Seven checks, and each rejects a way a plausible-looking assignment is wrong. None of them
    inspects a *value*: this decides whether the shape of the answer is possible, and what the
    numbers then say is the deterministic engine's business.

    **One of the seven can be unavailable rather than failed.** Where a page carries no dimension
    line-work at all, the attachment check has nothing to test against; it abstains, and the fields
    it could not vouch for come back in `unverified_placement` rather than refusing the batch. That
    is the difference between "we looked and this number is floating" and "there was nothing to look
    at", and collapsing them switched this step off for every scanned drawing.
    """
    fields = {field.key: field for field in context.fields}
    readings = {reading.candidate_id: reading for reading in context.readings}

    claimed: dict[str, str] = {}
    unverified: set[str] = set()
    for proposal in proposals:
        field = fields.get(proposal.field_key)
        if field is None:
            return AssignmentRefused(
                f"proposed a value for {proposal.field_key!r}, which this rulebook does not ask for"
            )

        if not proposal.candidate_ids:
            return AssignmentRefused(
                f"proposed {field.name} with no reading. A field nobody filled is left empty, "
                "which is already what happens without a proposal"
            )

        # A field takes one value or a run. A model offering three readings for a single-valued
        # quantity has not made a near-miss; it has misunderstood what the field is.
        if not field.many and len(proposal.candidate_ids) != 1:
            return AssignmentRefused(
                f"{field.name} takes one value and {len(proposal.candidate_ids)} were proposed"
            )

        for candidate_id in proposal.candidate_ids:
            reading = readings.get(candidate_id)
            if reading is None:
                return AssignmentRefused(
                    f"proposed reading {candidate_id!r}, which this run did not produce"
                )

            # Which sheet a reading came off is recorded by the reader, so a rule that takes its
            # value from the shop drawing cannot be filled from the architectural one. This is the
            # check most likely to fire, and the one a model is most likely to get wrong quietly.
            if reading.source != field.source:
                return AssignmentRefused(
                    f"{field.name} is read from the {field.source} drawing, and {reading.value} "
                    f"was read from the {reading.source} one"
                )

            # An unattached reading is a number floating on the sheet. `text_association` already
            # declined to say what it annotates; a model may not overrule that by using it.
            #
            # **Unless there was nothing to attach it to.** On a page with no dimension line-work —
            # a scanned drawing, where the detector had nothing to detect — the attachment check did
            # not run, and refusing on its silence would be reporting an examination that never
            # happened. It abstains instead, and the field is marked so the screen can say which of
            # the two grounds the value is there on. See `Reading.geometry_available`.
            if reading.line_key is None:
                if reading.geometry_available:
                    return AssignmentRefused(
                        f"{reading.value} is not attached to any dimension line, so there is "
                        f"nothing to say it measures {field.name}"
                    )
                unverified.add(field.key)

            if candidate_id in claimed:
                return AssignmentRefused(
                    f"{reading.value} was proposed for both {claimed[candidate_id]} and "
                    f"{field.name}. One reading measures one thing"
                )
            claimed[candidate_id] = field.name

        if field.many:
            refusal = _run_is_drawn_as_one(field, proposal, readings)
            if refusal is not None:
                return refusal

    return AcceptedAssignment(
        assignments=tuple(proposals), unverified_placement=tuple(sorted(unverified))
    )


def _run_is_drawn_as_one(
    field: Field, proposal: ProposedAssignment, readings: dict[str, Reading]
) -> AssignmentRefused | None:
    """Check that a many-valued field's readings are a run the drawing actually draws.

    **This is the check the geometry was built for.** `cabinet_widths` is not a set of numbers off
    the same sheet; it is the widths along one run, in order, and `CT-WIDTH-001` compares two runs
    position by position. A proposal that gathered three plausible widths from three unrelated
    places would produce a check that compares the second cabinet against the fifth.

    A chain is exactly that run — dimensions drawn end to end, which `dimension_lines` finds without
    reading any value. So the test is: do these readings sit on one chain, and are they in the order
    the proposal states? Both are answerable from where the strokes are.

    Readings in no chain are not refused outright. A single-item run is ordinary on a short sheet,
    and a drawing that dimensions its parts without chaining them is a drawing this cannot check —
    which is a limit worth stating rather than a failure to manufacture.
    """
    chosen = [readings[candidate_id] for candidate_id in proposal.candidate_ids]
    chains = {reading.chain_key for reading in chosen if reading.chain_key is not None}

    if len(chains) > 1:
        return AssignmentRefused(
            f"{field.name} was proposed from {len(chains)} separate runs on the drawing. Its values "
            "are compared position by position, so they have to be one run"
        )

    ordered = [reading.order for reading in chosen if reading.order is not None]
    if len(ordered) > 1 and ordered != sorted(ordered):
        return AssignmentRefused(
            f"{field.name} was proposed out of the order the drawing draws it. Position is what the "
            "check compares, so the order is a fact about the sheet rather than a presentation "
            "choice"
        )
    return None
