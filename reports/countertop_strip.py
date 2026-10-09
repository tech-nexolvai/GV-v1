"""Geometry for the countertop picture in the signed PDF, the same drawing the screen shows.

This is a port of the frontend's ``stripLayout`` (``frontend/main/src/lib/countertop-strip.ts``),
which follows the shared spec in the vault ("V1 backend - everything left (Codex)", section 4).
The screen's ``CountertopStrip`` and the PDF draw from the same rules, so one countertop row gives
one picture on screen and on paper:

* **Labels are never computed.** Every number written on the picture is the API's exact
  ``display`` text. Numbers are turned into floats only to place boxes, after each one is checked
  to be an exact integer fraction; a value that is not one stops the drawing (``refused``).
* **To scale only when every width is known.** One missing piece means its width is unknown, so
  the whole strip is drawn as equal boxes and says so (``to_scale`` false); a width is never
  invented to keep the picture proportional.
* **One scale for everything.** Pieces, field-cut caps and the printed and needed brackets share
  one scale, so a shortfall or overrun is drawn to its length, from the left wall face.
* **Field-cut caps only at ends that have a wall**, read from the row's wall layout; a layout this
  module does not know draws no walls and no caps rather than guessing an end.

Presentation only: nothing here judges a row or changes a recorded value.

Verification: tests/reports/test_countertop_strip_pdf.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

from app.schemas.visual_ui import CountertopResultOut, ExactValueOut

__all__ = [
    "MIN_CAP",
    "StripDrawn",
    "StripPiece",
    "StripRefused",
    "StripSpan",
    "Walls",
    "inches_of",
    "strip_layout",
    "walls_of",
]

PieceKind = Literal["filler", "cabinet", "appliance", "other"]

MIN_CAP: Final = 6.0
"""Smallest drawn width for a cap, so a 1" field cut on a long run is still visible."""

_INTEGER: Final = re.compile(r"-?[0-9]+")


@dataclass(frozen=True, slots=True)
class Walls:
    back: bool
    left: bool
    right: bool


@dataclass(frozen=True, slots=True)
class StripSpan:
    """A left edge and width in drawing units (0 … ``width``), with its exact label."""

    x: float
    w: float
    label: str


@dataclass(frozen=True, slots=True)
class StripPiece:
    index: int
    x: float
    w: float
    label: str | None
    """The API's exact text, e.g. ``12 5/8"``; ``None`` when the piece was not read."""
    kind: PieceKind
    source: str


@dataclass(frozen=True, slots=True)
class StripRefused:
    reason: str
    status: Literal["refused"] = "refused"


@dataclass(frozen=True, slots=True)
class StripDrawn:
    width: float
    to_scale: bool
    pieces: tuple[StripPiece, ...]
    printed: StripSpan | None
    """The stone as printed, drawn from the left wall face."""
    needed: StripSpan | None
    """Pieces plus field cut, drawn from the left wall face."""
    cap_left: StripSpan | None
    cap_right: StripSpan | None
    walls: Walls | None
    held: bool
    status: Literal["drawn"] = "drawn"

    @property
    def run_end(self) -> float:
        """Where the drawn run (pieces and caps) ends, in drawing units."""
        ends = [piece.x + piece.w for piece in self.pieces]
        if self.cap_right is not None:
            ends.append(self.cap_right.x + self.cap_right.w)
        return max(ends)


def inches_of(value: ExactValueOut | None) -> float | None:
    """The value in inches for placement only, or ``None`` when it is not an exact fraction."""
    if value is None:
        return None
    if not _INTEGER.fullmatch(value.numerator) or not _INTEGER.fullmatch(value.denominator):
        return None
    denominator = int(value.denominator)
    if denominator == 0:
        return None
    return int(value.numerator) / denominator


