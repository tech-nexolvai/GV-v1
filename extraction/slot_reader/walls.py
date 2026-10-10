"""Which ends of a countertop row stand against a wall: two readers agree, code may object (#992).

**The question, and why two readers.** CT-WIDTH-001 adds a field cut per wall end, so its variant
needs the wall layout (`wall_config`). E3 (2026-10-07) showed both readers the row with its ends
marked and the whole vendor view, and asked one narrow question per end: 9 rows agreed-right,
0 agreed-wrong, 4 to the person. The admin approved it as a seal (2026-10-07): a side counts only
when both readers, of different makers, say the same "yes" or the same "no".

**The layout from the two ends** (admin's rule):
- both ends "yes" → `back_left_right`;
- left "yes" and right agreed "no" → `back_and_left`; the reverse → `back_and_right` (#1138: the
  field cut is 1 inch per wall end, so one wall end is one field cut; the side is kept so the
  picture puts the cut at the wall). An open end is never inferred from absence: only the two
  readers' agreed "no" says an end is open;
- both ends "no" → `back_only` **only** when both readers call the view a plan and both see the
  back wall — an elevation does not show what is behind the run, so the back wall is not inferred;
- anything else — an end unsure, a reader missing, readers that differ — → the person.

A layout the readers sealed is still only a proposal: the row's reviewer confirms it before a check
uses it (`workflow/slot_row_scope.effective_row_wall`). Only code's own clues at both ends settle a
row without a click, and code never sees an open end, so a one-end layout always needs the click.

**Code may object, never approve.** E3's hatch check looks for a wall's hatching beside each row
end: many parallel strokes, or a cross-hatch, beside the end and along the drawing's height. Where
it sees one and a reader says "no wall" there, the row goes to the person. It cannot see a dotted
or plain-line wall, so seeing nothing proves nothing, and it is never a reason to seal.

**Pure.** No I/O and no float: the geometry is `Decimal`, the hatch's angles are binned against
written-down tangents instead of computed with trigonometry. The pictures are cut by the caller from
the boxes this module gives.

Source: issue #992 · Verification: `tests/extraction/slot_reader/test_walls.py`
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, fields
from decimal import Decimal
from enum import StrEnum
from typing import Final, Literal

from evidence.corroborate import UNKNOWN_MODEL_VENDOR, independence_key
from extraction.geometry.rows import Box, CountertopRowCandidate, PageInk

__all__ = [
    "BACK_AND_LEFT",
    "BACK_AND_RIGHT",
    "BACK_LEFT_RIGHT",
    "BACK_ONLY",
    "E3_WALL_SETTINGS",
    "WALL_PROMPT",
    "WALL_PROMPT_ID",
    "WALL_PROMPT_IDS",
    "CodeWallClues",
    "HatchSeen",
    "Side",
    "WallAnswer",
    "WallOutcome",
    "WallPictures",
    "WallSettings",
    "code_wall_outcome",
    "hatch_at",
    "seal_walls",
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
    """`vendor-drawing-clues`, `drawing-and-readers`, or `readers`; never human confirmation."""


@dataclass(frozen=True, slots=True)
class CodeWallClues:
    """Positive vendor-drawing evidence at either end; absence is unknown, never an open wall."""

    left: bool | None = None
    right: bool | None = None

    @property
    def has_evidence(self) -> bool:
        return self.left is True or self.right is True


_NO_CODE_WALL_CLUES: Final = CodeWallClues()


def code_wall_outcome(clues: CodeWallClues, *, row_ambiguity: str | None) -> WallOutcome | None:
    """Code may settle both affirmative wall ends; it never infers an open end from absence."""
    if row_ambiguity is not None or clues.left is not True or clues.right is not True:
        return None
    return WallOutcome(
        BACK_LEFT_RIGHT,
        None,
        None,
        Side.YES,
        Side.YES,
        None,
        source="vendor-drawing-clues",
    )


def _maker(model_id: str) -> str:
    return independence_key("bedrock-slot-reader", model_id)


_CLAUDE_PAIR: Final = frozenset({"anthropic.claude-opus-5-5", "anthropic.claude-sonnet-5-5"})


def _agreed(first: Side, second: Side) -> Side | None:
    return first if first == second and first is not Side.UNSURE else None


def seal_walls(
    answers: Sequence[WallAnswer],
    *,
    hatch: HatchSeen | None,
    row_ambiguity: str | None,
    code_clues: CodeWallClues = _NO_CODE_WALL_CLUES,
    allow_claude_pair: bool = False,
) -> WallOutcome:
    """The row's wall layout when two makers agree and nothing objects; otherwise why not.

    `answers` holds the readers that answered (an abstention is absent). `hatch` is code's hatch
    check at the two ends, `None` when it could not run (which objects to nothing).
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

    if row_ambiguity is None:
        code_outcome = code_wall_outcome(code_clues, row_ambiguity=None)
        if code_outcome is not None:
            return code_outcome

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
    if hatch is not None:
        for side, seen in (("left", hatch.left), ("right", hatch.right)):
            if seen and any(getattr(answer, side) is Side.NO for answer in answers):
                return held(
                    "hatch-contradiction",
                    f"code sees a wall hatch at the {side} end, but a reader says no wall there",
                )
    if code_clues.left is True and any(answer.left is Side.NO for answer in answers):
        return held("code-reader-conflict", "vendor drawing clues show a wall at the left end")
    if code_clues.right is True and any(answer.right is Side.NO for answer in answers):
        return held("code-reader-conflict", "vendor drawing clues show a wall at the right end")
    resolved_left = Side.YES if code_clues.left is True else left
    resolved_right = Side.YES if code_clues.right is True else right
    source = "drawing-and-readers" if code_clues.has_evidence else "readers"
    if resolved_left is Side.YES and resolved_right is Side.YES:
        return WallOutcome(
            BACK_LEFT_RIGHT, None, None, resolved_left, resolved_right, behind, source
        )
    if resolved_left is Side.YES and right is Side.NO:
        return WallOutcome(BACK_AND_LEFT, None, None, resolved_left, resolved_right, behind, source)
    if left is Side.NO and resolved_right is Side.YES:
        return WallOutcome(
            BACK_AND_RIGHT, None, None, resolved_left, resolved_right, behind, source
        )
    if left is Side.NO and right is Side.NO:
        if behind is Side.YES and all(answer.view == "plan" for answer in answers):
            return WallOutcome(BACK_ONLY, None, None, left, right, behind, "readers")
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


