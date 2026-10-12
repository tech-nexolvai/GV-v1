"""Which ends of a countertop row stand against a wall: two judgments, never one (#992, #1202).

**The question, and why two readers.** CT-WIDTH-001 adds a field cut per wall end, so its variant
needs the wall layout (`wall_config`). E3 (2026-10-07) showed both readers the row with its ends
marked and the whole vendor view, and asked one narrow question per end. A side counts only when
both readers, of different makers, say the same "yes" or the same "no".

**What the two readers' agreement gives** (admin's rule), always as a *proposal* the row's reviewer
confirms with one click (`workflow/slot_row_scope.effective_row_wall`):
- both ends "yes" → `back_left_right`;
- left "yes" and right agreed "no" → `back_and_left`; the reverse → `back_and_right` (#1138: one
  wall end is one field cut). An open end is never inferred from absence;
- both ends "no" → `back_only` **only** when both readers call the view a plan and both see the
  back wall;
- anything else → the person.

**The one automatic layout (#1202, decided 2026-10-13).** The walls research found that both AIs
agreeing always matched what the vendor drew, and was wrong exactly when the vendor drew a wall
that is not there (an island drawn with a wall at one end). Code's own wall ink seen at *both* ends
was never wrong. So `back_left_right` is set without a click only on two independent judgments:
- **code** sees wall ink at both row ends, in black or grey vendor ink only (`WallInkSeen`): a hatch
  whose strokes run up the end, a solid grey or black band, or a tall wall line rising above the
  cabinets beside the end. Never words, never colour, never absence;
- **both AIs** say "yes" at the left end and at the right end;
- and nothing says the stone stops at panels or runs into wall pockets (`stone_meets_walls`).

Every other layout, and every other combination, goes to the reviewer. Words at the ends (a
filler, a field cut, the word WALL) are only evidence for the reviewer: they were wrong at free ends.

**Code may object.** Where code sees wall ink at an end and a reader says "no wall" there, the row
goes to the person. Seeing nothing proves nothing: walls are often not drawn.

**Pure.** No I/O and no float: the geometry is `Decimal`, the hatch's angles are binned against
written-down tangents instead of computed with trigonometry. The pictures are cut by the caller from
the boxes this module gives.

Source: issues #992, #1202 · Verification: `tests/extraction/slot_reader/test_walls.py`
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, fields
from decimal import Decimal
from enum import StrEnum
from typing import Final, Literal

from evidence.corroborate import UNKNOWN_MODEL_VENDOR, independence_key
from extraction.geometry.rows import Box, CountertopRowCandidate, PageInk
from vocabulary.wall_layouts import AUTOMATIC_WALL_SOURCE, READERS_WALL_SOURCE

__all__ = [
    "AUTOMATIC_WALL_SOURCE",
    "BACK_AND_LEFT",
    "BACK_AND_RIGHT",
    "BACK_LEFT_RIGHT",
    "BACK_ONLY",
    "E3_WALL_SETTINGS",
    "READERS_WALL_SOURCE",
    "WALL_PROMPT",
    "WALL_PROMPT_ID",
    "WALL_PROMPT_IDS",
    "HatchSeen",
    "Side",
    "WallAnswer",
    "WallInkSeen",
    "WallOutcome",
    "WallPictures",
    "WallSettings",
    "band_at",
    "hatch_at",
    "seal_walls",
    "tall_line_at",
    "wall_pictures",
]

WALL_PROMPT_ID: Final = "slot-walls-v2"
"""v2 (#1111): every answer field is defined, each with one example of a yes and one of a no, all
invented. v1 never said what `view` means (yet two readers saying `plan` can seal `back_only`), how
to judge a back wall in an elevation, or that a wall-to-wall dimension shows a wall only at an end
it meets: a run can be shorter than wall to wall. It also names the colour of our marks (magenta,
which no reviewer markup uses) and says that the vendor's own words at an end may decide that wall
(`_code_wall_clues` and `_read_wall_clues` in `workflow/slot_reader.py`; `seal_walls` is unchanged).
v1 (#992): E3's question (2026-10-07, `research/E3/ask.py`)."""
#: Earlier wordings, still recognised when a stored run is replayed. Their answers have one shape.
WALL_PROMPT_IDS: Final = frozenset({WALL_PROMPT_ID, "slot-walls-v1"})
BACK_LEFT_RIGHT: Final = "back_left_right"
BACK_AND_LEFT: Final = "back_and_left"
BACK_AND_RIGHT: Final = "back_and_right"
BACK_ONLY: Final = "back_only"

#: The wall question, generic: no client value or drawing; every example is invented.
WALL_PROMPT: Final = (
    "These pictures come from a cabinet maker's shop drawing for a stone countertop job. Our marks "
    "are drawn in magenta (a bright pink-purple), a colour no reviewer markup uses. Red or yellow "
    "marks are a reviewer's markup, not the vendor's drawing: ignore them.\n"
    "Picture 1 shows one countertop width row, the RUN: the magenta horizontal line, with two "
    "magenta vertical lines marking its LEFT and RIGHT ends. Picture 1 also shows the drawing "
    "beyond each end, about half the run's width again: that is the neighbourhood of the run, "
    "not the run.\n"
    "Picture 2 shows the whole vendor view the run belongs to (the run is the magenta line).\n\n"
    "Count only evidence you can see: a hatched wall section, a wall line, the word WALL, a "
    "wall-to-wall dimension, or a plan view showing the walls. A wall-to-wall dimension is "
    "evidence for an end ONLY when its end tick or arrow meets that end of the run (the magenta "
    "vertical): a run can be shorter than wall to wall, and a wall-to-wall dimension that ends "
    "beyond the run's end says nothing about that end.\n"
    'Answer each side "yes", "no" (you can see the end is open: nothing stands beside it) or '
    '"unsure" (the drawing does not show it). The fields:\n'
    '- "left": is there a wall at the LEFT end of the run, at the left magenta vertical? '
    "Example yes: "
    "a band of hatching touches the left vertical. Example no: beyond the left vertical the floor "
    "line runs on with nothing standing on it.\n"
    '- "right": the same question at the RIGHT end, at the right magenta vertical. Example yes: '
    "a wall line stands at the right vertical with the word WALL beside it. Example no: the run "
    "ends at a finished end panel and open floor is drawn beyond it.\n"
    '- "behind": is there a wall BEHIND the run, along its whole length (a back wall)? In a plan '
    "(top) view it is the wall line or hatched band along the back edge of the countertop. In an "
    "elevation (front view) you look straight at the back wall, so it seldom shows as a line: "
    'answer "yes" there only when something drawn says so, such as the word WALL on the surface '
    'behind the cabinets or wall cabinets hung on that surface; answer "no" only when the drawing '
    'shows open space behind the run, such as an island; otherwise "unsure". Example yes: a plan '
    "view with a hatched wall band along the countertop's back edge. Example no: a plan view of "
    "an island with open floor on every side.\n"
    '- "view": what kind of drawing Picture 2 is, where the run is drawn. "elevation": a front '
    'view, looking at the cabinet fronts (doors, drawers, heights). "plan": a top view, looking '
    "down on the countertop's outline and depth, with the walls cut through as lines or hatched "
    'bands. "other": anything else, such as a section, a detail, or a view you cannot name. '
    "Example plan: a top view of an outlined counter with a sink cut-out and hatched walls "
    'around it. Example not plan ("elevation"): a view of door and drawer fronts standing on a '
    "floor line.\n"
    '- "left_evidence", "right_evidence", "behind_evidence": a few words (at most twelve) saying '
    'what you saw that decided that answer, or for unsure what is missing. Example: "hatched '
    'wall band touches the left end". Not an example: "yes" or "there is a wall", which repeat '
    "the answer without saying what you saw.\n"
    "Code also reads the vendor's own words at the run's end pieces: a filler, a field cut or the "
    "word WALL at an end counts as a wall there, and when both ends have such words code decides "
    "the walls whatever the answers say. Still answer only from what you see in the pictures.\n"
    "Return ONLY this JSON:\n"
    '{"left": "yes|no|unsure", "right": "yes|no|unsure", "behind": "yes|no|unsure",\n'
    ' "left_evidence": "at most 12 words", "right_evidence": "at most 12 words", '
    '"behind_evidence": "at most 12 words",\n'
    ' "view": "elevation" | "plan" | "other"}'
)


class Side(StrEnum):
    YES = "yes"
    NO = "no"
    UNSURE = "unsure"


type View = Literal["elevation", "plan", "other"]


@dataclass(frozen=True, slots=True)
class WallAnswer:
    """What one reader said about one row's walls. Evidence words are kept for the person only."""

    model_id: str
    left: Side
    right: Side
    behind: Side
    view: View
    evidence: tuple[str, str, str] = ("", "", "")
    """(left, right, behind) in the reader's words."""


@dataclass(frozen=True, slots=True)
class HatchSeen:
    left: bool
    right: bool
    why_left: str = ""
    why_right: str = ""


@dataclass(frozen=True, slots=True)
class WallInkSeen:
    """Code's own judgment (#1202): wall ink seen beside each row end, positive only.

    `True` at an end means a hatch running up the end, a solid grey or black band, or a tall wall
    line was seen there in the vendor's black or grey ink. `False` means none was seen, which says
    nothing about the end: walls are often not drawn.
    """

    left: bool
    right: bool
    why_left: str = ""
    why_right: str = ""

    @property
    def both_ends(self) -> bool:
        return self.left and self.right


@dataclass(frozen=True, slots=True)
class WallOutcome:
    """A sealed layout, or why the person decides."""

    config: str | None
    code: str | None
    reason: str | None
    left: Side | None
    """The side both readers agreed on; `None` where they did not."""
    right: Side | None
    behind: Side | None
    source: str | None = None
    """Who set it: `AUTOMATIC_WALL_SOURCE` (code's ink at both ends and both AIs, the one layout a
    check uses without a click) or `READERS_WALL_SOURCE` (the readers agreed; a proposal for the
    reviewer). Never a human confirmation. Runs before #1202 also stored `vendor-drawing-clues`
    and `drawing-and-readers`; a check no longer uses either without a click."""


def _maker(model_id: str) -> str:
    return independence_key("bedrock-slot-reader", model_id)


_CLAUDE_PAIR: Final = frozenset({"anthropic.claude-opus-5-5", "anthropic.claude-sonnet-5-5"})


def _agreed(first: Side, second: Side) -> Side | None:
    return first if first == second and first is not Side.UNSURE else None


def seal_walls(
    answers: Sequence[WallAnswer],
    *,
    ink: WallInkSeen | None,
    row_ambiguity: str | None,
    stone_meets_walls: bool = False,
    allow_claude_pair: bool = False,
) -> WallOutcome:
    """The row's wall layout when two makers agree and nothing objects; otherwise why not.

    `answers` holds the readers that answered (an abstention is absent). `ink` is code's wall-ink
    check at the two ends, `None` when it could not run (which objects to nothing and makes
    nothing automatic). `stone_meets_walls` is `True` only when both readers answered the counter
    question and neither, nor code, says the stone stops at panels or runs into wall pockets.

    Only `back_left_right` with code's ink at both ends, both AIs' "yes" at both ends and
    `stone_meets_walls` is automatic (`AUTOMATIC_WALL_SOURCE`); every other sealed layout is the
    readers' proposal (`READERS_WALL_SOURCE`), which the reviewer confirms.
    """
    if len(answers) > 2:
        raise ValueError("a row's walls are asked of exactly two readers")
    left = right = behind = None
    if len(answers) == 2:
        first, second = answers
        left = _agreed(first.left, second.left)
        right = _agreed(first.right, second.right)
        behind = _agreed(first.behind, second.behind)

    def held(code: str, reason: str) -> WallOutcome:
        return WallOutcome(None, code, reason, left, right, behind)

    if len(answers) == 2:
        makers = {_maker(answer.model_id) for answer in answers}
        claude_pair = allow_claude_pair and {answer.model_id for answer in answers} == _CLAUDE_PAIR
        if not claude_pair and (UNKNOWN_MODEL_VENDOR in makers or len(makers) != 2):
            return held(
                "reader-independence",
                "the wall answers are not from two known, different makers; reviewer must choose",
            )
    if len(answers) < 2:
        return held("one-reader-missing", "only one reader answered about the walls")
    if row_ambiguity is not None:
        return held("row-ambiguous", f"not sure this row is the countertop: {row_ambiguity}")
    if ink is not None:
        for side, seen in (("left", ink.left), ("right", ink.right)):
            if seen and any(getattr(answer, side) is Side.NO for answer in answers):
                # The code kept from #992's hatch check, so stored runs and screens read the same.
                return held(
                    "hatch-contradiction",
                    f"code sees wall ink at the {side} end, but a reader says no wall there",
                )
    if left is Side.YES and right is Side.YES:
        automatic = ink is not None and ink.both_ends and stone_meets_walls
        return WallOutcome(
            BACK_LEFT_RIGHT,
            None,
            None,
            left,
            right,
            behind,
            AUTOMATIC_WALL_SOURCE if automatic else READERS_WALL_SOURCE,
        )
    if left is Side.YES and right is Side.NO:
        return WallOutcome(BACK_AND_LEFT, None, None, left, right, behind, READERS_WALL_SOURCE)
    if left is Side.NO and right is Side.YES:
        return WallOutcome(BACK_AND_RIGHT, None, None, left, right, behind, READERS_WALL_SOURCE)
    if left is Side.NO and right is Side.NO:
        if behind is Side.YES and all(answer.view == "plan" for answer in answers):
            return WallOutcome(BACK_ONLY, None, None, left, right, behind, READERS_WALL_SOURCE)
        return held(
            "back-wall-unknown",
            "no wall at either end, and the drawing does not show whether there is a back wall",
        )
    return held("walls-not-agreed", "the readers do not agree on the wall at each end")


@dataclass(frozen=True, slots=True)
class WallSettings:
    """Every length the wall pictures and the hatch check use, in page points unless named.

    No defaults: E3's values are written down in `E3_WALL_SETTINGS`.
    """

    frame_slack_pt: Decimal
    """A row belongs to the pasted drawing whose box holds it, within this."""
    extent_gap_pt: Decimal
    """Ink continues away from the row until a gap this wide: that is the elevation's height."""
    row_context_fraction: Decimal
    row_context_pt: Decimal
    """The row picture reaches this fraction of the row's width, plus these points, past each end."""
    row_above_pt: Decimal
    row_below_pt: Decimal
    """...and this far above its top and below its bottom."""
    label_band_pt: Decimal
    """The row's labels stand within this of its line, on the side away from the drawing."""
    hatch_reach_pt: Decimal
    hatch_inside_pt: Decimal
    """Hatch strokes are looked for this far outside a row end, and this far inside it."""
    hatch_stroke_min_pt: Decimal
    hatch_stroke_max_pt: Decimal
    """A hatch stroke is this long at least (shorter is a glyph or a tick), and at most this."""
    hatch_axis_min_pt: Decimal
    """...and slanted: at least this far along both axes."""
    hatch_parallel_count: int
    hatch_cross_count: int
    hatch_span_pt: Decimal
    """A hatch: this many parallel strokes in one 5-degree bin, or two bins about 90 degrees apart
    with `hatch_cross_count` each, spread over this much height."""
    hatch_cross_slack_degrees: int
    hatch_through_pt: Decimal
    hatch_through_fraction: Decimal
    """A hatch that runs on into the row is a band passing the end (a plan's back wall), not a wall
    at it: the same strokes are counted this far inside the row, past `hatch_inside_pt`, and a
    count of at least this fraction of the hatch's own refuses it (#1202)."""
    band_max_lightness: Decimal
    band_min_width_pt: Decimal
    band_height_fraction: Decimal
    """A solid wall band: a path filled in black or grey no lighter than this (0 black, 1 white),
    at least this wide, taller than it is wide, standing beside the end (within `hatch_reach_pt`
    outside, `hatch_inside_pt` inside, never reaching further into the row), over at least this
    fraction of the drawing's height (#1202)."""
    tall_rise_pt: Decimal
    tall_casework_fraction: Decimal
    tall_casework_gap_pt: Decimal
    tall_floor_fraction: Decimal
    tall_frame_fraction: Decimal
    tall_line_min_pt: Decimal
    tall_inside_pt: Decimal
    tall_band_slack_pt: Decimal
    vertical_slack_pt: Decimal
    """A tall wall line: a vertical stroke (off upright by at most `vertical_slack_pt`, at least
    `tall_line_min_pt` long) within `hatch_reach_pt` outside the end or `tall_inside_pt` inside it,
    whose top rises at least `tall_rise_pt` above the cabinets beside the end (the ink starting
    `tall_casework_gap_pt` inside the end, over `tall_casework_fraction` of the row's width, whose
    top is within `tall_band_slack_pt` of the drawing's height) and
    whose bottom reaches the floor (within `tall_floor_fraction` of the drawing's height). A stroke
    as tall as `tall_frame_fraction` of the pasted drawing is its frame, not a wall (#1202)."""
    picture_max_side_px: int
    """A picture is shrunk until its longer side is at most this."""

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if field.type in ("int",):
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise TypeError(f"{field.name} must be a positive int")
            elif not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise TypeError(f"{field.name} must be a finite Decimal, zero or more")

    @property
    def config_hash(self) -> str:
        return ";".join(f"{field.name}={getattr(self, field.name)}" for field in fields(self))


#: E3's values (2026-10-07, `research/E3/build.py`), measured on both client sets; the hatch's
#: band test, the solid band and the tall line from the walls research (2026-10-13, #1202).
E3_WALL_SETTINGS = WallSettings(
    frame_slack_pt=Decimal(2),
    extent_gap_pt=Decimal(22),
    row_context_fraction=Decimal("0.45"),
    row_context_pt=Decimal(30),
    row_above_pt=Decimal(25),
    row_below_pt=Decimal(10),
    label_band_pt=Decimal(14),
    hatch_reach_pt=Decimal(30),
    hatch_inside_pt=Decimal(4),
    hatch_stroke_min_pt=Decimal(5),
    hatch_stroke_max_pt=Decimal(60),
    hatch_axis_min_pt=Decimal("0.5"),
    hatch_parallel_count=12,
    hatch_cross_count=8,
    hatch_span_pt=Decimal(40),
    hatch_cross_slack_degrees=10,
    hatch_through_pt=Decimal(30),
    hatch_through_fraction=Decimal("0.5"),
    band_max_lightness=Decimal("0.9"),
    band_min_width_pt=Decimal("1.5"),
    band_height_fraction=Decimal("0.4"),
    tall_rise_pt=Decimal(40),
    tall_casework_fraction=Decimal("0.15"),
    tall_casework_gap_pt=Decimal(2),
    tall_floor_fraction=Decimal("0.25"),
    tall_frame_fraction=Decimal("0.9"),
    tall_line_min_pt=Decimal(20),
    tall_inside_pt=Decimal("1.5"),
    tall_band_slack_pt=Decimal(5),
    vertical_slack_pt=Decimal("0.5"),
    picture_max_side_px=1600,
)

#: tan(2.5 + 5k degrees), k = 0..17: the edges of the 5-degree bins a stroke's slope falls into.
_BIN_EDGES: Final = (
    Decimal("0.0436609429"),
    Decimal("0.1316524976"),
    Decimal("0.2216946626"),
    Decimal("0.3152987889"),
    Decimal("0.4142135624"),
    Decimal("0.5205670506"),
    Decimal("0.6370702608"),
    Decimal("0.7673269880"),
    Decimal("0.9163311740"),
    Decimal("1.0913085011"),
    Decimal("1.3032253728"),
    Decimal("1.5696855771"),
    Decimal("1.9209821270"),
    Decimal("2.4142135624"),
    Decimal("3.1715948024"),
    Decimal("4.5107085037"),
    Decimal("7.5957541127"),
    Decimal("22.9037655484"),
)


def _bin(dx: Decimal, dy: Decimal) -> int:
    """The stroke's direction, to the nearest 5 degrees, in [0, 180)."""
    if dx < 0:
        dx, dy = -dx, -dy
    slope = abs(dy / dx)
    steps = sum(1 for edge in _BIN_EDGES if slope > edge)
    degrees = 5 * steps
    return degrees % 180 if dy >= 0 else (180 - degrees) % 180


@dataclass(frozen=True, slots=True)
class _Stroke:
    x: Decimal
    y: Decimal
    bin: int
    box: Box


def _segments(ink: PageInk) -> list[tuple[tuple[Decimal, Decimal], tuple[Decimal, Decimal]]]:
    """Every straight piece of the drawing's ink: its lines, and each side of its paths."""
    segments: list[tuple[tuple[Decimal, Decimal], tuple[Decimal, Decimal]]] = [
        ((line.x0, line.y0), (line.x1, line.y1)) for line in ink.lines
    ]
    for curve in ink.curves:
        segments.extend(zip(curve.points, curve.points[1:], strict=False))
        if curve.closed and len(curve.points) > 2:
            segments.append((curve.points[-1], curve.points[0]))
    return segments


def _strokes(ink: PageInk, settings: WallSettings) -> list[_Stroke]:
    low = settings.hatch_stroke_min_pt * settings.hatch_stroke_min_pt
    high = settings.hatch_stroke_max_pt * settings.hatch_stroke_max_pt
    out: list[_Stroke] = []
    for (ax, ay), (bx, by) in _segments(ink):
        dx, dy = bx - ax, by - ay
        if abs(dx) < settings.hatch_axis_min_pt or abs(dy) < settings.hatch_axis_min_pt:
            continue
        if not low <= dx * dx + dy * dy <= high:
            continue
        out.append(
            _Stroke(
                (ax + bx) / 2,
                (ay + by) / 2,
                _bin(dx, dy),
                Box(min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)),
            )
        )
    return out


def _end_window(
    x_end: Decimal, side: Literal["left", "right"], settings: WallSettings
) -> tuple[Decimal, Decimal]:
    """Beside the end: `hatch_reach_pt` outside it, `hatch_inside_pt` inside it."""
    if side == "left":
        return x_end - settings.hatch_reach_pt, x_end + settings.hatch_inside_pt
    return x_end - settings.hatch_inside_pt, x_end + settings.hatch_reach_pt


def hatch_at(
    ink: PageInk,
    *,
    x_end: Decimal,
    side: Literal["left", "right"],
    y_low: Decimal,
    y_high: Decimal,
    settings: WallSettings,
) -> tuple[bool, str]:
    """Whether a wall's hatching stands beside the row end at `x_end`, and in plain words why.

    Many parallel strokes (or a cross-hatch) beside the end, over the drawing's height, that also
    form a band running **up** the end (#1202): taller than wide, and not running on into the row.
    A plan's back wall drawn across the end window is a band lying across it, not a wall at it.
    """
    x_low, x_high = _end_window(x_end, side, settings)
    through = settings.hatch_through_pt
    bins: dict[int, list[_Stroke]] = {}
    inside: dict[int, int] = {}
    for stroke in _strokes(ink, settings):
        if not y_low <= stroke.y <= y_high:
            continue
        if x_low <= stroke.x <= x_high:
            bins.setdefault(stroke.bin, []).append(stroke)
        elif (side == "left" and x_high < stroke.x <= x_high + through) or (
            side == "right" and x_low - through <= stroke.x < x_low
        ):
            inside[stroke.bin] = inside.get(stroke.bin, 0) + 1
    if not bins:
        return False, "no slanted strokes beside the end"
    ranked = sorted(bins.items(), key=lambda item: (-len(item[1]), item[0]))

    def span(strokes: Sequence[_Stroke]) -> Decimal:
        return max(stroke.y for stroke in strokes) - min(stroke.y for stroke in strokes)

    first_bin, first = ranked[0]
    family: list[_Stroke] | None = None
    family_bins: tuple[int, ...] = ()
    why = ""
    if len(first) >= settings.hatch_parallel_count and span(first) >= settings.hatch_span_pt:
        family, family_bins = first, (first_bin,)
        why = f"{len(first)} parallel strokes at {first_bin} degrees"
    elif len(ranked) > 1:
        second_bin, second = ranked[1]
        apart = abs(first_bin - second_bin)
        if (
            len(first) >= settings.hatch_cross_count
            and len(second) >= settings.hatch_cross_count
            and abs(apart - 90) <= settings.hatch_cross_slack_degrees
            and span([*first, *second]) >= settings.hatch_span_pt
        ):
            family, family_bins = [*first, *second], (first_bin, second_bin)
            why = f"a cross-hatch of {len(first)} and {len(second)} strokes"
    if family is None:
        return False, f"at most {len(first)} parallel strokes"
    width = max(stroke.box.x1 for stroke in family) - min(stroke.box.x0 for stroke in family)
    height = max(stroke.box.bottom for stroke in family) - min(stroke.box.top for stroke in family)
    if height < width:
        return False, f"{why}, but lying across the end, not up it"
    running_on = sum(inside.get(item, 0) for item in family_bins)
    if running_on >= settings.hatch_through_fraction * len(family):
        return False, f"{why}, but the hatch runs on into the row: a band passing the end"
    return True, why


def band_at(
    ink: PageInk,
    *,
    x_end: Decimal,
    side: Literal["left", "right"],
    y_low: Decimal,
    y_high: Decimal,
    settings: WallSettings,
) -> tuple[bool, str]:
    """Whether a solid grey or black band stands beside the row end, and in plain words why (#1202).

    A path filled in black or grey, taller than wide, standing beside the end and never reaching
    into the row, over a good part of the drawing's height. A white or near-white fill is paper.
    """
    height = y_high - y_low
    if height <= 0:
        return False, "no drawing height beside the end"
    x_low, x_high = _end_window(x_end, side, settings)
    for curve in ink.curves:
        lightness = curve.fill_lightness
        if lightness is None or lightness > settings.band_max_lightness:
            continue
        box = curve.box
        if box.width < settings.band_min_width_pt or box.height < box.width:
            continue
        beside = x_low <= box.x1 <= x_high if side == "left" else x_low <= box.x0 <= x_high
        if not beside:
            continue
        overlap = min(box.bottom, y_high) - max(box.top, y_low)
        if overlap >= settings.band_height_fraction * height:
            return True, "a solid grey or black band beside the end"
    return False, "no solid band beside the end"


def _vertical_strokes(
    ink: PageInk, settings: WallSettings
) -> list[tuple[Decimal, Decimal, Decimal]]:
    """`(x, top, bottom)` of every upright straight piece of ink at least `tall_line_min_pt` long."""
    out: list[tuple[Decimal, Decimal, Decimal]] = []
    for (ax, ay), (bx, by) in _segments(ink):
        if abs(ax - bx) > settings.vertical_slack_pt:
            continue
        top, bottom = min(ay, by), max(ay, by)
        if bottom - top < settings.tall_line_min_pt:
            continue
        out.append(((ax + bx) / 2, top, bottom))
    return out


def tall_line_at(
    ink: PageInk,
    *,
    x_end: Decimal,
    side: Literal["left", "right"],
    row_width: Decimal,
    y_low: Decimal,
    y_high: Decimal,
    frame_height: Decimal,
    settings: WallSettings,
) -> tuple[bool, str]:
    """Whether a tall plain wall line stands at the row end, and in plain words why (#1202).

    An upright line just outside the end (or on it) that rises well above the cabinets beside the
    end and comes down to the floor: a wall drawn as plain lines or a tall empty box. `y_high` is
    the floor side of the drawing. The pasted drawing's own frame is never a wall.
    """
    height = y_high - y_low
    if height <= 0:
        return False, "no drawing height beside the end"
    if side == "left":
        cab_low = x_end + settings.tall_casework_gap_pt
        cab_high = x_end + settings.tall_casework_fraction * row_width
        x_low, x_high = x_end - settings.hatch_reach_pt, x_end + settings.tall_inside_pt
    else:
        cab_low = x_end - settings.tall_casework_fraction * row_width
        cab_high = x_end - settings.tall_casework_gap_pt
        x_low, x_high = x_end - settings.tall_inside_pt, x_end + settings.hatch_reach_pt
    slack = settings.vertical_slack_pt
    band = settings.tall_band_slack_pt
    casework_tops = [
        box.top
        for box in _shape_boxes(ink)
        if box.x0 >= cab_low - slack
        and box.x1 <= cab_high + slack
        and y_low - band <= box.top <= y_high + band
        and box.width * 2 < row_width
    ]
    if not casework_tops:
        return False, "nothing drawn beside the end to measure a wall line against"
    casework_top = min(casework_tops)
    floor_reach = y_high - settings.tall_floor_fraction * height
    frame = settings.tall_frame_fraction * frame_height
    for x, top, bottom in _vertical_strokes(ink, settings):
        if not x_low <= x <= x_high or bottom < floor_reach or bottom - top >= frame:
            continue
        if top <= casework_top - settings.tall_rise_pt:
            return True, "a tall wall line rising above the cabinets"
    return False, "no tall wall line at the end"


def _shape_boxes(ink: PageInk) -> list[Box]:
    """The boxes of the drawing's lines and paths (no characters): what the cabinets are drawn by."""
    boxes = [
        Box(
            min(line.x0, line.x1),
            min(line.y0, line.y1),
            max(line.x0, line.x1),
            max(line.y0, line.y1),
        )
        for line in ink.lines
    ]
    boxes.extend(curve.box for curve in ink.curves)
    return boxes


@dataclass(frozen=True, slots=True)
class WallPictures:
    """Where the two pictures are cut, in page points (pdfplumber's frame), the hatch check, and
    code's wall-ink judgment at each end (#1202)."""

    row: Box
    view: Box
    hatch: HatchSeen
    ink: WallInkSeen


def _ink_boxes(ink: PageInk) -> list[Box]:
    boxes = _shape_boxes(ink)
    boxes.extend(char.box for char in ink.characters)
    return boxes


def _reach(
    boxes: Sequence[Box],
    x0: Decimal,
    x1: Decimal,
    y: Decimal,
    up: bool,
    limit: Decimal,
    gap: Decimal,
) -> Decimal:
    """How far ink continues from the row's line, upwards or downwards, before a gap of `gap`."""
    spans: list[tuple[Decimal, Decimal]] = []
    one = Decimal(1)
    for box in boxes:
        if box.x1 < x0 or box.x0 > x1:
            continue
        if up and box.bottom <= y + one and box.top >= limit:
            spans.append((y - box.bottom, y - box.top))
        if not up and box.top >= y - one and box.bottom <= limit:
            spans.append((box.top - y, box.bottom - y))
    spans.sort()
    reach = Decimal(0)
    for near, far in spans:
        if near > reach + gap:
            break
        reach = max(reach, far)
    return reach


def wall_pictures(
    row: CountertopRowCandidate, ink: PageInk, *, settings: WallSettings
) -> WallPictures:
    """The row picture (its ends and the drawing beside them), the vendor view holding the row, and
    code's hatch check and wall-ink judgment at both ends — all from the drawing's own ink, never a
    model's choice."""
    slack = settings.frame_slack_pt
    xs0, xs1, y = row.x0, row.x1, row.y
    frame = next(
        (
            box
            for box in ink.drawing_boxes
            if box.x0 - slack <= xs0
            and xs1 <= box.x1 + slack
            and box.top - slack <= y <= box.bottom + slack
        ),
        Box(Decimal(0), Decimal(0), ink.width, ink.height),
    )
    boxes = _ink_boxes(ink)
    one = Decimal(1)
    up = _reach(boxes, xs0 + one, xs1 - one, y, True, frame.top, settings.extent_gap_pt)
    down = _reach(boxes, xs0 + one, xs1 - one, y, False, frame.bottom, settings.extent_gap_pt)
    drawing_below = down >= up
    reach = max(up, down)
    if drawing_below:
        top = y - settings.label_band_pt
        bottom = min(frame.bottom, y + reach + 4 * one)
        hatch_low, hatch_high = y, y + reach
    else:
        top = max(frame.top, y - reach - 4 * one)
        bottom = y + settings.label_band_pt
        hatch_low, hatch_high = y - reach, y
    if row.overall is not None:
        top = min(top, row.overall.y - settings.label_band_pt)
        bottom = max(bottom, row.overall.y + settings.label_band_pt)
    width = xs1 - xs0
    context = settings.row_context_fraction * width + settings.row_context_pt
    row_box = Box(
        max(frame.x0, xs0 - context),
        max(frame.top, top - settings.row_above_pt),
        min(frame.x1, xs1 + context),
        min(frame.bottom, bottom + settings.row_below_pt),
    )
    left = hatch_at(
        ink, x_end=xs0, side="left", y_low=hatch_low, y_high=hatch_high, settings=settings
    )
    right = hatch_at(
        ink, x_end=xs1, side="right", y_low=hatch_low, y_high=hatch_high, settings=settings
    )
    seen: dict[str, tuple[bool, str]] = {}
    ends: tuple[tuple[Literal["left", "right"], Decimal, tuple[bool, str]], ...] = (
        ("left", xs0, left),
        ("right", xs1, right),
    )
    for side, x_end, hatch in ends:
        if hatch[0]:
            seen[side] = (True, f"hatch: {hatch[1]}")
            continue
        band = band_at(
            ink, x_end=x_end, side=side, y_low=hatch_low, y_high=hatch_high, settings=settings
        )
        if band[0]:
            seen[side] = band
            continue
        seen[side] = tall_line_at(
            ink,
            x_end=x_end,
            side=side,
            row_width=width,
            y_low=hatch_low,
            y_high=hatch_high,
            frame_height=frame.height,
            settings=settings,
        )
    return WallPictures(
        row=row_box,
        view=frame,
        hatch=HatchSeen(left[0], right[0], left[1], right[1]),
        ink=WallInkSeen(seen["left"][0], seen["right"][0], seen["left"][1], seen["right"][1]),
    )
