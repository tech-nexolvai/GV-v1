"""Strict page-form response schema. Model coordinates are placement hints, never measurements."""

from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)


class NormalizedBox(BaseModel):
    """Qwen prompt-v5 box on its 0..1000 rendered-page grid."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    x0: StrictInt = Field(ge=0, le=1000)
    y0: StrictInt = Field(ge=0, le=1000)
    x1: StrictInt = Field(ge=0, le=1000)
    y1: StrictInt = Field(ge=0, le=1000)

    @model_validator(mode="before")
    @classmethod
    def from_v5_array(cls, value: object) -> object:
        """Prompt v5 emits the compact four-integer list, not a named-object mapping."""
        if isinstance(value, (list, tuple)) and len(value) == 4:
            return dict(zip(("x0", "y0", "x1", "y1"), value, strict=True))
        return value

    @model_validator(mode="after")
    def positive_extent(self) -> NormalizedBox:
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError("box must have positive width and height")
        return self


class FormDimension(BaseModel):
    """One printed form value plus the reader's non-authoritative observations."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    text: StrictStr | None
    whole: StrictInt | None
    numerator: StrictInt | None
    denominator: StrictInt | None
    stacked: StrictBool
    kind: Literal[
        "filler",
        "cabinet",
        "cabinets_equal",
        "appliance_space",
        "end_panel",
        "unknown",
    ]
    combined: StrictBool
    readable: StrictBool
    box: NormalizedBox | None
    position: StrictInt

    @field_validator("whole", "numerator", "denominator", mode="before")
    @classmethod
    def parse_prompt_integer(cls, value: object) -> object:
        """Accept v5's digit strings/empty strings without using them to construct a value."""
        if value is None or value == "":
            return None
        if isinstance(value, str) and value.isascii() and value.isdigit():
            return int(value)
        return value

    @field_validator("text", mode="before")
    @classmethod
    def empty_text_is_missing(cls, value: object) -> object:
        return None if value == "" else value


class CountertopForm(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    overall: FormDimension | None
    overall_scope: Literal["run", "wall", "unknown"] | None
    chain: list[FormDimension]
    view_title: StrictStr = ""


class PageFormAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    page_index: StrictInt = Field(ge=0)
    countertops: list[CountertopForm]
    notes: StrictStr = ""
