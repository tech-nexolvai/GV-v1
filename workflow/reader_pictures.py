"""What each vision reader is shown: the crop as cut, or the label upright and sharper (#907).

**Measured per reader, not chosen.** On the 51-crop human-read key Nova 2 Lite read 9 of 35 labels
right from the crop as the stage cuts it and 23 when the label was turned upright and shown larger;
Qwen3-VL read best from the crop as cut, and turning and enlarging slightly hurt it (Reading upgrade
v2, §3b). So the picture is a property of the reader (`extraction.models.nova.ReaderPicture`), and
this module makes the one other picture there is.

**Sharper is rendered, never upscaled.** These sheets are vector, so a sharper picture is the same
page area drawn again from the page at a higher dpi (`extraction.rasterise.render_region`): real
detail, where an upscaled crop only interpolates the pixels it had. The dpi is the deployment's to
state, `GV_VISION_SHARPER_PICTURE_DPI`, and has no default.

**Upright from the drawing's own facts, never from a key.** Which way a label runs is read from the
file: the direction its own text is printed in, where the label is text, or the direction its glyph
paths run, by the reading agent's rule (`extraction.agent.geometry.label_geometry`, #783), where the
label is drawn. The trial turned crops by a person's tick on the answer key; production has no key.

**The same page area.** The sharper picture covers the crop the stage cut, scaled out to whole
pixels, so whatever decides what a crop shows — a stacked fraction, a GV mark — decides it for this
picture too, from the crop's own box.

Source: issue #907 · Verification: `tests/workflow/test_reader_pictures.py`
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from extraction.models.nova import ReaderPicture
from extraction.reader import TextItem
from extraction.vector_first import upright_png

__all__ = [
    "SHARPER_PICTURE_DPI_ENV",
    "PictureSettings",
    "PrintedRun",
    "label_turn",
    "picture_settings_from_environment",
    "printed_runs",
    "reader_picture",
    "sharper_box",
]

#: The resolution a sharper picture is rendered at, stated by the deployment. No default.
SHARPER_PICTURE_DPI_ENV: Final = "GV_VISION_SHARPER_PICTURE_DPI"

#: The two quarter turns a label is turned back from (`upright_png`).
_TURNS: Final = frozenset({90, 270})

Box = tuple[int, int, int, int]
"""left, top, right, bottom, in page pixels."""


@dataclass(frozen=True, slots=True)
class PictureSettings:
    """How a sharper picture is made: every setting stated, none defaulted."""

    sharper_dpi: int
    """The dpi the page area is rendered at. Above the stage's own, which the stage checks."""

    def __post_init__(self) -> None:
        if (
            isinstance(self.sharper_dpi, bool)
            or not isinstance(self.sharper_dpi, int)
            or self.sharper_dpi <= 0
        ):
            raise ValueError("sharper_dpi must be a whole number of dots per inch above zero")

    @property
    def config_text(self) -> str:
        """Part of a reading run's identity: a picture made under another dpi is another run."""
        return f"sharper_picture_dpi={self.sharper_dpi}"


def picture_settings_from_environment(
    environ: Mapping[str, str] = os.environ,
) -> PictureSettings | None:
    """The deployment's stated sharper dpi, or `None` where it states none.

    `None` is not a default: a stage with a reader that asks for a sharper picture refuses to start
    without it (`workflow.stages.DatabaseStages`). A stated value that is not a whole number is
    refused, never read as something near it.
    """
    raw = environ.get(SHARPER_PICTURE_DPI_ENV, "").strip()
    if not raw:
        return None
    if not raw.isascii() or not raw.isdigit():
        raise ValueError(f"{SHARPER_PICTURE_DPI_ENV} must be a whole number of dpi, not {raw!r}")
    return PictureSettings(sharper_dpi=int(raw))


@dataclass(frozen=True, slots=True)
class PrintedRun:
    """One run of the file's own text that holds a numeral, and the direction it is printed in."""

    box: Box
    """Its page pixels at the stage's dpi."""

    rotation_degrees: int
    """As `extraction.reader.TextItem` reads it from the characters' own matrix: 90 runs up the
    page, 270 down, 0 across."""


