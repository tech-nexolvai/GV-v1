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

    @field_validator("kind", mode="before")
    @classmethod
    def overall_is_not_a_piece_kind(cls, value: object) -> object:
        """A reader that labels the overall width "overall" has not misread anything.

        Kind is a suggestion that never sets a value, and the overall has no piece kind at all, so
        this word means "unknown" rather than a malformed answer (#977).
        """
        return "unknown" if value == "overall" else value

    @field_validator("box", mode="before")
    @classmethod
    def unusable_box_is_no_box(cls, value: object) -> object:
        """A box outside the 0..1000 grid, or not four whole numbers, is dropped, not fatal.

        The box is a display hint only; the printed text decides. A reader that gets the grid wrong
        (Kimi often answers in pixels) must not have a correct reading thrown away for it (#977).
        """
        if value is None:
            return None
        if isinstance(value, (list, tuple)) and len(value) == 4:
            coordinates = list(value)
        elif isinstance(value, dict) and set(value) == {"x0", "y0", "x1", "y1"}:
            coordinates = [value["x0"], value["y0"], value["x1"], value["y1"]]
        else:
            return None
        if not all(
            isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 1000 for c in coordinates
        ):
            return None
        x0, y0, x1, y1 = coordinates
        if x1 <= x0 or y1 <= y0:
            return None
        return value


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
