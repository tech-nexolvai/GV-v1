"""What a reviewer types, on its way in, and what came back.

**Values arrive as the token the person typed, not as a number.** `25 1/2"`, `984 mm`, `3'-6"` — the
same strings `units/normalise.py` already parses for extraction, converted exactly. Three reasons,
and the third is the one that decided it:

* A JSON number would be a float in most clients. `app/api/finding_chain.py` explains why the
  outbound direction never uses one, and the inbound direction has the same problem: 25.5 is
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

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rules.parameters import REFERENCE_MAX_LENGTH, Provenance


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
    source: Provenance | None = Field(
        default=None,
        description=(
            "Where the value came from (#827), one of the setting's `sources` in required-inputs. "
            "May be left out only when the setting allows a single source."
        ),
    )
    reference: str | None = Field(
        default=None,
        max_length=REFERENCE_MAX_LENGTH,
        description='Where in that source, in the reviewer\'s words: "Architect A-501, section 3".',
    )
    citation: UUID | None = Field(
        default=None,
        description=(
            "The passage this value was typed from (#866): the `proposal_id` of the setting's "
            "`found` pointer in required-inputs. The server reads the passage's number and refuses "
            "a value that differs; on a match it records the source and the reference itself, so "
            "neither is sent with a citation."
        ),
    )

    @field_validator("reference")
    @classmethod
    def _blank_is_none(cls, reference: str | None) -> str | None:
        """An empty box is no reference, not a blank one — the form sends what the reviewer left."""
        if reference is None or not reference.strip():
            return None
        return reference.strip()


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


class ClassificationEntry(BaseModel):
    """What a reviewer says each item in one ordered run is.

    Separate from `MeasurementEntry` because a category is not a dimension: it has no unit, nothing
    parses it, and `normalise_to_inches` correctly refuses it. Sending one through `values` was how
    #684 was found — a reviewer had no way at all to say which cabinet was the sink cabinet.

    The choices come from the rulebook, through the required-inputs form, and the server checks the
    submission against them rather than trusting the client's list.
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    categories: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "One category per item, in layout order — left to right along the run. Order is kept "
            "because the distribution adjusts positionally: which cabinet is the equipment cabinet "
            "is the whole question."
        ),
    )

    @field_validator("categories")
    @classmethod
    def _each_one_present(cls, categories: tuple[str, ...]) -> tuple[str, ...]:
        """An empty slot is not an answer.

        A run submitted with a blank in it looks answered and is not. Leaving the item out would be
        no better — the operation compares the run's length against the cabinets and abstains — but
        a blank would reach it as a category nothing recognises, which is a worse way to say the
        same thing.
        """
        for position, category in enumerate(categories):
            if not category.strip():
                raise ValueError(
                    f"position {position} has no category. Classify every item in the run, or "
                    "leave the run out entirely."
                )
        return categories


class ReviewerEntry(BaseModel):
    """Everything one reviewer submission carries.

    Every part is optional so a reviewer can set the project's parameters once and then enter
    measurements per package without resending them.
    """

    model_config = ConfigDict(extra="forbid")

    parameters: tuple[ParameterEntry, ...] = ()
    measurements: tuple[MeasurementEntry, ...] = ()
    classifications: tuple[ClassificationEntry, ...] = ()


class StoredValue(BaseModel):
    """One value as stored: exact, and in inches because inches decide (Q12)."""

    name: str
    numerator: str
    denominator: str
    unit: str
    as_typed: str
    #: Where a setting came from and where in it (#827); absent for a measurement.
    source: str | None = None
    reference: str | None = None
    #: The passage a setting was typed from and matched (#866), as the `proposal_id` that was sent.
    citation: UUID | None = None


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
    #: The choices, when this input is a category rather than a dimension — empty otherwise.
    #:
    #: A form that rendered a text box here would ask a reviewer to type `single_door` with a unit,
    #: and the parser would refuse it. Non-empty means offer these and send them back under
    #: `classifications`, not `measurements`.
    categories: tuple[str, ...] = ()


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
    page_index: int
    #: Why this value has a semantic type.  ``exact_vector_tag`` is the only automatic type route.
    qualification: Literal["reviewer_confirmed", "exact_vector_tag"]


class SourceOut(BaseModel):
    """One source a setting may come from, and what choosing it means (#827)."""

    value: str
    guidance: str


class SettingPointerOut(BaseModel):
    """Where the architect's drawing states a setting: a page and a crop, **never the number** (#866).

    The reviewer types the value they see without being shown the one the app found, and the server
    saves it only if the two match (step 3.3 of #798). So nothing here may carry the number: no
    value, no text from the drawing, and no id of the runs, which `GET .../candidates` would turn
    back into a value. `tests/api/test_setting_citations.py` holds this class to that list.
    """

    #: What to send back as the entry's `citation`, and the key of the crop at
    #: `GET .../parameter-proposals/{proposal_id}/crop`.
    proposal_id: UUID
    #: The page the passage is on, from 0, as every other `page_index` this API sends.
    page_index: int
    #: The upload the page belongs to, `architectural` or `shop`: on a combined sheet the
    #: architect's drawing sits in the vendor's upload, and "page 3" alone would not say which file.
    document_kind: str
    #: Whether a crop of the number's runs is stored. Without one, the reviewer reads the page itself.
    has_crop: bool


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
    #: The sources a value may honestly claim, in the order the form offers them (#827). One means the
    #: form need not ask.
    sources: tuple[SourceOut, ...] = ()
    #: Where the architect's drawing states this setting, while a pointer to it still holds (#866).
    #: `None` when nothing was found, which is every setting until something proposes one.
    found: SettingPointerOut | None = None


