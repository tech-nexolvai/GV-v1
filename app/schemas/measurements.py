"""What a reviewer types, on its way in, and what came back.

**Values arrive as the token the person typed, not as a number.** `25 1/2"`, `984 mm`, `3'-6"` — the
same strings `units/normalise.py` already parses for extraction, converted exactly. Three reasons,
and the third is the one that decided it:

* A JSON number would be a float in most clients. `frontend/main/src/api/fractions.ts` explains why
  the outbound direction never uses one, and the inbound direction has the same problem: 25.5 is
  representable and 1/3 of an inch is not, so some values would arrive already wrong.
* The parser is tested, and it is the same parser that reads a drawing. A second numeric format here
  would be a second definition of what `25 1/2` means.
* **A bare number is refused, and that is inherited rather than re-decided.** `984` with no unit was
  once recorded as 984 inches — 82 feet — because tokenisation split it from its `mm` (#483). The
  parser refuses a unitless token, so this endpoint does too, and a reviewer who omits the mark is
  told rather than guessed at.

Exact values come *back* as `numerator`/`denominator` decimal strings, matching every other exact
number this API emits, so a client can render `51/2` as `25 1/2` without a float in the path.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ParameterEntry(BaseModel):
    """One setting a reviewer supplies for this job — a cabinet depth, an overhang, a sink interior.

    **`scope` decides which layer it lands in and it is not cosmetic.** A `project` setting applies to
    every review of the job; a `run` setting was true for this one — the sink on this cut sheet, the
    room as measured today. `rules/parameters.py` draws the same line, and filing a run value as a
    project setting would make a stale sink dimension look authoritative on the next review.

    The rulebook says which each parameter is, so a caller should send back what
    `GET .../required-inputs` reported rather than choosing.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    value: str = Field(
        min_length=1,
        max_length=100,
        description='The value as typed, carrying its unit: 24", 610 mm.',
    )
    scope: Literal["project", "run"] = "project"


