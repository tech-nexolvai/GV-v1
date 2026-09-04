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

from pydantic import BaseModel, ConfigDict, Field


class ParameterEntry(BaseModel):
    """One project setting a reviewer supplies for this job — a cabinet depth, an overhang."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    value: str = Field(
        min_length=1,
        max_length=100,
        description='The value as typed, carrying its unit: 24", 610 mm.',
    )


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
    value: str = Field(
        min_length=1, max_length=100, description="The value as typed, with its unit."
    )


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


class ReviewerEntryOut(BaseModel):
    """What was stored, echoed back exactly so a client can show what the system understood.

    Echoing the parse rather than the input is the point: `25.5"` and `25 1/2"` are the same value and
    a reviewer should be able to see that the system agrees.
    """

    parameter_set_version: int | None
    measurement_set_version: int | None
    parameters: tuple[StoredValue, ...]
    measurements: tuple[StoredValue, ...]
