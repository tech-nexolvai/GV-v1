"""Reading a vendor label from its character shapes, exactly, or refusing (#756 phase C).

`extraction/glyph_shapes.py` describes and compares one character. This module reads a whole
label: it gathers the label's characters from the page, decides which way the label reads,
matches every character against the templates a person confirmed (`scripts/glyph_inventory.py`),
composes them by where they sit — whole number, stacked fraction, inch ticks, a millimetre line and
its bracketed inches — and parses the result with the same parser every other reader's text goes
through. The value is exact because every step is: a shape either matched a confirmed template by
the stated margin or it did not.

**It refuses rather than guesses, and it never returns part of a label.** One unmatched character,
one piece of line-work inside the label, a layout that is not a dimension's, a reading direction it
cannot settle, or a string the parser will not value — any of them, and the whole label abstains
with a sentence saying which. A reviewer then reads it, exactly as they do today.

**Which way a label reads is settled by the characters, with the drafting convention first.**
Aligned dimension text reads from the bottom or the right of the sheet, so a label that runs up the
page is tried turned clockwise before it is tried turned the other way. The other way is accepted
only where the conventional direction leaves a character unmatched and it matches every one; where
both match fully the label abstains, because a turned `6` is a `9` (#756 §What the measurement
changes, the font test's only miss).

**Nothing here has a default.** The match distance and margin, the size ratio, how close a
character must be to join a label and how large a label may grow are all the deployment's to
state, and all of them are in `ReaderSettings.config_hash`.

Source: issue #756 · Verification: `tests/extraction/test_glyph_reader.py`
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Final

import cv2
import numpy as np
from numpy.typing import NDArray

from extraction.annotations import PathSegment, VectorPath, glyph_runs
from extraction.glyph_shapes import (
    GlyphShape,
    ShapeSettings,
    UndrawablePath,
    describe,
    size_ratio,
)
from units.measurement import Measurement
from units.normalise import UnitNormalisationError, normalise_to_inches
from units.notation import canonical_notation, is_compound

__all__ = [
    "GlyphAbstention",
    "GlyphReading",
    "ReaderSettings",
    "TemplateSet",
    "described",
    "gather_label",
    "read_label",
]

NOT_A_CHARACTER: Final = "not_a_character"
SIDEWAYS: Final = "sideways"

#: What a fraction bar is marked as. Not a character a person labelled — see `_bars`.
_BAR: Final = "fraction bar"

Box = tuple[Decimal, Decimal, Decimal, Decimal]
"""left, bottom, right, top, in PDF points."""


# ---------------------------------------------------------------------------
# Templates and settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TemplateSet:
    """The shapes a person confirmed, each with its label, from one hashed set."""

    set_hash: str
    shape_settings: str
    glyph_gap_pt: Decimal
    """The run gap the inventory sized these shapes with. A reader must group runs the same way."""

    labels: tuple[str, ...]
    shapes: tuple[GlyphShape, ...]

    @classmethod
    def load(cls, directory: Path) -> TemplateSet:
        """A set written by `glyph_inventory.py build`. Refused if its name is not its hash."""
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if directory.name != manifest["sha256"][:12]:
            raise ValueError(
                f"{directory.name} is not the start of its manifest's hash; the set has been renamed "
                "or edited, and a template set is identified by its hash"
            )
        data = np.load(directory / "templates.npz")
        shapes = tuple(
            GlyphShape(
                raster=np.asarray(raster, dtype=np.bool_),
                relative_height=Fraction(height),
                relative_width=Fraction(width),
                dot=bool(dot),
            )
            for raster, height, width, dot in zip(
                data["rasters"],
                data["relative_heights"],
                data["relative_widths"],
                data["dots"],
                strict=True,
            )
        )
        return cls(
            set_hash=str(manifest["sha256"]),
            shape_settings=str(manifest["shape_settings"]),
            glyph_gap_pt=Decimal(manifest["reader_settings"]["GV_READER_GLYPH_GAP_PT"]),
            labels=tuple(str(label) for label in data["labels"]),
            shapes=shapes,
        )


@dataclass(frozen=True, slots=True)
class ReaderSettings:
    """Everything a reading depends on besides the drawing and the templates."""

    shape: ShapeSettings
    maximum_distance: Decimal
    """The largest chamfer distance, in pixels, at which a character matches its nearest template."""

    minimum_margin: Decimal
    """How much nearer that template must be than the nearest template with a *different* label."""

    maximum_size_ratio: Decimal
    """How far a character's size, relative to its line, may be from the template's."""

    label_gap_pt: Decimal
    """How close, in PDF points, a character must be to a label's box to be part of the label."""

    maximum_label_pt: Decimal
    """How large a label may grow on either axis before it is text running into other text."""

    glyph_gap_pt: Decimal
    """The gap along a baseline that still joins two characters into one run. It must be the one
    the templates were sized with, because a character's size is measured against its run."""

    def __post_init__(self) -> None:
        for name in (
            "maximum_distance",
            "minimum_margin",
            "label_gap_pt",
            "maximum_label_pt",
            "glyph_gap_pt",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a finite, non-negative Decimal")
        if self.maximum_size_ratio < 1:
            raise ValueError("maximum_size_ratio must be at least 1")

    @property
    def config_hash(self) -> str:
        return (
            f"{self.shape.config_hash};distance<={self.maximum_distance};"
            f"margin>={self.minimum_margin};size<={self.maximum_size_ratio};"
            f"gap<={self.label_gap_pt};label<={self.maximum_label_pt};run_gap={self.glyph_gap_pt}"
        )


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GlyphReading:
    """A label read exactly: what it says, its value, which way it reads, and where it is."""

    text: str
    value: Measurement
    rotation_degrees: int
    """How far the label is turned on the page: 0 upright, 90 reading up the page, 270 down it."""

    box: Box
    template_set: str


@dataclass(frozen=True, slots=True)
class GlyphAbstention:
    """A label this reader would not read, and why, in words a reviewer can act on."""

    reason: str
    box: Box | None


# ---------------------------------------------------------------------------
# Gathering a label
# ---------------------------------------------------------------------------


def _box(path: VectorPath) -> Box:
    xs = [x for x, _ in path.points]
    ys = [y for _, y in path.points]
    return (min(xs), min(ys), max(xs), max(ys))


def _union(boxes: Sequence[Box]) -> Box:
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _near(box: Box, around: Box, gap: Decimal) -> bool:
    return (
        box[0] <= around[2] + gap
        and around[0] - gap <= box[2]
        and box[1] <= around[3] + gap
        and around[1] - gap <= box[3]
    )


def gather_label(
    seeds: Sequence[VectorPath], page_glyphs: Sequence[VectorPath], *, settings: ReaderSettings
) -> tuple[tuple[VectorPath, ...], str | None]:
    """The whole label the seeds belong to: every page glyph within `label_gap_pt` of it, repeatedly.

    A label is grown from the page rather than cut to a box, because a crop that cut a label is
    exactly the failure #641 recorded — two readers agreed on the last two digits of a three-digit
    label. Returns the paths and `None`, or the paths so far and why the label could not be closed.
    """
    if not seeds:
        return (), "there are no characters here to read"
    members = list(seeds)
    seen = {id(path) for path in members}
    others = [path for path in page_glyphs if id(path) not in seen]
    while True:
        around = _union([_box(path) for path in members])
        if (
            around[2] - around[0] > settings.maximum_label_pt
            or around[3] - around[1] > settings.maximum_label_pt
        ):
            return tuple(members), (
                "the characters here run into more text than one label holds, so where this label "
                "ends is not settled"
            )
        joining = [path for path in others if _near(_box(path), around, settings.label_gap_pt)]
        if not joining:
            return tuple(members), None
        members.extend(joining)
        joined = {id(path) for path in joining}
        others = [path for path in others if id(path) not in joined]


# ---------------------------------------------------------------------------
# Orientation
# ---------------------------------------------------------------------------

Turn = Callable[[tuple[Decimal, Decimal]], tuple[Decimal, Decimal]]

#: How to turn a label upright, by how it is turned on the page.
_UPRIGHT: Final[dict[int, Turn]] = {
    0: lambda point: point,
    # Reading up the page — the drafting convention for a vertical dimension. Turned clockwise.
    90: lambda point: (point[1], -point[0]),
    # Reading down the page. Turned anticlockwise.
    270: lambda point: (-point[1], point[0]),
}


def _turned(path: VectorPath, turn: Turn) -> VectorPath:
    return VectorPath(
        segments=tuple(
            PathSegment(kind=segment.kind, point=turn(segment.point), closes=segment.closes)
            for segment in path.segments
        ),
        stroked=path.stroked,
        filled=path.filled,
    )


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Glyph:
    path: VectorPath
    box: Box
    label: str | None
    """The matched template's label, or `None` where no template matched by the stated margin."""


@dataclass(frozen=True, slots=True)
class _TemplateIndex:
    """A template set laid out for comparing one shape with every template at once.

    Whole numbers throughout: each template's ink, its city-block distance map, and each relative
    size's exact numerator and denominator. A chamfer distance is then a ratio of whole-number sums,
    so every comparison can be made exactly — the vectorised path decides the same thing, template
    for template, as `chamfer` and `size_ratio` would one pair at a time.
    """

    labels: tuple[str, ...]
    masks: NDArray[np.int64]
    distances: NDArray[np.int64]
    ink: NDArray[np.int64]
    dots: NDArray[np.bool_]
    shapes: tuple[GlyphShape, ...]
    heights: NDArray[np.float64]
    widths: NDArray[np.float64]
    """Relative sizes as floats — for settling the clear cases of the size check only. A size
    within a hair of the limit is settled by `size_ratio`, exactly."""


_INDEXES: dict[int, tuple[TemplateSet, _TemplateIndex]] = {}


def _index(templates: TemplateSet) -> _TemplateIndex:
    """The set's index, built once per set object for the life of the process."""
    cached = _INDEXES.get(id(templates))
    if cached is not None and cached[0] is templates:
        return cached[1]
    # **One copy of each distinct template.** A cluster a person labelled holds hundreds of members
    # and many are the same raster at the same size — 2,291 identical vertical strokes on the
    # client's set. An exact duplicate gives exactly the same distance, so keeping one per label,
    # raster and size changes no decision and removes most of the work.
    distinct: dict[tuple[str, bytes, Fraction, Fraction, bool], int] = {}
    for position, (label, shape) in enumerate(zip(templates.labels, templates.shapes, strict=True)):
        key = (
            label,
            shape.raster.tobytes(),
            shape.relative_height,
            shape.relative_width,
            shape.dot,
        )
        distinct.setdefault(key, position)
    kept = sorted(distinct.values())
    shapes = tuple(templates.shapes[position] for position in kept)
    rasters = np.stack([shape.raster for shape in shapes]).astype(np.int64)
    distances = np.stack(
        [
            cv2.distanceTransform(
                np.where(shape.raster, 0, 255).astype(np.uint8), cv2.DIST_L1, cv2.DIST_MASK_3
            ).astype(np.int64)
            for shape in shapes
        ]
    )
    index = _TemplateIndex(
        labels=tuple(templates.labels[position] for position in kept),
        masks=rasters,
        distances=distances,
        ink=rasters.sum(axis=(1, 2)),
        dots=np.array([shape.dot for shape in shapes], dtype=np.bool_),
        shapes=shapes,
        heights=np.array([float(shape.relative_height) for shape in shapes]),
        widths=np.array([float(shape.relative_width) for shape in shapes]),
    )
    _INDEXES[id(templates)] = (templates, index)
    return index


def _sized(shape: GlyphShape, index: _TemplateIndex, limit: Fraction) -> list[int]:
    """The templates whose size is within `limit` of the shape's — `size_ratio(shape, t) <= limit`.

    A zero size is decided exactly and all at once: a relative size is a ratio of PDF-point
    differences, so its float is zero exactly when it is, and `size_ratio` puts a zero beside a
    non-zero out of reach while two zeros match. Every other template is settled by floats when it
    is clearly in or clearly out; one within a millionth of the limit is settled by `size_ratio`
    itself, so the answer is exactly the exact rule's.
    """
    candidates = np.flatnonzero(~index.dots)
    height, width = float(shape.relative_height), float(shape.relative_width)
    heights, widths = index.heights[candidates], index.widths[candidates]
    mismatched = ((heights == 0) != (height == 0)) | ((widths == 0) != (width == 0))
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio_h = np.where(
            (heights != 0) & (height != 0),
            np.maximum(heights, height) / np.minimum(heights, height),
            1.0,
        )
        ratio_w = np.where(
            (widths != 0) & (width != 0),
            np.maximum(widths, width) / np.minimum(widths, width),
            1.0,
        )
    ratio = np.maximum(ratio_h, ratio_w)
    bound = float(limit)
    clearly_in = ~mismatched & (ratio < bound * (1 - 1e-6))
    unsure = ~mismatched & ~clearly_in & (ratio <= bound * (1 + 1e-6))
    kept = [int(i) for i in candidates[clearly_in]]
    kept += [int(i) for i in candidates[unsure] if size_ratio(shape, index.shapes[int(i)]) <= limit]
    return sorted(kept)


def _match(shape: GlyphShape, templates: TemplateSet, settings: ReaderSettings) -> str | None:
    """The label of the nearest template, if it is near enough and clearly nearer than any other.

    A dot matches only dots. A shape of any other kind is compared with every template of a
    compatible size; the nearest must be within `maximum_distance`, and the nearest template with a
    different label must be at least `minimum_margin` further away.

    **Every template at once, and still exact.** Template *i*'s chamfer distance to the shape is
    `(A_i / g + B_i / t_i) / 2`: `A_i` the template's distance summed over the shape's ink, `g` the
    shape's ink, `B_i` the shape's distance summed over the template's ink, `t_i` the template's ink.
    All four are whole numbers, so the distance is the exact fraction `(A_i t_i + B_i g) / 2 g t_i`
    and each label's nearest template is found by comparing those fractions, never floats.
    """
    index = _index(templates)
    if shape.dot:
        dots = {index.labels[i] for i in np.flatnonzero(index.dots)}
        best: dict[str, Fraction] = {label: Fraction(0) for label in dots}
    else:
        sized = _sized(shape, index, Fraction(settings.maximum_size_ratio))
        best = {}
        if sized:
            chosen = np.array(sized)
            glyph = shape.raster.astype(np.int64)
            glyph_ink = int(glyph.sum())
            if glyph_ink:
                glyph_distances = cv2.distanceTransform(
                    np.where(shape.raster, 0, 255).astype(np.uint8), cv2.DIST_L1, cv2.DIST_MASK_3
                ).astype(np.int64)
                to_template = (index.distances[chosen] * glyph).sum(axis=(1, 2))
                to_glyph = (index.masks[chosen] * glyph_distances).sum(axis=(1, 2))
                ink = index.ink[chosen]
                numerators = to_template * ink + to_glyph * glyph_ink
                denominators = 2 * glyph_ink * ink
                for position, template in enumerate(chosen):
                    if not ink[position]:
                        continue
                    distance = Fraction(int(numerators[position]), int(denominators[position]))
                    label = index.labels[template]
                    if label not in best or distance < best[label]:
                        best[label] = distance
    if not best:
        return None
    ranked = sorted(best.items(), key=lambda entry: (entry[1], entry[0]))
    label, distance = ranked[0]
    if distance > Fraction(settings.maximum_distance):
        return None
    if len(ranked) > 1 and ranked[1][1] - distance < Fraction(settings.minimum_margin):
        return None
    return label


def _lines(glyphs: Sequence[_Glyph]) -> list[list[_Glyph]]:
    """Group characters that share a baseline band, top line first, each read left to right."""
    lines: list[list[_Glyph]] = []
    for glyph in sorted(glyphs, key=lambda item: -item.box[3]):
        for line in lines:
            band = (min(g.box[1] for g in line), max(g.box[3] for g in line))
            if glyph.box[1] <= band[1] and band[0] <= glyph.box[3]:
                line.append(glyph)
                break
        else:
            lines.append([glyph])
    for line in lines:
        line.sort(key=lambda item: item.box[0])
    return lines


def _bars(boxes: Sequence[Box]) -> set[int]:
    """Which of the label's paths are fraction bars: flat, with a character wholly above and one
    wholly below it, both over its length.

    **Found by where it is, not by what it looks like.** A bar is the one character a person never
    labels: the inventory's regions come from runs along the baseline, and a bar — with the
    denominator under it — is left out of every run (`OutlinedTextRegion.stacked_glyphs`). Its
    shape is also every flat stroke on the sheet. What identifies it is the arrangement, which is
    how `extraction/glyph_bands.py` finds the same bars for the vision route (#735). The characters
    above and below are matched like any other, so a flat stroke between two pieces of line-work
    makes a label that abstains, not a fraction.
    """
    bars: set[int] = set()
    for index, (left, bottom, right, top) in enumerate(boxes):
        if top != bottom:
            continue
        over = [
            other
            for position, other in enumerate(boxes)
            if position != index and other[0] < right and left < other[2]
        ]
        if any(other[1] >= top for other in over) and any(other[3] <= bottom for other in over):
            bars.add(index)
    return bars


def described(
    paths: Sequence[VectorPath], *, settings: ReaderSettings
) -> tuple[list[Box], set[int], list[GlyphShape | None]]:
    """A label's characters as they are compared: boxes, which are fraction bars, and each shape.

    Sized exactly as the inventory sizes the shapes it asks a person to label: against the run a
    character sits in — the grouping `glyph_runs` makes, with the same gap — and a character in no
    run against itself. A bar has no shape here (`None`), and neither has a character that cannot
    be drawn or has no height to be sized against.
    """
    boxes = [_box(path) for path in paths]
    bars = _bars(boxes)
    others = [index for index in range(len(paths)) if index not in bars]
    runs, _ = glyph_runs([boxes[index] for index in others], settings.glyph_gap_pt, 0)
    height: dict[int, Decimal] = {}
    for run in runs:
        members = [others[position] for position in run]
        span = max(boxes[i][3] for i in members) - min(boxes[i][1] for i in members)
        for index in members:
            height[index] = span
    shapes: list[GlyphShape | None] = []
    for index, path in enumerate(paths):
        run_height = height.get(index, boxes[index][3] - boxes[index][1])
        shape: GlyphShape | None = None
        if index not in bars and run_height > 0:
            try:
                shape = describe(path, run_height=run_height, settings=settings.shape)
            except UndrawablePath:
                shape = None
        shapes.append(shape)
    return boxes, bars, shapes


def _matched(
    paths: Sequence[VectorPath], templates: TemplateSet, settings: ReaderSettings
) -> list[_Glyph]:
    """Every character matched against the templates; bars marked; the rest `None` if unmatched."""
    boxes, bars, shapes = described(paths, settings=settings)
    return [
        _Glyph(
            path=path,
            box=box,
            label=(
                _BAR
                if index in bars
                else None if shape is None else _match(shape, templates, settings)
            ),
        )
        for index, (path, box, shape) in enumerate(zip(paths, boxes, shapes, strict=True))
    ]


# ---------------------------------------------------------------------------
# Composing
# ---------------------------------------------------------------------------


def _text(line: Sequence[_Glyph]) -> str:
    """One line's characters, left to right, with a pair of inch ticks written as one mark."""
    text = "".join(glyph.label or "" for glyph in line)
    return text.replace("''", '"')


def _compose(glyphs: Sequence[_Glyph]) -> tuple[str | None, str | None]:
    """The label's text from where its characters sit, or why its layout is not a dimension's."""
    bars = [glyph for glyph in glyphs if glyph.label == _BAR]
    if len(bars) > 1:
        return None, "the label holds more than one fraction bar"
    if bars:
        bar = bars[0]
        rest = [glyph for glyph in glyphs if glyph is not bar]
        over = [g for g in rest if g.box[0] < bar.box[2] and bar.box[0] < g.box[2]]
        numerator = [g for g in over if g.box[1] >= bar.box[3]]
        denominator = [g for g in over if g.box[3] <= bar.box[1]]
        whole = [g for g in rest if g.box[2] <= bar.box[0]]
        after = [g for g in rest if g.box[0] >= bar.box[2]]
        if len(numerator) + len(denominator) + len(whole) + len(after) != len(rest):
            return None, "a character sits across the fraction bar"
        if not numerator or not denominator:
            return None, "the fraction has nothing above or nothing below its bar"
        if len(_lines(numerator)) != 1 or len(_lines(denominator)) != 1:
            return None, "the fraction's numerator or denominator is not one row"
        whole_text = "".join(_text(line) for line in _lines(whole)) if whole else ""
        after_text = "".join(_text(line) for line in _lines(after)) if after else ""
        fraction = f"{_text(_lines(numerator)[0])}/{_text(_lines(denominator)[0])}"
        return (f"{whole_text} {fraction}" if whole_text else fraction) + after_text, None

    lines = _lines(glyphs)
    if len(lines) == 1:
        return _text(lines[0]), None
    if len(lines) == 2:
        top, bottom = (_text(line) for line in lines)
        # A millimetre line and its inches in brackets, either way up (#733 reads `381 [15]`).
        if bottom.startswith("[") and bottom.endswith("]") and "[" not in top:
            return f"{top} {bottom}", None
        if top.startswith("[") and top.endswith("]") and "[" not in bottom:
            return f"{bottom} {top}", None
        return None, "the label has two lines, and neither is a bracketed inch line"
    return (
        None,
        f"the characters lie on {len(lines)} lines, which is not how a dimension is written",
    )


def _value(text: str) -> Measurement | None:
    """The exact value of a composed label, by the rule every reader's text is valued by."""
    if is_compound(text):
        return None
    try:
        return normalise_to_inches(canonical_notation(text)[0])
    except UnitNormalisationError:
        return None


def _attempt(
    paths: Sequence[VectorPath], rotation: int, templates: TemplateSet, settings: ReaderSettings
) -> tuple[str | None, str]:
    """Read the label turned upright from `rotation`: the text, or `None` and why not."""
    upright = [_turned(path, _UPRIGHT[rotation]) for path in paths]
    glyphs = _matched(upright, templates, settings)
    unmatched = sum(glyph.label is None for glyph in glyphs)
    if unmatched:
        return None, (
            f"{unmatched} of the label's {len(glyphs)} characters "
            f"{'has' if unmatched == 1 else 'have'} not been labelled yet, or look like more than "
            "one labelled shape"
        )
    if any(glyph.label == SIDEWAYS for glyph in glyphs):
        return None, "the characters are turned on their side in this direction"
    if any(glyph.label == NOT_A_CHARACTER for glyph in glyphs):
        return None, "a piece of the drawing sits inside the label"
    text, layout = _compose(glyphs)
    if text is None:
        return None, layout or "the characters could not be put in order"
    return text, ""


def read_label(
    seeds: Sequence[VectorPath],
    page_glyphs: Sequence[VectorPath],
    *,
    templates: TemplateSet,
    settings: ReaderSettings,
    within: Box | None = None,
) -> GlyphReading | GlyphAbstention:
    """Read the label the seed characters belong to, exactly, or abstain and say why.

    `within` is the area a caller asked about — a crop somebody read by eye. Where the whole label
    runs past its edge the label abstains: the crop showed part of it, and a reading of the whole
    would be scored against what the crop showed. Without `within`, the label is simply read whole.
    """
    if templates.glyph_gap_pt != settings.glyph_gap_pt:
        raise ValueError(
            f"the template set's shapes were sized with a run gap of {templates.glyph_gap_pt} pt; "
            "sized with another, they would not match themselves"
        )
    if templates.shape_settings != settings.shape.config_hash:
        raise ValueError(
            "the template set was drawn with other shape settings "
            f"({templates.shape_settings}), so its shapes are not comparable with these"
        )
    paths, unclosed = gather_label(seeds, page_glyphs, settings=settings)
    box = _union([_box(path) for path in paths]) if paths else None
    if unclosed is not None:
        return GlyphAbstention(reason=unclosed, box=box)
    assert box is not None
    if within is not None and not (
        within[0] <= box[0] and within[1] <= box[1] and box[2] <= within[2] and box[3] <= within[3]
    ):
        return GlyphAbstention(
            reason="the label runs past the edge of the area asked about, which shows only part of it",
            box=box,
        )

    upright, why = _attempt(paths, 0, templates, settings)
    if upright is not None:
        value = _value(upright)
        if value is not None:
            return GlyphReading(upright, value, 0, box, templates.set_hash)
        why = f"it reads as {upright!r}, which is not one dimension"

    # The drafting convention first; the other way only where the convention fails and it does not.
    conventional, conventional_why = _attempt(paths, 90, templates, settings)
    opposite, _ = _attempt(paths, 270, templates, settings)
    conventional_value = None if conventional is None else _value(conventional)
    opposite_value = None if opposite is None else _value(opposite)
    if conventional_value is not None and opposite_value is not None:
        return GlyphAbstention(
            reason=(
                "the label reads validly both ways up — as "
                f"{conventional!r} and as {opposite!r} — so which way it reads is not settled"
            ),
            box=box,
        )
    if conventional_value is not None and conventional is not None:
        return GlyphReading(conventional, conventional_value, 90, box, templates.set_hash)
    if opposite_value is not None and opposite is not None:
        return GlyphReading(opposite, opposite_value, 270, box, templates.set_hash)
    return GlyphAbstention(reason=why or conventional_why, box=box)
