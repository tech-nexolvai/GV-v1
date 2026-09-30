"""Whether a crop cut a label off, and which way the label runs — from the file's own paths (#757).

Two of the three failures that dominate the measured key are geometric: a label cut off at the
crop's edge (#641: two readers agreed on the last two digits of a three-digit label) and a label
that reads sideways (0 of 10 read). Neither needs a model to see. On the client's drawings one path
object is one character (#756), so the characters of a label are in the file, where it is, and a
crop's edge either cuts them or does not.

**A label is gathered, not boxed.** Its characters are the page's glyph paths that lie in the
region, grown by `extraction/glyph_reader.gather_label` — the rule the shape reader gathers a label
by — to every glyph within the stated gap, repeatedly. A crop cut round the region's own box is
exactly what cut the label on #641; the label's whole run is what the widening refinement crops to.

**Touching the crop's edge counts as cut.** A path's points are the centre line of the stroke, so
ink runs half a line width past them: a label whose points meet the edge has lost that much of it.

**Which way it runs, from where the characters sit.** A label runs up the page when its characters
form runs advancing up the page (`extraction/annotations.glyph_runs` turned 90°) and none advancing
across it. Which of the two vertical directions it reads in, the boxes cannot say: a character's
box is the same either way up. The drafting convention decides — aligned dimensions read from the
bottom or the right of the sheet, so a vertical label reads up the page (90°). Where a drawing
breaks the convention the turned crop shows the label upside down, and what a reader makes of that
is one more reading: proposed, never sealed on its own, and set against every other reading of the
region by the decision table. Anything else, including a label with runs both ways, is read as it
stands, which is what every reader did before #757.

**Nothing here has a default.** Both gathering lengths and the run gap are the deployment's to
state, and all of them are in `LabelReach.config_hash`.

Source: issue #757 · Verification: `tests/extraction/agent/test_geometry.py`
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from extraction.annotations import VectorPath, glyph_runs
from extraction.glyph_reader import gather_label

__all__ = ["Box", "LabelGeometry", "LabelReach", "label_geometry"]

Box = tuple[Decimal, Decimal, Decimal, Decimal]
"""left, bottom, right, top, in PDF points."""

#: A vertical label reads up the page, by the drafting convention (see the module docstring).
CONVENTIONAL_VERTICAL_DEGREES = 90


@dataclass(frozen=True, slots=True)
class LabelReach:
    """How a label is gathered and how its characters run, in PDF points. None has a default."""

    label_gap_pt: Decimal
    """How close a character must be to join the label (`gather_label`)."""

    maximum_label_pt: Decimal
    """How far one label may extend before where it ends counts as unsettled (`gather_label`)."""

    glyph_gap_pt: Decimal
    """The largest gap between consecutive characters of one run (`glyph_runs`) — the association
    settings' own, so a label's direction is judged by the rule its regions were formed by."""

    def __post_init__(self) -> None:
        for name in ("label_gap_pt", "maximum_label_pt", "glyph_gap_pt"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be a Decimal, never a float")
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a finite length greater than zero")

    @property
    def config_hash(self) -> str:
        """Part of a run's identity: a label judged under other lengths is judged differently."""
        return (
            f"label_gap_pt={self.label_gap_pt};maximum_label_pt={self.maximum_label_pt};"
            f"glyph_gap_pt={self.glyph_gap_pt}"
        )


@dataclass(frozen=True, slots=True)
class LabelGeometry:
    """What the file's paths say about the label in one region."""

    label_box: Box | None
    """The whole label's extent. `None` where no glyph path lies in the region — a label drawn as
    font text rather than paths (#738), which this cannot see and says nothing about."""

    closed: bool
    """Whether the label's end is settled. An unclosed label runs into more text than one label
    holds, or has a character on its own line just past its end, within the run gap
    (`gather_label`): either way where it ends is not settled, so it cannot be widened to."""

    cut_at_edge: bool
    rotation_degrees: int
    """0 as it stands; 90 reading up the page. Never 270 from geometry alone (see the docstring)."""


def _box(path: VectorPath) -> Box:
    xs = [x for x, _ in path.points]
    ys = [y for _, y in path.points]
    return (min(xs), min(ys), max(xs), max(ys))


def _overlaps(first: Box, second: Box) -> bool:
    return (
        first[0] <= second[2]
        and second[0] <= first[2]
        and first[1] <= second[3]
        and second[1] <= first[3]
    )


def _strictly_inside(inner: Box, outer: Box) -> bool:
    return (
        outer[0] < inner[0] and outer[1] < inner[1] and inner[2] < outer[2] and inner[3] < outer[3]
    )


def _runs_up_the_page(boxes: list[Box], glyph_gap_pt: Decimal) -> bool:
    across, _ = glyph_runs(boxes, glyph_gap_pt, 0)
    up, _ = glyph_runs(boxes, glyph_gap_pt, 90)
    return not across and bool(up)


def label_geometry(
    region: Box, crop: Box, page_glyphs: Sequence[VectorPath], reach: LabelReach
) -> LabelGeometry:
    """The label in `region`: its whole extent, whether `crop` cuts it, and which way it runs.

    `region` is the box a reader's reading was placed at; `crop` is the page area a crop of it
    shows. Both in PDF points, the space the glyph paths are in.
    """
    # **Only characters that could join, and exactly those.** While a label is still growing its
    # box is at most `maximum_label_pt` across and holds a seed, which touches the region; a joining
    # character is within `label_gap_pt` of that box. Nothing farther can join, so leaving it out
    # changes no result — it only keeps a busy sheet from being scanned whole for every region.
    reach_pt = reach.maximum_label_pt + reach.label_gap_pt
    around = (
        region[0] - reach_pt,
        region[1] - reach_pt,
        region[2] + reach_pt,
        region[3] + reach_pt,
    )
    nearby = [path for path in page_glyphs if path.points and _overlaps(_box(path), around)]
    seeds = [path for path in nearby if _overlaps(_box(path), region)]
    if not seeds:
        return LabelGeometry(label_box=None, closed=True, cut_at_edge=False, rotation_degrees=0)
    members, unclosed = gather_label(seeds, nearby, settings=reach)
    boxes = [_box(path) for path in members]
    label_box: Box = (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )
    return LabelGeometry(
        label_box=label_box,
        closed=unclosed is None,
        cut_at_edge=not _strictly_inside(label_box, crop),
        rotation_degrees=(
            CONVENTIONAL_VERTICAL_DEGREES if _runs_up_the_page(boxes, reach.glyph_gap_pt) else 0
        ),
    )