class LayoutProposalOut(BaseModel):
    """A model-proposed discriminator answer, still waiting for reviewer confirmation."""

    value: str
    crop_artifact_id: UUID
    model_id: str
    prompt_id: str
    confirmed: bool = False
    requires_confirmation: bool = True


class DiscriminatorOut(BaseModel):
    """A judgement about the drawing that decides which variant of a rule applies."""

    name: str
    rule_ids: tuple[str, ...]
    #: Closed. The resolver matches against the declared variants, so anything else resolves to
    #: nothing and the rule reports NO_APPLICABLE_RULE — which reads as "does not apply here" rather
    #: than "you mistyped the layout".
    choices: tuple[str, ...]
    proposal: LayoutProposalOut | None = None


class ProposedReadingOut(BaseModel):
    """One reading a model proposes for a field, named by the candidate it already is."""

    #: The candidate the extraction layer produced. Not a value a model composed — a model may only
    #: *choose* one of these, and `assignment_tool_schema` puts the real ids in the tool's `enum` so
    #: an invented one is not something it can emit.
    candidate_id: UUID
    value: str
    page_index: int
    #: Position in a many-valued form field. Sparse positions preserve a missing slot rather than
    #: shifting the next sealed width into it.
    position: int | None = None
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
    #: Number of drawing slots represented by this row, including unresolved gaps. Present for
    #: partial slot-reader proposals so the form can keep empty boxes in their original positions.
    expected_count: int | None = None
    values: tuple[ProposedReadingOut, ...]


class SavedValueOut(BaseModel):
    """One value a reviewer saved, exactly as stored, with who saved it and when (#1074).

    `numerator`/`denominator` are the stored number itself, as decimal strings. `text` is that same
    number written the way the form takes it (`25 1/2"`), and reads back to exactly these two numbers
    when sent again: a millimetre value comes back as the inches it was converted to (`984 mm` is
    `38 94/127"`), because the characters first typed are not stored — only the number is.
    """

    numerator: str
    denominator: str
    unit: str
    #: `None` only for a unit the form does not write, which nothing stores today.
    text: str | None
    set_by: str
    set_at: datetime


class SavedQuantityOut(BaseModel):
    """What this review already holds for one quantity of the form, keyed like `QuantityOut.key`.

    A quantity feeds one or more rule inputs (`QuantityOut.consumers`) and the form saves it once per
    input. `values` are those of the input saved most recently — one value, or a run in layout order
    for a many-valued quantity.
    """

    key: str
    values: tuple[SavedValueOut, ...]
    #: Who saved the newest of `values`, and when.
    set_by: str
    set_at: datetime
    #: True only when every rule input this quantity feeds holds exactly these values. False means at
    #: least one check would read nothing, or a different number, so the field is not done yet.
    complete: bool


class SavedParameterOut(BaseModel):
    """The setting value in force for one parameter, as saved, with where it came from (#1074)."""

    name: str
    #: `run` for a value saved for this review only, `project` for one saved for every review of the
    #: project — the layer the form's `scope` filed it in, and the one the checks read it from.
    layer: Literal["project", "run"]
    value: SavedValueOut
    #: Where the value came from (#827), e.g. `G.C / Client`.
    source: str
    reference: str | None = None
    #: The passage the value was typed from and matched (#866), as the `proposal_id` that was sent.
    citation: UUID | None = None


class RequiredInputsOut(BaseModel):
    """Everything the published rulebook needs, grouped so a form can render it.

    Derived from the published rules rather than listed, which is what makes it impossible for a
    field to be missing: a rule that gains an input gains a field here on the next publish.
    """

    quantities: tuple[QuantityOut, ...]
    #: One-based page numbers available for page-scoped review.
    page_numbers: tuple[int, ...] = ()
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

    #: What this review already holds for each quantity of the form, so the form refills after a
    #: reload (#1074). Only quantities with something saved appear; only this package revision's.
    saved_measurements: tuple[SavedQuantityOut, ...] = ()
    #: The saved value in force for each setting of the form (#1074), from the layer its scope
    #: files it in. Only settings with something saved appear.
    saved_parameters: tuple[SavedParameterOut, ...] = ()


class CheckRequest(BaseModel):
    """Asking for the checks, and what the reviewer says about the layout.

    **Discriminators travel with the request rather than being stored as evidence**, because that is
    what they are: a statement about how to read this package on this run. A rule with a discriminator
    nobody stated abstains with REVIEW_REQUIRED however complete the measurements are, so without
    these two `CT-WIDTH-001` and `CAB-FILLER-001` could never reach a verdict.
    """

    model_config = ConfigDict(extra="forbid")

    discriminators: dict[str, str] = Field(default_factory=dict)


class PageProposalIn(BaseModel):
    """Request a proposal for exactly one drawing page."""

    model_config = ConfigDict(extra="forbid")

    page_number: int = Field(ge=1)


class ProposedMeasurementsOut(BaseModel):
    """What survived every structural check, and enough counts to say so honestly.

    **Nothing here is stored.** These fill a form a reviewer then reads, edits and saves; the saving
    is what records a value, and it records it as the reviewer's. A model's proposal never becomes a
    measurement without a person submitting it.
    """

    assignments: tuple[ProposedFieldOut, ...]
    page_number: int = Field(ge=1)
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