class MeasurementEntry(BaseModel):
    """One dimension a reviewer read off a drawing, and which check input it is.

    **Keyed by rule and input name rather than by semantic type**, deliberately. The engine looks an
    operand up by the input name the rule declares, and the semantic vocabulary is still provisional
    (`CLIENT_FACTS` Q20) — so keying on a tag nobody has settled would bake a guess into the wire
    format. This asks for what the rulebook already names.
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    value: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="The value as typed, with its unit. Use `values` for a many-valued input.",
    )
    values: tuple[str, ...] | None = Field(
        default=None,
        description=(
            "The values as typed, in layout order, for an input the rulebook declares as many — a "
            "run of cabinet widths, the fillers either side. Order is kept, because "
            "CAB-ARCH-VS-SHOP-001 compares two runs position by position."
        ),
    )

    @model_validator(mode="after")
    def _exactly_one_form(self) -> MeasurementEntry:
        """One of `value` or `values`, never both and never neither.

        Both would leave two answers for one input with no stated winner. Neither would record a
        reading with nothing read — a row that exists and says nothing, which is worse than the
        reviewer having skipped the field, because it looks answered.
        """
        if (self.value is None) == (self.values is None):
            raise ValueError(
                f"{self.rule_id}.{self.name}: give either `value` for a single measurement or "
                "`values` for a many-valued one — not both, and not neither."
            )
        if self.values is not None and not self.values:
            raise ValueError(
                f"{self.rule_id}.{self.name}: `values` is empty. A run with no cabinets in it is "
                "not a measurement; leave the field out if there is nothing to record."
            )
        return self


class ReviewerEntry(BaseModel):
    """Everything one reviewer submission carries.

    Both halves are optional so a reviewer can set the project's parameters once and then enter
    measurements per package without resending them.
    """

    model_config = ConfigDict(extra="forbid")

    parameters: tuple[ParameterEntry, ...] = ()
    measurements: tuple[MeasurementEntry, ...] = ()


class StoredValue(BaseModel):
    """One value as stored: exact, and in inches because inches decide (Q12)."""

    name: str
    numerator: str
    denominator: str
    unit: str
    as_typed: str


class StoredList(BaseModel):
    """One many-valued input as stored, in layout order."""

    name: str
    values: tuple[StoredValue, ...]


class ReviewerEntryOut(BaseModel):
    """What was stored, echoed back exactly so a client can show what the system understood.

    Echoing the parse rather than the input is the point: `25.5"` and `25 1/2"` are the same value and
    a reviewer should be able to see that the system agrees.
    """

    parameter_set_version: int | None
    measurement_set_version: int | None
    parameters: tuple[StoredValue, ...]
    measurements: tuple[StoredValue, ...]
    #: Many-valued inputs, grouped, so a client can show a run back as a run rather than as
    #: `cabinet_widths#0` … `#3`.
    lists: tuple[StoredList, ...] = ()


class QuantityOut(BaseModel):
    """One physical measurement the reviewer must read off a drawing."""

    key: str
    semantic_type: str
    source: str
    many: bool
    #: The rule inputs this one measurement feeds, so a caller fans a single typed value out rather
    #: than asking for it once per rule.
    consumers: tuple[dict[str, str], ...]


class ConfirmedReadingOut(BaseModel):
    """One qualified drawing reading that can prefill this package's measurement form.

    This is intentionally an *input suggestion*, not a new verdict operand.  It can be a reviewer
    confirmation, or the deliberately narrower automatic lane: an exact vector vocabulary tag on
    the same associated dimension line.  The latter is named explicitly on the wire so the UI never
    presents a machine qualification as if a person had performed it.  Unqualified candidates never
    appear here.
    """

    #: The same ``SOURCE:semantic_type`` key used by :class:`QuantityOut`.
    key: str
    source: str
    semantic_type: str
    #: Exact normalized display text, e.g. ``25 1/2 in``.  The browser never computes this value.
    value: str
    #: Why this value has a semantic type.  ``exact_vector_tag`` is the only automatic type route.
    qualification: Literal["reviewer_confirmed", "exact_vector_tag"]


class ParameterOut(BaseModel):
    """One setting the reviewer supplies or confirms."""

    name: str
    scope: str
    rule_ids: tuple[str, ...]
    #: The rulebook's own stand-in where it has one, as authored text. A rule author's default, not a
    #: client-confirmed value — `CLIENT_FACTS` Q21 has the filler maximum at two different numbers.
    declared_default: str | None
    #: True for a value nobody may supply. Today only `back_offset_minimum`, whose rule states the
    #: vendor has not given it; offering a field would invite an invented safety threshold.
    blocked: bool


class DiscriminatorOut(BaseModel):
    """A judgement about the drawing that decides which variant of a rule applies."""

    name: str
    rule_ids: tuple[str, ...]
    #: Closed. The resolver matches against the declared variants, so anything else resolves to
    #: nothing and the rule reports NO_APPLICABLE_RULE — which reads as "does not apply here" rather
    #: than "you mistyped the layout".
    choices: tuple[str, ...]


class ProposedReadingOut(BaseModel):
    """One reading a model proposes for a field, named by the candidate it already is."""

    #: The candidate the extraction layer produced. Not a value a model composed — a model may only
    #: *choose* one of these, and `assignment_tool_schema` puts the real ids in the tool's `enum` so
    #: an invented one is not something it can emit.
    candidate_id: UUID
    value: str
    page_index: int
    #: Which run of end-to-end dimensions this reading's line belongs to, where the drawing draws
    #: one. Present so a reviewer can see *why* an ordered run was accepted as one.
    chain_key: str | None = None
    chain_position: int | None = None


class ProposedFieldOut(BaseModel):
    """One field and the readings proposed to fill it, in drawing order."""

    field_key: str
    #: The rulebook's own readable name, so a form need not translate `CT004` itself.
    name: str
    source: str
    many: bool
    #: Whether the drawing's own geometry confirmed where these readings sit.
    #:
    #: `False` on a page with no dimension line-work — a scanned drawing — where the attachment
    #: check had nothing to test against and abstained rather than refusing. Every other check still
    #: applied. The distinction is on the wire because a screen showing a value has to be able to say
    #: on what grounds it is there, and "the geometry agrees" and "there was no geometry" are not
    #: the same grounds.
    placement_verified: bool = True
    values: tuple[ProposedReadingOut, ...]


class RequiredInputsOut(BaseModel):
    """Everything the published rulebook needs, grouped so a form can render it.

    Derived from the published rules rather than listed, which is what makes it impossible for a
    field to be missing: a rule that gains an input gains a field here on the next publish.
    """

    quantities: tuple[QuantityOut, ...]
    #: Readings a reviewer already confirmed on the mechanical crop for this exact package revision.
    #: They are returned separately from rule requirements so the UI can make their provenance visible.
    confirmed_readings: tuple[ConfirmedReadingOut, ...] = ()
    #: What a model proposed for this revision, already checked, filed when the drawings were read.
    #:
    #: **Here rather than behind a second request**, because a form that arrives empty and fills a
    #: moment later is a form a reviewer starts typing into. Separate from `confirmed_readings` for
    #: the reason those are separate from the quantities: a proposal is the weakest claim on the
    #: screen, and the page has to be able to mark it as one.
    proposed_readings: tuple[ProposedFieldOut, ...] = ()
    parameters: tuple[ParameterOut, ...]
    discriminators: tuple[DiscriminatorOut, ...]
    #: How many rules are published. Zero means the form is empty because nothing is published, which
    #: is a different problem from a rulebook that asks for nothing.
    rules_published: int

    #: Whether something is still working on this package.
    #:
    #: **The form loads once, and reading a drawing takes the better part of a minute.** Opening
    #: Measure straight after uploading therefore showed an empty form with "nothing was read off
    #: these drawings" — permanently, because nothing went back to look. The readings landed thirty
    #: seconds later and the page never knew.
    #:
    #: So the form says which of the two it is: the reader has not finished, or it has finished and
    #: found nothing. They want opposite things from a reviewer — wait, or go and look at Documents.
    still_reading: bool = False

    #: The package revision's lifecycle state, as the pipeline last recorded it. The reason behind
    #: `still_reading`, so a screen can say *what* is happening rather than only that something is.
    revision_state: str = ""


class CheckRequest(BaseModel):
    """Asking for the checks, and what the reviewer says about the layout.

    **Discriminators travel with the request rather than being stored as evidence**, because that is
    what they are: a statement about how to read this package on this run. A rule with a discriminator
    nobody stated abstains with REVIEW_REQUIRED however complete the measurements are, so without
    these two `CT-WIDTH-001` and `CAB-FILLER-001` could never reach a verdict.
    """

    model_config = ConfigDict(extra="forbid")

    discriminators: dict[str, str] = Field(default_factory=dict)


class ProposedMeasurementsOut(BaseModel):
    """What survived every structural check, and enough counts to say so honestly.

    **Nothing here is stored.** These fill a form a reviewer then reads, edits and saves; the saving
    is what records a value, and it records it as the reviewer's. A model's proposal never becomes a
    measurement without a person submitting it.
    """

    assignments: tuple[ProposedFieldOut, ...]
    #: Every field the published rulebook asks for. The denominator of "filled".
    fields_total: int
    #: How many of them this proposal fills. The one genuine completion figure on the screen.
    fields_filled: int
    #: Readings this run produced with a value, and how many of those are attached to a dimension
    #: line. The second is the number that could fill anything: `guard_assignment` refuses an
    #: unattached reading, because `text_association` already declined to say what it annotates.
    readings_considered: int
    readings_attached: int
    #: The model that was asked, or `None` when no model is configured — in which case the fields
    #: stay empty and a reviewer fills them, which is what happens today.
    model_id: str | None = None
    #: Why nothing was filled, in the words of whatever declined — the deterministic guard's own
    #: sentence where a guard refused the proposal, otherwise that no model is configured or that
    #: the provider did not answer. `None` when something was filled.
    #:
    #: Named for the outcome rather than for a refusal, because the three causes reach the reviewer
    #: as the same situation — empty fields to type into — and only one of them is a refusal.
    unfilled_reason: str | None = None


class AssignmentStepOut(BaseModel):
    """One phase of the assignment, sent as that phase begins.

    **`percent` is phases finished, and says so.** It is not a guess at how long the model will
    take and not a confidence in the answer — those are two numbers nothing on this side of the
    request knows. A retry re-sends the phase it went back to, so the bar holds rather than
    advancing on work that was rejected.
    """

    index: int
    total: int
    #: Stable identifier for the phase, for a client that wants to style it. Prose is in `label`.
    name: str
    label: str
    detail: str = ""
    percent: int
    #: Which attempt this is, `1` for the first. A second attempt means a deterministic check
    #: refused the first and the model was told why.
    attempt: int = 1

    #: Every phase label in order, sent once on the first frame and empty afterwards.
    #:
    #: So a client can show what is still to come without keeping its own copy of the sequence —
    #: which would be a second answer to "what are the phases", free to disagree with this one the
    #: first time a phase is added. It also lets a client tell a phase that was *skipped* from one
    #: still waiting: a proposal nobody made is never checked, and rendering that as "done" would
    #: claim a check that did not run.
    sequence: tuple[str, ...] = ()


class AssignmentEvent(BaseModel):
    """One frame of the assignment stream: a phase beginning, or the finished result.

    The endpoint returns `text/event-stream` and each frame's `data:` is one of these. A stream
    rather than a single response because the model call is the slow part, and a screen that shows
    a real phase name while it waits is telling the truth about what is happening — where a bar
    moving on a timer would not be.
    """

    event: Literal["step", "result"]
    step: AssignmentStepOut | None = None
    result: ProposedMeasurementsOut | None = None