def printed_runs(texts: Iterable[TextItem]) -> tuple[PrintedRun, ...]:
    """The runs of text the file itself prints that hold a numeral, with their directions.

    The page's own text and the font text inside its pasted drawings alike: each is the file's
    characters, set in a direction the file states. A word beside a label is left out, so only
    what could be the label speaks for which way it runs.
    """
    runs: list[PrintedRun] = []
    for item in texts:
        if not any(character.isnumeric() for character in item.text) or not item.image_extent:
            continue
        xs = [point.x for point in item.image_extent]
        ys = [point.y for point in item.image_extent]
        runs.append(
            PrintedRun(
                box=(min(xs), min(ys), max(xs), max(ys)),
                rotation_degrees=item.rotation_degrees,
            )
        )
    return tuple(runs)


def _overlaps(first: Box, second: Box) -> bool:
    return (
        first[0] <= second[2]
        and second[0] <= first[2]
        and first[1] <= second[3]
        and second[1] <= first[3]
    )


def label_turn(region: Box, printed: Sequence[PrintedRun], *, geometry_degrees: int) -> int:
    """How far the label in `region` is turned on the page: 90, 270, or 0 to read it as it stands.

    **The file's own text first.** Where runs of text holding a numeral lie in the region, wholly or
    in part, they say: one direction, 90 or 270, is the label's. Runs that disagree — an upright
    label touching a sideways one — settle nothing, and the label is read as it stands, as the
    geometry rule reads a label with runs both ways.

    **Otherwise its drawn glyphs**: `geometry_degrees`, from `label_geometry`, which says 90 for a
    label whose characters run up the page and 0 otherwise (#783). Where nothing says, 0.
    """
    directions = {run.rotation_degrees for run in printed if _overlaps(run.box, region)}
    if directions:
        if len(directions) != 1:
            return 0
        (direction,) = directions
        return direction if direction in _TURNS else 0
    return geometry_degrees if geometry_degrees in _TURNS else 0


def sharper_box(box: Box, *, base_dpi: int, dpi: int) -> Box:
    """`box`, cut at `base_dpi`, as the pixels at `dpi` that cover the same page area.

    Scaled out, never in: the left and top round down and the right and bottom up, so the picture
    shows all the crop showed. Exact when `dpi` is a multiple of `base_dpi`.
    """
    left, top, right, bottom = box
    return (
        left * dpi // base_dpi,
        top * dpi // base_dpi,
        -(-right * dpi // base_dpi),
        -(-bottom * dpi // base_dpi),
    )


def reader_picture(
    picture: ReaderPicture,
    *,
    as_cut: bytes,
    crop_box: Box,
    base_dpi: int,
    settings: PictureSettings | None,
    turn: int,
    render: Callable[[Box, int], bytes],
) -> bytes:
    """The PNG a reader is shown for one crop.

    `as_cut` is the crop the stage cut from its own render, round `crop_box` at `base_dpi`; a
    reader shown the crop as cut is given exactly those bytes. Otherwise the same page area is
    rendered at the stated sharper dpi by `render(box, dpi)` — `render_region` on the page — and
    turned by `turn`, from `label_turn`. A rendering that fails raises, and the caller records why:
    another picture is never sent in its place, because the reader was measured on this one.
    """
    if picture is ReaderPicture.AS_CUT:
        return as_cut
    if settings is None:
        raise ValueError(
            f"this reader is shown a sharper picture, and {SHARPER_PICTURE_DPI_ENV} is not stated"
        )
    if settings.sharper_dpi <= base_dpi:
        raise ValueError(
            f"a sharper picture must be rendered above the stage's {base_dpi} dpi, not at "
            f"{settings.sharper_dpi}"
        )
    sharper = render(
        sharper_box(crop_box, base_dpi=base_dpi, dpi=settings.sharper_dpi), settings.sharper_dpi
    )
    if turn in _TURNS:
        return upright_png(sharper, label_rotation_degrees=turn)
    return sharper