#: E3's values (2026-10-07, `research/E3/build.py`), measured on both client sets.
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


def _strokes(ink: PageInk, settings: WallSettings) -> list[_Stroke]:
    segments: list[tuple[tuple[Decimal, Decimal], tuple[Decimal, Decimal]]] = [
        ((line.x0, line.y0), (line.x1, line.y1)) for line in ink.lines
    ]
    for curve in ink.curves:
        segments.extend(zip(curve.points, curve.points[1:], strict=False))
    low = settings.hatch_stroke_min_pt * settings.hatch_stroke_min_pt
    high = settings.hatch_stroke_max_pt * settings.hatch_stroke_max_pt
    out: list[_Stroke] = []
    for (ax, ay), (bx, by) in segments:
        dx, dy = bx - ax, by - ay
        if abs(dx) < settings.hatch_axis_min_pt or abs(dy) < settings.hatch_axis_min_pt:
            continue
        if not low <= dx * dx + dy * dy <= high:
            continue
        out.append(_Stroke((ax + bx) / 2, (ay + by) / 2, _bin(dx, dy)))
    return out


def hatch_at(
    ink: PageInk,
    *,
    x_end: Decimal,
    side: Literal["left", "right"],
    y_low: Decimal,
    y_high: Decimal,
    settings: WallSettings,
) -> tuple[bool, str]:
    """Whether a wall's hatching stands beside the row end at `x_end`, and in plain words why."""
    if side == "left":
        x_low, x_high = x_end - settings.hatch_reach_pt, x_end + settings.hatch_inside_pt
    else:
        x_low, x_high = x_end - settings.hatch_inside_pt, x_end + settings.hatch_reach_pt
    bins: dict[int, list[_Stroke]] = {}
    for stroke in _strokes(ink, settings):
        if x_low <= stroke.x <= x_high and y_low <= stroke.y <= y_high:
            bins.setdefault(stroke.bin, []).append(stroke)
    if not bins:
        return False, "no slanted strokes beside the end"
    ranked = sorted(bins.items(), key=lambda item: (-len(item[1]), item[0]))

    def span(strokes: Sequence[_Stroke]) -> Decimal:
        return max(stroke.y for stroke in strokes) - min(stroke.y for stroke in strokes)

    first_bin, first = ranked[0]
    if len(first) >= settings.hatch_parallel_count and span(first) >= settings.hatch_span_pt:
        return True, f"{len(first)} parallel strokes at {first_bin} degrees"
    if len(ranked) > 1:
        second_bin, second = ranked[1]
        apart = abs(first_bin - second_bin)
        if (
            len(first) >= settings.hatch_cross_count
            and len(second) >= settings.hatch_cross_count
            and abs(apart - 90) <= settings.hatch_cross_slack_degrees
            and span([*first, *second]) >= settings.hatch_span_pt
        ):
            return True, f"a cross-hatch of {len(first)} and {len(second)} strokes"
    return False, f"at most {len(first)} parallel strokes"


@dataclass(frozen=True, slots=True)
class WallPictures:
    """Where the two pictures are cut, in page points (pdfplumber's frame), and the hatch's band."""

    row: Box
    view: Box
    hatch: HatchSeen


def _ink_boxes(ink: PageInk) -> list[Box]:
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
    code's hatch check at both ends — all from the drawing's own ink, never a model's choice."""
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
    return WallPictures(
        row=row_box,
        view=frame,
        hatch=HatchSeen(left[0], right[0], left[1], right[1]),
    )