def walls_of(config: str | None) -> Walls | None:
    """Which ends have a wall, for the layouts the rulebook publishes; anything else is unknown.

    Mirrors the frontend's ``wallsOf``: a layout that does not say which end has a wall is shown as
    not established rather than drawn from a guess.
    """
    if config == "back_left_right":
        return Walls(back=True, left=True, right=True)
    if config == "back_only":
        return Walls(back=True, left=False, right=False)
    if config == "island":
        return Walls(back=False, left=False, right=False)
    return None


def _kind_of(kind: str | None) -> PieceKind:
    if kind == "filler":
        return "filler"
    if kind == "appliance_space":
        return "appliance"
    if kind in ("cabinet", "equal_cabinets"):
        return "cabinet"
    return "other"


def strip_layout(result: CountertopResultOut, width: float) -> StripDrawn | StripRefused:
    """Place the countertop picture for one row in ``width`` drawing units, or refuse to."""
    return _layout(result, width, walls_of(result.wall_layout.config))


def _layout(
    result: CountertopResultOut, width: float, walls: Walls | None
) -> StripDrawn | StripRefused:
    if not result.pieces:
        return StripRefused("Pieces not read")

    values = [None if piece.value is None else inches_of(piece.value) for piece in result.pieces]
    for piece, value in zip(result.pieces, values, strict=True):
        if piece.value is not None and (value is None or value <= 0):
            return StripRefused("A width is not an exact positive number")
    printed_in = inches_of(result.printed_overall)
    needed_in = inches_of(result.expected_total)
    cap_in = inches_of(result.field_cut_per_end)
    if (
        (result.printed_overall is not None and printed_in is None)
        or (result.expected_total is not None and needed_in is None)
        or (result.field_cut_per_end is not None and cap_in is None)
    ):
        return StripRefused("A total is not an exact number")

    has_cap = cap_in is not None and cap_in > 0
    cap_left_on = bool(walls is not None and walls.left and has_cap)
    cap_right_on = bool(walls is not None and walls.right and has_cap)
    cap_text = "" if result.field_cut_per_end is None else f"+{result.field_cut_per_end.display}"
    known = [value for value in values if value is not None]
    to_scale = len(known) == len(values)

    if to_scale:
        caps_in = (cap_in or 0.0) * (int(cap_left_on) + int(cap_right_on))
        total_in = max(sum(known) + caps_in, printed_in or 0.0, needed_in or 0.0)
        scale = width / total_in
        cap_w = max((cap_in or 0.0) * scale, MIN_CAP) if cap_in is not None else 0.0
        widths = [value * scale for value in known]
    else:
        # Schematic: equal boxes, caps at a fixed size, brackets across the whole drawing.
        scale = None
        cap_w = MIN_CAP * 2 if cap_in is not None else 0.0
        inner = width - (cap_w if cap_left_on else 0.0) - (cap_w if cap_right_on else 0.0)
        widths = [inner / len(result.pieces)] * len(result.pieces)

    x = 0.0
    cap_left = None
    if cap_left_on:
        cap_left = StripSpan(x, cap_w, cap_text)
        x += cap_w
    pieces: list[StripPiece] = []
    for piece, piece_w in zip(result.pieces, widths, strict=True):
        pieces.append(
            StripPiece(
                index=piece.index,
                x=x,
                w=piece_w,
                label=None if piece.value is None else piece.value.display,
                kind=_kind_of(piece.kind),
                source=piece.source,
            )
        )
        x += piece_w
    cap_right = StripSpan(x, cap_w, cap_text) if cap_right_on else None

    def bracket(value: ExactValueOut | None, inches: float | None) -> StripSpan | None:
        if value is None or inches is None:
            return None
        return StripSpan(0.0, width if scale is None else inches * scale, value.display)

    return StripDrawn(
        width=width,
        to_scale=to_scale,
        pieces=tuple(pieces),
        printed=bracket(result.printed_overall, printed_in),
        needed=bracket(result.expected_total, needed_in),
        cap_left=cap_left,
        cap_right=cap_right,
        walls=walls,
        held=result.hold is not None,
    )
