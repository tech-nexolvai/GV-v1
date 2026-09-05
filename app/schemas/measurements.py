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


class RequiredInputsOut(BaseModel):
    """Everything the published rulebook needs, grouped so a form can render it.

    Derived from the published rules rather than listed, which is what makes it impossible for a
    field to be missing: a rule that gains an input gains a field here on the next publish.
    """

    quantities: tuple[QuantityOut, ...]
    parameters: tuple[ParameterOut, ...]
    discriminators: tuple[DiscriminatorOut, ...]
    #: How many rules are published. Zero means the form is empty because nothing is published, which
    #: is a different problem from a rulebook that asks for nothing.
    rules_published: int


class CheckRequest(BaseModel):
    """Asking for the checks, and what the reviewer says about the layout.

    **Discriminators travel with the request rather than being stored as evidence**, because that is
    what they are: a statement about how to read this package on this run. A rule with a discriminator
    nobody stated abstains with REVIEW_REQUIRED however complete the measurements are, so without
    these two `CT-WIDTH-001` and `CAB-FILLER-001` could never reach a verdict.
    """

    model_config = ConfigDict(extra="forbid")

    discriminators: dict[str, str] = Field(default_factory=dict)
