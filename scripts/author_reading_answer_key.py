"""Prepare a crop set so that authoring a reading answer key is reading, not building (#689).

**This script never authors a value.** It produces the crops and the empty columns; a person reads
each crop and types what it says. That division is the point: #666 is admin-owned because the answer
key is only worth having if a human established it, and a key our own pipeline agreed with measures
agreement with ourselves. `eval/experiments/model_bakeoff.py` refuses such a key outright.

The steps, in the order a person meets them.

    sample    DRAWING.pdf --pages 2-17 --out data/goldset/reading-key-DATE-NAME/ \\
              --reader-settings scripts/demo.sh --prefix s2 --quota stacked=10 ... (#867)
              -> a dimension-first sample: numbered PNGs, a wide view of each, crops.csv
    scaffold  DRAWING.pdf --pages 3-5 --out data/goldset/reading-key/ --count 50 \\
              --reader-settings scripts/demo.sh
              -> the older sample, of regions next to a dimension line, in the same sheet

    triage    KEY_DIR [KEY_DIR ...] --minimum-share 0.6
              -> per drawing, how many crops a person marked as not a dimension
    check     KEY_DIR --pdf DRAWING.pdf
              -> a blind "look again" list, by crop, where what was typed differs from the file
    build     KEY_DIR --pdf DRAWING.pdf --annotator "..." --on 2026-09-27 --case-id NAME
              -> answer_key.json + model_bakeoff_metadata.json, verified by loading them back

**The sheet splits the vendor's number from GV's (#867).** `vendor_value` is the number the drawing
prints in black; `gv_value_seen` is GV's own number where the crop shows one in colour, which is
never the vendor's. `stacked`, `dual_unit`, `cut_off` and `rotated` are the person's ticks, and
`not_a_dimension` the triage mark. A sheet scaffolded before #867 has a `value` column, which
`build` still reads as the vendor's number.

**`sample` is dimension-first** (`eval/experiments/key_frame.py`): its places are the drawing's
printed text, path regions shaped like a label, the vendor's label at each place GV's reviewer
corrected, and regions a run agreed on — read from a run's database inside a read-only transaction,
by place alone. Kinds come from geometry, each drawn to a stated quota. **It never reads what the
vendor's drawing says, or what any reader read there**: the frame is built from where things are
and how the file lays them out. GV's own text is read for one thing only, to find where GV
corrected a dimension.

**The crops are cut the way production cuts them (#835).** The regions come from the pipeline's own
region finder, planned with the thresholds the stage runs with — read from the settings file the
worker is started with, never copied into this script. Each crop is the region plus the stage's
vision margin (`workflow.stages.VISION_CROP_CONTEXT_MARGIN_PT`), at `VISION_CROP_DPI`, from the
vendor's layer only. It is rendered by the bake-off's own `render_crop`, so the picture a person
reads is, byte for byte, the picture `load_crops` later shows a model. Before #835 the scaffold cut
the bare region, with no margin and its own thresholds: a person read crops no model is shown, and
the margin's absence cut labels short.

**The key records its frame** — the dpi its polygons are in and the margin they hold. The bake-off,
the agent scorecard and the geometry check refuse a key that records none unless their caller names
it, and refuse a named frame that differs from the recorded one (`key_polygon_dpi`).

The sample is stratified, because a key made only of legible crops measures nothing that matters.

**Nothing it writes belongs in the repository.** The output directory lives under `data/`, which is
gitignored, and `tests/test_repo_hygiene.py` asks git rather than reading this file.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import random
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

import pypdfium2 as pdfium  # type: ignore[import-untyped]

# Run as a file, the repository is not on the path; the other scripts here add it the same way.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.experiments.key_frame import (
    Geometry,
    Glyph,
    Kind,
    LabelShape,
    Layout,
    LookAgain,
    Place,
    Site,
    Source,
    Typed,
    Witnessed,
    frame,
    look_again,
    sample,
    triage,
)
from eval.experiments.model_bakeoff import (
    HARD_CASE_TAGS,
    KeyFrame,
    ModelBakeoffError,
    key_frame,
    load_crops,
    render_crop,
)
from eval.markup_yardstick import GvNote, SiteKind, site_of
from evidence.coordinates import ImagePoint, PageTransform
from evidence.crop import POINTS_PER_INCH, decode_rgb_png, encode_png
from extraction.agent.geometry import LabelReach, label_geometry
from extraction.annotations import (
    PathSegment,
    SegmentKind,
    StackedFraction,
    VectorPath,
    page_box_polygon,
    read_annotation_layers,
    read_markup_layer,
)
from extraction.geometry.containment import DimensionExtent
from extraction.geometry.dimension_lines import detect
from extraction.geometry.text_association import DimensionText, associate, lines_within
from extraction.glyph_bands import FractionBarGeometry
from extraction.rasterise import VISION_CROP_DPI
from extraction.reader import MissingSpace, SetAsideReason, read_page_contents, read_pages
from extraction.stamp_text import drawing_ink, read_stamp_text, stamps_only
from extraction.vector_first import plan_reads
from units.measurement import Measurement
from units.normalise import UnitNormalisationError, normalise_to_inches
from units.notation import canonical_notation, is_compound
from workflow.stages import VISION_CROP_CONTEXT_MARGIN_PT, crop_shows_a_stacked_fraction

CROPS_CSV: Final = "crops.csv"
ANSWER_KEY: Final = "answer_key.json"
BAKEOFF_METADATA: Final = "model_bakeoff_metadata.json"
HOW_TO: Final = "HOW_TO_READ_THESE.md"
CONTACT_SHEET: Final = "contact_sheet.html"
FRAME_RECORD: Final = "frame.json"

#: The columns a person fills in, in the order the sheet shows them (#867). Every sheet this script
#: writes ends with them; nothing before them is the person's to change.
PERSON_COLUMNS: Final = (
    "vendor_value",
    "gv_value_seen",
    "unreadable",
    "cut_off",
    "not_a_dimension",
    "not_a_single_value",
    "stacked",
    "dual_unit",
    "rotated",
    "note",
)

#: Where a crop is, before anything about what it shows: the columns `build` and every loader place
#: it by.
_LOCATION_COLUMNS: Final = (
    "crop_id",
    "image",
    "wide_image",
    "page",
    "left_px",
    "top_px",
    "right_px",
    "bottom_px",
    "width_px",
    "height_px",
    "stratum",
)

#: The frame every crop this script cuts is in: pixels at the vision crop resolution, each polygon
#: holding the stage's vision margin round its region. Written beside the crops by `scaffold` and
#: carried into the key by `build`, so a loader never has to be told it (#835).
_FRAME: Final = KeyFrame(polygon_dpi=VISION_CROP_DPI, margin_pt=VISION_CROP_CONTEXT_MARGIN_PT)

#: How much drawing the wide view shows round a region, so a label the crop cut can be seen carrying
#: on past the crop's edge. The width the #778 check sheet uses. A viewing aid; nothing is decided
#: by it, and no model is shown it.
WIDE_VIEW_PT: Final = Decimal(30)

#: The colour of the crop's edge in the wide view, the red the #778 check sheet draws it in.
_EDGE: Final = b"\xdd\x11\x11"

#: Longest axis, in pixels at the crop DPI, below which a region is a small glyph rather than a
#: label. Measured, not chosen: on `AI_Set_2.pdf` the median planned region is 37px and a legible
#: dimension label runs past 100px, so the boundary separates the two populations rather than
#: splitting one. It is a sampling aid only — it decides which crops a person is shown, never what
#: any of them says.
SMALL_GLYPH_PX: Final = 60

#: Longest axis, in pixels at the crop DPI, below which a region is a speck rather than a reading
#: task. This is the boundary the previous scaffold only warned about. It is still just an
#: authoring-filter: values below it are reported and excluded from the sheet; nothing here decides
#: what any surviving crop says.
MIN_LEGIBLE_AXIS_PX: Final = 20


class ScaffoldError(Exception):
    """The crop set could not be prepared, or the filled sheet could not be trusted."""


def _reader_settings(path: Path) -> dict[str, str]:
    """The stage's reader thresholds, from the settings file the worker is started with.

    **Read, never copied** (#835). This script used to carry its own values, and they had drifted
    from the ones `scripts/demo.sh` starts the worker with — a 12 pt line minimum against its 6 — so
    the regions a person read were not the regions the stage plans. Every value is required and none
    has a default; the file is read by the function `scripts/agent_geometry_check.py` reads it with.
    """
    from scripts.agent_geometry_check import CheckError, read_settings
    from scripts.glyph_inventory import InventoryError

    try:
        return read_settings(path)
    except (CheckError, InventoryError) as error:
        raise ScaffoldError(str(error)) from error


@dataclass(frozen=True, slots=True)
class Candidate:
    """One region a person will be asked to read, and the crop they will read it from."""

    crop_id: str
    page: int
    """One-based, as `GoldObservation` requires."""

    region: tuple[int, int, int, int]
    """The planned region — left, top, right, bottom — in pixels at `VISION_CROP_DPI`."""

    crop: tuple[int, int, int, int]
    """The region with the stage's vision margin round it, in the same pixels. This is what is
    rendered, and it is the polygon the key records."""

    stratum: str
    path_count: int
    line_count: int

    @property
    def width_px(self) -> int:
        """The region's width: the label's size, not the crop's."""
        return self.region[2] - self.region[0]

    @property
    def height_px(self) -> int:
        return self.region[3] - self.region[1]

    @property
    def long_axis_px(self) -> int:
        return max(self.width_px, self.height_px)


def _page_px(pdf: bytes, page_index: int) -> tuple[int, int]:
    """The page's size in whole pixels at `VISION_CROP_DPI`: as far as a crop may reach.

    Measured as `model_bakeoff.pdf_box` measures the page, so no crop reaches past the edge the
    bake-off checks a polygon against.
    """
    document = pdfium.PdfDocument(pdf)
    try:
        width_pt, height_pt = (Decimal(str(value)) for value in document[page_index].get_size())
    finally:
        document.close()
    per_px = POINTS_PER_INCH / Decimal(VISION_CROP_DPI)
    return (
        int((width_pt / per_px).to_integral_value(rounding=ROUND_FLOOR)),
        int((height_pt / per_px).to_integral_value(rounding=ROUND_FLOOR)),
    )


def _grown(
    region: tuple[int, int, int, int], *, margin_pt: Decimal, page_px: tuple[int, int]
) -> tuple[int, int, int, int]:
    """`region` with `margin_pt` of page round it, in whole pixels at `VISION_CROP_DPI`.

    Rounded outward, as `evidence.crop.crop_pixel_box` rounds production's crop, so no side has less
    margin than it was given; and held inside the page, as production's crop is held inside its
    render.
    """
    margin = margin_pt * Decimal(VISION_CROP_DPI) / POINTS_PER_INCH
    left, top, right, bottom = region
    width, height = page_px
    return (
        max(0, int((left - margin).to_integral_value(rounding=ROUND_FLOOR))),
        max(0, int((top - margin).to_integral_value(rounding=ROUND_FLOOR))),
        min(width, int((right + margin).to_integral_value(rounding=ROUND_CEILING))),
        min(height, int((bottom + margin).to_integral_value(rounding=ROUND_CEILING))),
    )


def _stratum(
    *,
    long_axis_px: int,
    stacked_glyphs: bool,
    baseline_rotation_degrees: int,
) -> str:
    """Sampling bucket, chosen from geometry and glyph layout rather than parsed content."""
    if baseline_rotation_degrees % 360:
        return "rotated"
    if stacked_glyphs:
        return "stacked_fraction"
    if long_axis_px < SMALL_GLYPH_PX:
        return "small_glyph"
    return "dimension_label"


def _candidates(pdf: bytes, *, page_index: int, settings: Mapping[str, str]) -> list[Candidate]:
    """The regions the stage would plan on one page that sit next to a detected dimension line,
    each with the crop production's margin makes of it.

    Planned as the stage plans its localized reads (`workflow/stages.py`): the same layer reader,
    the same `plan_reads`, and the same named settings in each place. The dimension-line filter is
    this script's own sampling choice, not the stage's.
    """
    layers = read_annotation_layers(
        pdf,
        page_index,
        document_version_id=uuid4(),
        dpi=VISION_CROP_DPI,
        line_minimum_pt=Decimal(settings["GV_READER_LINE_MINIMUM_PT"]),
        glyph_maximum_pt=Decimal(settings["GV_READER_GLYPH_MAXIMUM_PT"]),
        glyph_gap_pt=Decimal(settings["GV_READER_GLYPH_GAP_PT"]),
        fraction_bar=FractionBarGeometry(
            bar_thickness_max_pt=Decimal(settings["GV_READER_FRACTION_BAR_THICKNESS_MAX_PT"]),
            bar_length_min_pt=Decimal(settings["GV_READER_FRACTION_BAR_LENGTH_MIN_PT"]),
            reach_pt=Decimal(settings["GV_READER_FRACTION_REACH_PT"]),
            glyph_min_pt=Decimal(settings["GV_READER_FRACTION_GLYPH_MIN_PT"]),
            glyph_max_pt=Decimal(settings["GV_READER_FRACTION_GLYPH_MAX_PT"]),
            proportion_max=Decimal(settings["GV_READER_FRACTION_PROPORTION_MAX"]),
            character_gap_pt=Decimal(settings["GV_READER_FRACTION_CHARACTER_GAP_PT"]),
            turned_aspect_min=Decimal(settings["GV_READER_FRACTION_TURNED_ASPECT_MIN"]),
        ),
    )
    plan = plan_reads(
        layers,
        proximity_limit=Decimal(settings["GV_READER_PROXIMITY_LIMIT"]),
        minimum_paths=int(settings["GV_READER_LOCALIZED_MINIMUM_PATHS"]),
        maximum_span=Decimal(settings["GV_READER_LOCALIZED_MAXIMUM_SPAN"]),
    )
    detected = detect(
        layers.drawing_segments,
        witness_tolerance=Decimal(settings["GV_READER_WITNESS_TOLERANCE"]),
        minimum_span=Decimal(settings["GV_READER_MINIMUM_SPAN"]),
        straightness=Decimal(settings["GV_READER_STRAIGHTNESS"]),
        crossing_margin=Decimal(settings["GV_READER_CROSSING_MARGIN"]),
    )
    dimension_lines = tuple(line.extent for line in detected.lines)
    page_px = _page_px(pdf, page_index)
    found: list[Candidate] = []
    for index, entry in enumerate(plan.to_read):
        near_dimension_lines = lines_within(
            entry.region.extent,
            dimension_lines,
            proximity_limit=Decimal(settings["GV_READER_PROXIMITY_LIMIT"]),
        )
        if not near_dimension_lines:
            continue
        points = entry.region.image_extent
        left = min(point.x for point in points)
        right = max(point.x for point in points)
        top = min(point.y for point in points)
        bottom = max(point.y for point in points)
        if right <= left or bottom <= top:
            continue
        long_axis = max(right - left, bottom - top)
        region = (left, top, right, bottom)
        found.append(
            Candidate(
                crop_id=f"p{page_index + 1}-r{index:04d}",
                page=page_index + 1,
                region=region,
                crop=_grown(region, margin_pt=VISION_CROP_CONTEXT_MARGIN_PT, page_px=page_px),
                stratum=_stratum(
                    long_axis_px=long_axis,
                    stacked_glyphs=entry.region.stacked_glyphs,
                    baseline_rotation_degrees=entry.region.baseline_rotation_degrees,
                ),
                path_count=entry.region.path_count,
                line_count=len(near_dimension_lines),
            )
        )
    return found


def _stratified(candidates: list[Candidate], *, count: int, seed: int) -> list[Candidate]:
    """A sample that spans the strata rather than the first N of whatever the page drew first.

    Round-robin across strata, so a page that is 87% small glyphs does not produce a key that is
    87% small glyphs — and, just as important, so a page dominated by legible labels still yields
    the hard cases. **A key made only of the easy ones measures nothing that matters.**

    Seeded, so the same page gives the same set: a person half way through reading fifty crops must
    not have them renumbered underneath them by a re-run.
    """
    by_stratum: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        by_stratum.setdefault(candidate.stratum, []).append(candidate)
    rng = random.Random(seed)
    for pool in by_stratum.values():
        rng.shuffle(pool)

    chosen: list[Candidate] = []
    order = sorted(by_stratum)
    while len(chosen) < count and any(by_stratum[name] for name in order):
        for name in order:
            if not by_stratum[name]:
                continue
            chosen.append(by_stratum[name].pop())
            if len(chosen) >= count:
                break
    return sorted(chosen, key=lambda candidate: (candidate.page, candidate.crop_id))


def _wide_name(crop_id: str) -> str:
    return f"{crop_id}_wide.png"


def _wide_view(
    pdf: bytes,
    *,
    page: int,
    region: tuple[int, int, int, int],
    crop: tuple[int, int, int, int],
    crop_size: tuple[int, int],
    page_px: tuple[int, int],
) -> bytes:
    """The region with `WIDE_VIEW_PT` of drawing round it, and the crop's edge drawn in red.

    Rendered as the crop is, from the vendor's layer only. The line runs on the pixels just outside
    the crop, so it covers nothing a model is shown; `crop_size` is the crop as rendered, which can
    be a pixel short of its polygon.
    """
    wide = _grown(region, margin_pt=WIDE_VIEW_PT, page_px=page_px)
    width, height, rgb = decode_rgb_png(
        render_crop(
            pdf,
            page=page,
            polygon=wide,
            polygon_dpi=VISION_CROP_DPI,
            output_dpi=VISION_CROP_DPI,
        )
    )
    pixels = bytearray(rgb)
    left = crop[0] - wide[0]
    top = crop[1] - wide[1]
    right = left + crop_size[0]
    bottom = top + crop_size[1]
    edge = [(x, y) for x in range(left - 1, right + 1) for y in (top - 1, bottom)]
    edge += [(x, y) for y in range(top, bottom) for x in (left - 1, right)]
    for x, y in edge:
        if 0 <= x < width and 0 <= y < height:
            offset = (y * width + x) * 3
            pixels[offset : offset + 3] = _EDGE
    return encode_png(width, height, bytes(pixels))


@dataclass(frozen=True, slots=True)
class SheetRow:
    """One crop as the sheet lists it: where it is, what it was drawn as, and nothing it shows."""

    crop_id: str
    page: int
    region: tuple[int, int, int, int]
    """The planned region, in pixels at `VISION_CROP_DPI`: the label's size, not the crop's."""

    crop: tuple[int, int, int, int]
    """The region with the stage's vision margin round it: what is rendered, and the polygon the key
    records."""

    stratum: str
    extra: tuple[str, ...] = ()
    """The values of the sheet's own extra columns, in their order."""


def _write_sheet(out: Path, rows: Sequence[SheetRow], *, extra_columns: Sequence[str]) -> None:
    """The sheet a person fills in: where each crop is, then the person's columns, all empty.

    The four box columns are the crop, in pixels at `VISION_CROP_DPI` — the polygon `build` records.
    """
    with (out / CROPS_CSV).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([*_LOCATION_COLUMNS, *extra_columns, *PERSON_COLUMNS])
        for row in rows:
            if len(row.extra) != len(extra_columns):
                raise ScaffoldError(f"{row.crop_id}: its extra columns do not match the sheet's")
            writer.writerow(
                [
                    row.crop_id,
                    f"{row.crop_id}.png",
                    _wide_name(row.crop_id),
                    row.page,
                    *row.crop,
                    row.crop[2] - row.crop[0],
                    row.crop[3] - row.crop[1],
                    row.stratum,
                    *row.extra,
                    *("" for _ in PERSON_COLUMNS),
                ]
            )


def _write_contact_sheet(out: Path, cards: Sequence[tuple[str, str]]) -> None:
    """A browseable sheet of the crops, each beside its wide view, before anyone types answers.

    `cards` is each crop's id and the caption under it."""
    figures = []
    for crop_id, caption in cards:
        name = html.escape(crop_id)
        image = html.escape(f"{crop_id}.png")
        wide = html.escape(_wide_name(crop_id))
        figures.append(
            f'<figure><img src="{image}" alt="{name}">'
            f'<img src="{wide}" alt="{name}, wide view"><figcaption>'
            f"<strong>{name}</strong><br>{html.escape(caption)}</figcaption></figure>"
        )
    document = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>Reading Answer-Key Contact Sheet</title>
<style>
body { font-family: system-ui, sans-serif; margin: 24px; color: #222; }
h1 { font-size: 20px; margin: 0 0 16px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 16px; }
figure { margin: 0; border: 1px solid #ddd; padding: 10px; background: #fafafa; }
img { display: inline-block; width: 48%; height: 160px; object-fit: contain; margin: 0 1% 8px; }
figcaption { font-size: 12px; line-height: 1.35; overflow-wrap: anywhere; }
</style>
<h1>Reading Answer-Key Contact Sheet</h1>
<div class="grid">
"""
    document += "\n".join(figures)
    document += "\n</div>\n</html>\n"
    (out / CONTACT_SHEET).write_text(document, encoding="utf-8")


def _legible(candidates: list[Candidate]) -> tuple[list[Candidate], int]:
    """Drop specks before sampling, and report the count to the person running the scaffold."""
    kept = [candidate for candidate in candidates if candidate.long_axis_px >= MIN_LEGIBLE_AXIS_PX]
    return kept, len(candidates) - len(kept)


_HOW_TO_TEXT: Final = """# Reading these crops

Each crop (`s2-p3-004.png`) is exactly the picture the readers you are marking are shown. Beside it
is a wide view (`s2-p3-004_wide.png`): the same place with more drawing round it, and the crop's
edge in red. `contact_sheet.html` shows them side by side. Nothing here has read them: every value
in `crops.csv` will be one you typed.

For each row of `crops.csv`, fill in what applies and leave the rest empty. Put an `x` in a tick
column.

- **`vendor_value`**: the number the drawing prints in black, exactly as written, with its unit:
  `28 3/4"`, `648 mm`, `25-1/2"`, `381 [15]`, `2" (VIF)`. Not `28.75"` (the check is exact, so a
  rounded answer is a wrong one) and not `28 3/4` (a number with no unit is refused).
- **`gv_value_seen`**: if GV's own number shows in the crop, in red, blue or another colour, type it
  as GV wrote it. It is never the vendor's number, even where it covers it.
- **`unreadable`**: you cannot read the black number. Leave `vendor_value` empty.
- **`cut_off`**: the label carries on past the red line in the wide view. Leave `vendor_value`
  empty, and write what the wide view shows in `note` if you like.
- **`not_a_dimension`**: the crop holds no dimension at all: a symbol, hatching, a word.
- **`not_a_single_value`**: two numbers and an operator, like `39 1/4"+6"`. Do not add them up.
- **`stacked`**: the fraction is set one number over the other.
- **`dual_unit`**: millimetres and inches together, like `305` over `[12]`.
- **`rotated`**: the text runs sideways or upside down.
- **`note`**: anything else worth saying. Nothing reads it.

Leave every other column alone: they place the crop on the page.

When you are done, run the look-again check. It lists crops where what you typed differs from the
drawing's own printed text or from GV's, without saying what either says:

    python scripts/author_reading_answer_key.py check THIS_FOLDER --pdf THE_DRAWING.pdf
"""


def _render_crops(pdf: bytes, out: Path, rows: Sequence[SheetRow]) -> tuple[list[SheetRow], int]:
    """Each row's crop and wide view, written beside the sheet, and how many would not render.

    **A crop that will not render is not a crop**, and the count is reported rather than absorbed.
    Before the margin, 29 of 361 planned regions on page 3 of `AI_Set_2.pdf` were one or two pixels
    across and PDFium refused them. Each crop now holds the margin round its region, so a refusal
    here is rarer, and still the renderer's to make rather than a minimum size invented here.

    **Rendered by `render_crop`, the bake-off's own renderer**, from the polygon the key will
    record, at `VISION_CROP_DPI`: the picture a person reads. A model is shown the same polygon at
    production's reader resolution (`load_crops`).
    """
    written: list[SheetRow] = []
    unrenderable = 0
    page_px = {page: _page_px(pdf, page - 1) for page in {row.page for row in rows}}
    for row in rows:
        try:
            image = render_crop(
                pdf,
                page=row.page,
                polygon=row.crop,
                polygon_dpi=VISION_CROP_DPI,
                output_dpi=VISION_CROP_DPI,
            )
            width, height, _rgb = decode_rgb_png(image)
            wide = _wide_view(
                pdf,
                page=row.page,
                region=row.region,
                crop=row.crop,
                crop_size=(width, height),
                page_px=page_px[row.page],
            )
        except (ModelBakeoffError, ValueError):
            unrenderable += 1
            continue
        (out / f"{row.crop_id}.png").write_bytes(image)
        (out / _wide_name(row.crop_id)).write_bytes(wide)
        written.append(row)
    return written, unrenderable


def scaffold(arguments: argparse.Namespace) -> int:
    pdf_path = Path(arguments.pdf)
    try:
        pdf = pdf_path.read_bytes()
    except OSError as error:
        raise ScaffoldError(f"could not read {pdf_path}: {error}") from error

    settings = _reader_settings(Path(arguments.reader_settings))

    candidates: list[Candidate] = []
    for page_index in _page_indexes(arguments.pages):
        try:
            candidates.extend(_candidates(pdf, page_index=page_index, settings=settings))
        except Exception as error:  # noqa: BLE001 - one unreadable page must not lose the rest
            print(f"  page {page_index + 1}: skipped ({type(error).__name__}: {error})")

    if not candidates:
        raise ScaffoldError(
            "no regions were planned next to detected dimension lines on those pages. Either the "
            f"stage's thresholds in {arguments.reader_settings} exclude everything on this drawing, "
            "the dimension-line detector found no lines, or the pages carry no outlined text."
        )

    candidates, too_small = _legible(candidates)
    if not candidates:
        raise ScaffoldError(
            f"all planned regions were under {MIN_LEGIBLE_AXIS_PX}px on their longest axis. They "
            "are too small to ask a person to read; choose other pages before authoring a key."
        )

    chosen = _stratified(candidates, count=arguments.count, seed=arguments.seed)
    out = Path(arguments.out)
    out.mkdir(parents=True, exist_ok=True)

    rows = [
        SheetRow(
            crop_id=candidate.crop_id,
            page=candidate.page,
            region=candidate.region,
            crop=candidate.crop,
            stratum=candidate.stratum,
            extra=(str(candidate.line_count), str(candidate.path_count)),
        )
        for candidate in chosen
    ]
    written, unrenderable = _render_crops(pdf, out, rows)
    if not written:
        raise ScaffoldError(
            f"none of the {len(chosen)} sampled regions could be rendered as a crop; the renderer "
            "refused every one."
        )

    kept = {row.crop_id for row in written}
    chosen = [candidate for candidate in chosen if candidate.crop_id in kept]
    _write_sheet(out, written, extra_columns=("near_dimension_lines", "path_count"))
    _write_contact_sheet(
        out,
        [
            (
                candidate.crop_id,
                (
                    f"p{candidate.page} · {candidate.stratum} · "
                    f"label {candidate.width_px}x{candidate.height_px}px · "
                    f"{candidate.line_count} line(s)"
                ),
            )
            for candidate in chosen
        ],
    )
    (out / HOW_TO).write_text(_HOW_TO_TEXT, encoding="utf-8")
    # **The frame, written beside the crops it describes** (#835). `build` carries it into the key,
    # and refuses a sheet without one: a sheet cut before #835 holds crops no model is shown.
    (out / BAKEOFF_METADATA).write_text(
        json.dumps({"frame": _FRAME.model_dump(mode="json"), "tags": {}}, indent=2) + "\n",
        encoding="utf-8",
    )

    # **Printed, because a thin stratum is worth seeing before anybody reads fifty crops.** A set
    # that turned out to be all small glyphs would still produce a scorecard, and the scorecard
    # would quietly be about one kind of crop.
    counts = Counter(candidate.stratum for candidate in chosen)
    available = Counter(candidate.stratum for candidate in candidates)
    print(f"\n  {len(chosen)} crops written to {out}")
    if too_small:
        print(
            f"    {too_small} planned region(s) were under {MIN_LEGIBLE_AXIS_PX}px on their "
            "longest axis and were dropped as too small to read"
        )
    if unrenderable:
        print(f"    {unrenderable} sampled crop(s) could not be rendered and were dropped")
    for stratum in sorted(available):
        print(f"    {stratum:<12} {counts.get(stratum, 0):>4} of {available[stratum]} available")
    # **The size spread, because it predicts how the session will go.** A set whose median label is
    # 37px is a set where most of the reader's time goes on ticking `unreadable` — which is a real
    # measurement of the region finder, and much better learned here than forty crops in. Measured
    # on the regions, not the crops: the margin makes every crop look big enough.
    widths = sorted(candidate.width_px for candidate in chosen)
    tiny = sum(1 for width in widths if width < 20)
    print(
        f"\n  label width px: min {widths[0]}, median {widths[len(widths) // 2]}, max {widths[-1]}"
    )
    if tiny:
        print(
            f"    {tiny} of {len(widths)} are under 20px wide, and unlikely to be readable. They are\n"
            "    the stage's own regions, so they are kept: an unreadable tick is the measurement."
        )
    print(f"\n  reader thresholds: {arguments.reader_settings}")
    print(
        "\n  the rotated CSV column is NOT assigned here: it is still for the reader to tick when\n"
        "  the crop itself reads sideways or upside down (#689).\n"
        f"\n  Contact sheet: {out / CONTACT_SHEET}"
        f"\n  Next: open {out / HOW_TO}\n"
    )
    return 0


def _page_indexes(pages: str) -> list[int]:
    """`3` or `3-5` or `1,4,9`, as zero-based indexes."""
    indexes: list[int] = []
    for part in pages.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            first, _, last = part.partition("-")
            try:
                start, end = int(first), int(last)
            except ValueError as error:
                raise ScaffoldError(f"could not read page range {part!r}") from error
            if start < 1 or end < start:
                raise ScaffoldError(f"page range {part!r} is not one-based and ascending")
            indexes.extend(range(start - 1, end))
        else:
            try:
                number = int(part)
            except ValueError as error:
                raise ScaffoldError(f"could not read page {part!r}") from error
            if number < 1:
                raise ScaffoldError("pages are one-based")
            indexes.append(number - 1)
    if not indexes:
        raise ScaffoldError("no pages given")
    return sorted(set(indexes))


#: Anything a person typed that means "no unit, and none is coming". Checked before the parser, so
#: the message names the row rather than reporting a parse failure on an empty string.
_BLANK: Final = ""


class NotASingleValue(ScaffoldError):
    """The crop carries an instruction to add, not one dimension (#730)."""


#: The notation rules live in `units/notation.py`, shared with every reading lane: one canonicaliser,
#: so the key and the scorer can never disagree about what a written dimension means (#732).
_canonical = canonical_notation


def _parsed(raw: str, *, crop_id: str) -> Measurement:
    """One typed value, exactly, or a refusal that names the crop.

    A decimal is refused before `units/` sees it. `normalise_to_inches` accepts `28.75"` happily and
    it is a wrong answer under exact match (Q2): the drawing says `28 3/4"`, and a key that recorded
    the decimal would score a model wrong for reading the drawing right.

    **The notations the client's drawings actually use are accepted here, not in `units/`.** A
    hyphenated fraction, a dual-unit token and a trailing site note are how the trade writes a
    dimension; `units/` is on the verdict path and does not move for a transcription convenience
    (#730).
    """
    token = raw.strip()
    if is_compound(token):
        raise NotASingleValue(
            f"{crop_id}: {token!r} is two dimensions and an operator, not one value. Tick "
            "`not_a_single_value` and leave `vendor_value` empty. Do not add them up — arithmetic we "
            "performed is not something a reader read, and a model that returned this exactly "
            "would then be scored wrong."
        )
    token, _mm = _canonical(token)
    if "." in token:
        raise ScaffoldError(
            f"{crop_id}: {token!r} is a decimal. Write it the way the drawing writes it — "
            '28 3/4" and not 28.75". The verdict is exact match, so a rounded answer is a wrong '
            "answer, and this key would then mark a correct reading as a miss."
        )
    try:
        return normalise_to_inches(token)
    except UnitNormalisationError as refused:
        raise ScaffoldError(
            f'{crop_id}: {refused}. Give the value with its unit, for example 25 1/2" or 648 mm. '
            'A hyphenated fraction (25-1/2"), a dual-unit token (381 [15]) and a trailing note '
            '(2" (VIF)) are all accepted as written.'
        ) from refused


#: The person's tick columns that become a bake-off tag, and the tag each becomes (#867).
_TICKED_TAGS: Final = (("rotated", "rotated"), ("stacked", "stacked"), ("dual_unit", "dual_unit"))


def _ticked(row: Mapping[str, str | None], column: str) -> bool:
    return bool((row.get(column) or _BLANK).strip())


def _vendor_text(row: Mapping[str, str | None]) -> str:
    """The vendor's number as typed: `vendor_value`, or `value` on a sheet scaffolded before #867."""
    column = "vendor_value" if "vendor_value" in row else "value"
    return (row.get(column) or _BLANK).strip()


def _gv_number(raw: str, *, crop_id: str) -> Fraction | None:
    """GV's number as typed in `gv_value_seen`, in inches, or `None` where nothing was typed.

    Read by #850's rule for GV's own text (`eval.markup_yardstick.site_of`): one value, with or
    without a unit, because the person types it as GV wrote it and GV does not always write one. A
    sum, a sentence or a word is refused, so the look-again check has a number to compare.
    """
    text = raw.strip()
    if not text:
        return None
    found = site_of(GvNote(0, text, (Decimal(0), Decimal(0), Decimal(1), Decimal(1))))
    if found is None or found.kind is not SiteKind.SINGLE or found.gv_value is None:
        raise ScaffoldError(
            f"{crop_id}: {text!r} in `gv_value_seen` is not one number. Type GV's number as GV "
            'wrote it, for example 30-1/2" or 30; put anything longer in `note`.'
        )
    return found.gv_value


def _sheet_frame(out: Path) -> KeyFrame:
    """The frame `scaffold` recorded beside the crops, which `build` carries into the key (#835).

    **Copied, not assumed from today's constants.** A sheet cut under another margin is a sheet
    cut under another margin, and the key must say so rather than claim the current one.
    """
    try:
        frame = key_frame(out)
    except ModelBakeoffError as error:
        raise ScaffoldError(
            f"the frame recorded beside the crops is unreadable: {error}"
        ) from error
    if frame is None:
        raise ScaffoldError(
            f"{out} does not record the frame its crops were cut in, so it was scaffolded before "
            "#835 made crops match production's. Its crops are not the ones a model is shown: "
            "scaffold the pages again rather than build a key from these."
        )
    return frame


def _rows(out: Path) -> list[dict[str, str]]:
    sheet = out / CROPS_CSV
    try:
        with sheet.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except OSError as error:
        raise ScaffoldError(f"could not read {sheet}: {error}") from error


def build(arguments: argparse.Namespace) -> int:
    out = Path(arguments.out)
    rows = _rows(out)
    if not rows:
        raise ScaffoldError(f"{out / CROPS_CSV} has no rows; run scaffold first")
    frame = _sheet_frame(out)

    pdf_path = Path(arguments.pdf)
    try:
        pdf = pdf_path.read_bytes()
    except OSError as error:
        raise ScaffoldError(f"could not read {pdf_path}: {error}") from error

    observations: list[dict[str, object]] = []
    tags: dict[str, list[str]] = {}
    unreadable: list[str] = []
    cut_off: list[str] = []
    not_a_dimension: list[str] = []
    not_a_single_value: list[str] = []
    empty: list[str] = []

    for index, row in enumerate(rows):
        crop_id = (row.get("crop_id") or "").strip()
        if not crop_id:
            raise ScaffoldError(f"row {index} has no crop_id; do not reorder or edit those columns")
        typed = _vendor_text(row)
        # **GV's number is checked wherever it was typed**, scored or not: on a crop whose vendor's
        # label GV's box covers, it is the only number the person could see.
        _gv_number(row.get("gv_value_seen") or _BLANK, crop_id=crop_id)
        if _ticked(row, "unreadable"):
            # **Recorded, not dropped.** A crop no person can read is a crop no model should be
            # trusted on, and the count belongs in the result rather than in the difference between
            # two numbers nobody compares.
            unreadable.append(crop_id)
            continue
        if _ticked(row, "cut_off"):
            # **Not scored, like an unreadable crop**: the crop shows part of a label, and what the
            # whole label says is not what a reader of this crop was shown.
            if typed:
                raise ScaffoldError(
                    f"{crop_id}: `cut_off` is ticked and a `vendor_value` is typed. A cut-off crop "
                    "is not scored, because its reader is shown only part of the label: leave "
                    "`vendor_value` empty, and put what the wide view shows in `note`."
                )
            cut_off.append(crop_id)
            continue
        if _ticked(row, "not_a_dimension"):
            not_a_dimension.append(crop_id)
            continue
        if _ticked(row, "not_a_single_value"):
            # **A different fact from `unreadable`, and kept apart from it deliberately (#730).**
            # "I could not read it" is about legibility; "this is not one dimension" is about what
            # the drawing says. Counting them together would hide how often these sheets carry a
            # compound like `39 1/4"+6"`, which is itself a finding, and would make the key's
            # unreadable rate look worse than the drawings are.
            not_a_single_value.append(crop_id)
            continue
        if not typed:
            empty.append(crop_id)
            continue

        value = _parsed(typed, crop_id=crop_id)
        try:
            polygon = tuple(
                int(row[name]) for name in ("left_px", "top_px", "right_px", "bottom_px")
            )
            page = int(row["page"])
        except (KeyError, ValueError) as error:
            raise ScaffoldError(
                f"{crop_id}: the page and polygon columns are unreadable ({error}). They locate the "
                "crop and build needs them exactly as scaffold wrote them."
            ) from error

        # The bake-off derives its own crop id from position in the file, so the tag map is keyed by
        # index as well as by name — `_tags_for` reads either. Every tag is a person's tick.
        bakeoff_id = str(len(observations))
        chosen = [tag for column, tag in _TICKED_TAGS if _ticked(row, column)]
        if (row.get("gv_value_seen") or _BLANK).strip():
            chosen.append("gv_seen")
        if chosen:
            tags[bakeoff_id] = chosen
            tags[crop_id] = list(chosen)

        observations.append(
            {
                # Required by the schema and **ignored by the bake-off**, which scores crop against
                # value and nothing else. A neutral type here rather than a plausible one: inviting
                # a reader to pick a semantic type is #188's job and needs the architectural set.
                "semantic_type": "cabinet_width",
                "source": "SHOP",
                "value": _measurement_payload(value, typed),
                "page": page,
                "polygon": list(polygon),
                "item_id": crop_id,
            }
        )

    if not observations:
        raise ScaffoldError(
            f"no values were typed in {out / CROPS_CSV}. "
            f"{len(unreadable)} marked unreadable, {len(cut_off)} cut off, "
            f"{len(not_a_dimension)} not a dimension, {len(not_a_single_value)} not a single "
            f"value, {len(empty)} left blank."
        )

    digest = hashlib.sha256(pdf).hexdigest()
    case = {
        "id": arguments.case_id,
        "product_type": "cabinet",
        "arch": pdf_path.name,
        "shop": pdf_path.name,
        "ground_truth": {
            "observations": observations,
            "matches": [],
            "expected_findings": [],
        },
        "provenance": {
            "annotator": arguments.annotator,
            "annotated_on": arguments.on.isoformat(),
            "documents": [
                {
                    "source": "SHOP",
                    "document_version_id": str(
                        UUID(bytes=hashlib.sha256(pdf_path.name.encode()).digest()[:16])
                    ),
                    "content_hash": f"sha256:{digest}",
                }
            ],
        },
    }

    (out / ANSWER_KEY).write_text(json.dumps(case, indent=2) + "\n", encoding="utf-8")
    (out / BAKEOFF_METADATA).write_text(
        json.dumps(
            {
                "frame": frame.model_dump(mode="json"),
                "tags": {k: v for k, v in sorted(tags.items())},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    # The bake-off renders from the PDF beside the answer key, so it has to be there.
    destination = out / pdf_path.name
    if not destination.exists():
        destination.write_bytes(pdf)

    # **Loaded back before this returns.** A key the bake-off would refuse must fail here, not after
    # somebody has spent an afternoon typing values into it. No frame is named: the key's own is the
    # one every later loader will use, so it is the one checked.
    try:
        crops = load_crops(out)
    except ModelBakeoffError as error:
        raise ScaffoldError(
            f"the bake-off refused the key this just wrote: {error}\n"
            "Nothing is wrong with what you typed — this is a fault in the scaffold."
        ) from error

    tagged = Counter(tag for crop in crops for tag in crop.tags)
    print(f"\n  {len(crops)} crops in {out / ANSWER_KEY}, and the bake-off loads them.")
    for tag in HARD_CASE_TAGS:
        print(f"    {tag:<12} {tagged.get(tag, 0):>4}")
    print(f"    {'unreadable':<12} {len(unreadable):>4}  (recorded, not scored)")
    print(f"    {'cut off':<12} {len(cut_off):>4}  (recorded, not scored)")
    print(f"    {'not a dim.':<12} {len(not_a_dimension):>4}  (recorded, not scored)")
    if not_a_single_value:
        print(
            f"    {'compound':<12} {len(not_a_single_value):>4}  "
            "(not one dimension — recorded, not scored)"
        )
    if empty:
        print(f"    {'not yet read':<12} {len(empty):>4}")
    if not tagged.get("rotated"):
        print(
            "\n  No crop is tagged rotated. That is the category two models read as 60 and as 4 on\n"
            "  the same image — if the set genuinely has none, say so; if nobody ticked the column,\n"
            "  the scorecard will not measure the case that matters most."
        )
    print()
    return 0


def _measurement_payload(value: object, raw: str) -> dict[str, str]:
    """The parsed value, authored as exact text the gold schema will accept.

    Taken from the parse rather than from what was typed, so `648 mm` is recorded as the inches the
    verdict works in — and rejected earlier if it could not be converted exactly.
    """
    exact = getattr(value, "exact", None)
    if exact is None:  # pragma: no cover - normalise_to_inches always returns a Measurement
        raise ScaffoldError(f"{raw!r} did not parse to a measurement")
    return {"exact": str(exact), "unit": "in", "raw_text": raw}


# ---------------------------------------------------------------------------
# #867: a dimension-first sample
# ---------------------------------------------------------------------------

#: The text reader's setting (#912): the share of the text's height at which a gap inside the
#: inches is a space the file left out. The frame and the look-again check both read the drawing's
#: printed text with the reader, so both read it from the settings file, the frame among the
#: reader's own settings (`_reader_settings`); it has no default.
MISSING_SPACE_SETTING: Final = "GV_READER_MISSING_SPACE_HEIGHTS"

#: Settings the dimension-first frame reads beyond the reader's own: the association's ambiguity
#: margin, which decides whether one dimension line is clearly the nearest, and the reading agent's
#: two label lengths, which decide where a label ends and so whether a crop cut it. Read from the
#: same file, like the rest; none has a default.
FRAME_SETTINGS: Final = (
    "GV_READER_AMBIGUITY_MARGIN",
    "GV_AGENT_LABEL_GAP_PT",
    "GV_AGENT_MAX_LABEL_PT",
)

#: Where a run agreed, on the drawing with this content hash. **Place only**: the query selects the
#: page, the polygon and the resolution it was recorded at, and no column that holds a reading. The
#: statuses are the ones a corroboration lane records an agreement with
#: (`eval/markup_yardstick._AGREED`): `RAW_CANDIDATE` with the lane named, or `CORROBORATED`.
AGREED_SQL: Final = """
    SELECT p.index, oc.polygon, er.dpi
      FROM observation_candidates oc
      JOIN extraction_runs er ON er.id = oc.extraction_run_id
      JOIN pages p ON p.id = oc.page_id
      JOIN document_versions dv ON dv.id = oc.document_version_id
     WHERE dv.sha256 = :sha256
       AND oc.corroboration_lane IS NOT NULL
       AND oc.corroboration_status IN ('RAW_CANDIDATE', 'CORROBORATED')
     ORDER BY p.index, oc.id
"""

#: The subtype of a reviewer's note box (`scripts/markup_yardstick.FREE_TEXT`).
_FREE_TEXT: Final = "FreeText"

type PointBox = tuple[Decimal, Decimal, Decimal, Decimal]
"""left, bottom, right, top, in PDF points: the space a drawing's paths and characters are in."""


def _frame_settings(path: Path) -> dict[str, str]:
    """The reader's settings and the frame's own, from one `scripts/demo.sh`-style file."""
    settings = _reader_settings(path)
    text = path.read_text(encoding="utf-8")
    for name in FRAME_SETTINGS:
        match = re.search(rf"^\s*{name}=(\S+)", text, flags=re.MULTILINE)
        if match is None:
            raise ScaffoldError(f"{path} does not state {name}, and it has no default")
        settings[name] = match.group(1).rstrip("\\").strip()
    return settings


def _missing_space(reader: Mapping[str, str]) -> MissingSpace:
    """The text reader's missing-space setting, as the settings file states it (#912)."""
    raw = reader[MISSING_SPACE_SETTING]
    try:
        return MissingSpace(gap_heights=Decimal(raw))
    except (ArithmeticError, TypeError, ValueError) as error:
        raise ScaffoldError(f"{MISSING_SPACE_SETTING}={raw} is not usable: {error}") from error


def _stated_missing_space(path: Path) -> MissingSpace:
    """`_missing_space`, read from a `scripts/demo.sh`-style file on its own: all the look-again
    check needs, because it reads the drawing's printed text and nothing else."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ScaffoldError(f"could not read the reader settings in {path}: {error}") from error
    match = re.search(rf"^\s*{MISSING_SPACE_SETTING}=(\S+)", text, flags=re.MULTILINE)
    if match is None:
        raise ScaffoldError(f"{path} does not state {MISSING_SPACE_SETTING}, and it has no default")
    return _missing_space({MISSING_SPACE_SETTING: match.group(1).rstrip("\\").strip()})


def _point_box(path: VectorPath) -> PointBox:
    xs = [x for x, _ in path.points]
    ys = [y for _, y in path.points]
    return (min(xs), min(ys), max(xs), max(ys))


def _point_overlap(first: PointBox, second: PointBox) -> bool:
    return (
        first[0] <= second[2]
        and second[0] <= first[2]
        and first[1] <= second[3]
        and second[1] <= first[3]
    )


def _character(box: PointBox) -> VectorPath:
    """A printed character as the box it occupies, in the form the label geometry gathers.

    **Its place and size, never what it is.** The label geometry asks only where glyphs are, so a
    character's box stands in for a path; nothing reads which character it is.
    """
    left, bottom, right, top = box
    corners = ((left, bottom), (right, bottom), (right, top), (left, top))
    return VectorPath(
        segments=tuple(
            PathSegment(
                kind=SegmentKind.MOVE if number == 0 else SegmentKind.LINE,
                point=corner,
                closes=number == len(corners) - 1,
            )
            for number, corner in enumerate(corners)
        ),
        stroked=False,
        filled=True,
        stroke_colour=None,
        fill_colour=None,
    )


class _GlyphIndex:
    """A page's glyphs bucketed by where they are, so a place asks only about the ones near it.

    A speed-up and nothing else: `near` returns exactly the glyphs a scan of the whole page would
    find overlapping the box, in the page's own order.
    """

    def __init__(self, glyphs: Sequence[VectorPath], cell_pt: Decimal) -> None:
        self._cell = cell_pt
        self._glyphs = [glyph for glyph in glyphs if glyph.points]
        self._boxes = [_point_box(glyph) for glyph in self._glyphs]
        self._cells: dict[tuple[int, int], list[int]] = {}
        for number, box in enumerate(self._boxes):
            for cell in self._cells_of(box):
                self._cells.setdefault(cell, []).append(number)

    def _step(self, value: Decimal) -> int:
        return int((value / self._cell).to_integral_value(rounding=ROUND_FLOOR))

    def _cells_of(self, box: PointBox) -> list[tuple[int, int]]:
        return [
            (x, y)
            for x in range(self._step(box[0]), self._step(box[2]) + 1)
            for y in range(self._step(box[1]), self._step(box[3]) + 1)
        ]

    def near(self, box: PointBox) -> list[VectorPath]:
        found: set[int] = set()
        for cell in self._cells_of(box):
            found.update(self._cells.get(cell, ()))
        return [
            self._glyphs[number]
            for number in sorted(found)
            if _point_overlap(self._boxes[number], box)
        ]


@dataclass(frozen=True, slots=True)
class _FrameSettings:
    """Every setting the frame is built under, stated by the caller."""

    reach: LabelReach
    proximity_limit: Decimal
    ambiguity_margin: Decimal


@dataclass(frozen=True, slots=True)
class _Page:
    """One page as the frame reads it: geometry only."""

    page_index: int
    version_id: UUID
    transform: PageTransform
    page_px: tuple[int, int]
    glyphs: _GlyphIndex
    """The vendor's glyph-sized paths and its black printed characters, in PDF points."""
    fractions: tuple[StackedFraction, ...]
    lines: tuple[DimensionExtent, ...]
    coloured: tuple[PointBox, ...]
    """Where markup is drawn in colour inside the drawing: coloured text and coloured glyph paths."""

    def points(self, box: tuple[int, int, int, int]) -> PointBox:
        corners = [
            self.transform.to_pdf(ImagePoint(x, y)) for x, y in ((box[0], box[1]), (box[2], box[3]))
        ]
        return (
            min(corner.x for corner in corners),
            min(corner.y for corner in corners),
            max(corner.x for corner in corners),
            max(corner.y for corner in corners),
        )


def _pixel_box(points: Sequence[ImagePoint]) -> tuple[int, int, int, int]:
    return (
        min(point.x for point in points),
        min(point.y for point in points),
        max(point.x for point in points),
        max(point.y for point in points),
    )


def _in_reading_frame(box: PointBox, rotation: int) -> Glyph:
    """A glyph's box along and across the line it reads on (`key_frame.Glyph`)."""
    left, bottom, right, top = box
    if rotation in (90, 270):
        return (bottom, top, left, right)
    return (left, right, bottom, top)


def _one_nearest_line(page: _Page, box: PointBox, rotation: int, settings: _FrameSettings) -> bool:
    """Whether the stage's association rule attaches a label here to one dimension line.

    `text_association.associate` is asked about the label's place and direction alone — its
    `DimensionText` carries no number by design — under the stage's own proximity limit and
    ambiguity margin. A box too thin to be placed is attached to nothing.
    """
    try:
        polygon, _corners = page_box_polygon(box, page.transform, page.version_id, page.page_index)
        result = associate(
            (DimensionText(uuid4(), polygon, rotation),),
            page.lines,
            proximity_limit=settings.proximity_limit,
            ambiguity_margin=settings.ambiguity_margin,
        )
    except (ArithmeticError, TypeError, ValueError):
        return False
    return bool(result.associated)


def _place(
    page: _Page,
    box: tuple[int, int, int, int],
    source: Source,
    *,
    settings: _FrameSettings,
    rotation: int | None,
    stacked: bool,
    layout: Layout | None,
) -> Place:
    """One place and the geometry there, by the stage's own code wherever the stage has it.

    The label is gathered by `extraction.agent.geometry.label_geometry`, the rule the reading agent
    decides a cut and a direction by, from the vendor's paths and printed characters alike. A text
    run's direction is the one its reader read; any other place's is the gathered label's.
    """
    reach = settings.reach
    region = page.points(box)
    crop_px = _grown(box, margin_pt=VISION_CROP_CONTEXT_MARGIN_PT, page_px=page.page_px)
    crop = page.points(crop_px)
    around = reach.maximum_label_pt + max(reach.label_gap_pt, reach.glyph_gap_pt)
    nearby = page.glyphs.near(
        (region[0] - around, region[1] - around, region[2] + around, region[3] + around)
    )
    found = label_geometry(region, crop, nearby, reach)
    # **The label's glyphs are the ones its gathered box holds.** For a closed label that is exactly
    # the set `gather_label` gathered, because a glyph touching the box is within the label gap of
    # it and would have joined; gathering a second time would cost as much again on a busy sheet.
    label = found.label_box if found.label_box is not None else region
    members = (
        [glyph for glyph in nearby if _point_overlap(_point_box(glyph), label)]
        if found.label_box is not None
        else []
    )
    turned = found.rotation_degrees if rotation is None else rotation
    return Place(
        page_index=page.page_index,
        box=box,
        sources=frozenset({source}),
        geometry=Geometry(
            glyphs=tuple(_in_reading_frame(_point_box(member), turned) for member in members),
            closed=found.closed,
            one_nearest_line=_one_nearest_line(page, label, turned, settings),
            sideways=turned != 0,
            stacked=stacked or crop_shows_a_stacked_fraction(crop_px, page.fractions),
            cut=found.cut_at_edge,
            gv_mark=any(_point_overlap(mark, crop) for mark in page.coloured),
            layout=layout,
        ),
    )


def _characters(flattened: bytes, page_index: int) -> tuple[list[PointBox], list[PointBox]]:
    """The boxes of the pasted drawings' printed characters: in black or grey, and in colour.

    In the page's own PDF points, where the vendor's paths are: pdfplumber gives `x` there already
    and `y` from the media box's bottom edge, so that edge is added back. A character that draws
    nothing — a space — is left out, because it has no ink to be a glyph.
    """
    import pdfplumber

    black: list[PointBox] = []
    coloured: list[PointBox] = []
    with pdfplumber.open(io.BytesIO(flattened)) as document:
        page = document.pages[page_index]
        bottom = Decimal(str(page.page_obj.mediabox[1]))
        for char in page.chars:
            if not str(char.get("text", "")).strip():
                continue
            box = (
                Decimal(str(char["x0"])),
                Decimal(str(char["y0"])) + bottom,
                Decimal(str(char["x1"])),
                Decimal(str(char["y1"])) + bottom,
            )
            (black if drawing_ink(char) else coloured).append(box)
    return black, coloured


@dataclass(frozen=True, slots=True)
class _PageFrame:
    """What one page adds to the frame."""

    places: list[Place]
    sites: list[Site]


def _page_frame(
    pdf: bytes,
    page_index: int,
    *,
    transform: PageTransform,
    reader: Mapping[str, str],
    settings: _FrameSettings,
    agreed: Sequence[tuple[int, int, int, int]],
) -> _PageFrame:
    """Every place one page offers, and GV's sites on it.

    Read by the stage's own readers at `VISION_CROP_DPI`, the frame the key's polygons are in: the
    vendor's layer (`read_annotation_layers`), the planned regions (`plan_reads`), the dimension
    lines (`detect`), the printed text inside the pasted drawings (`read_stamp_text`, whose runs are
    taken by place, direction and layout, never by what they say), and the reviewer's notes.
    """
    version_id = uuid4()
    layers = read_annotation_layers(
        pdf,
        page_index,
        document_version_id=version_id,
        dpi=VISION_CROP_DPI,
        line_minimum_pt=Decimal(reader["GV_READER_LINE_MINIMUM_PT"]),
        glyph_maximum_pt=Decimal(reader["GV_READER_GLYPH_MAXIMUM_PT"]),
        glyph_gap_pt=Decimal(reader["GV_READER_GLYPH_GAP_PT"]),
        fraction_bar=_fraction_bar(reader),
    )
    plan = plan_reads(
        layers,
        proximity_limit=Decimal(reader["GV_READER_PROXIMITY_LIMIT"]),
        minimum_paths=int(reader["GV_READER_LOCALIZED_MINIMUM_PATHS"]),
        maximum_span=Decimal(reader["GV_READER_LOCALIZED_MAXIMUM_SPAN"]),
    )
    detected = detect(
        layers.drawing_segments,
        witness_tolerance=Decimal(reader["GV_READER_WITNESS_TOLERANCE"]),
        minimum_span=Decimal(reader["GV_READER_MINIMUM_SPAN"]),
        straightness=Decimal(reader["GV_READER_STRAIGHTNESS"]),
        crossing_margin=Decimal(reader["GV_READER_CROSSING_MARGIN"]),
    )
    flattened = stamps_only(pdf, page_index)
    black, coloured_characters = _characters(flattened, page_index)
    reach = settings.reach
    page = _Page(
        page_index=page_index,
        version_id=version_id,
        transform=transform,
        page_px=_page_px(pdf, page_index),
        glyphs=_GlyphIndex(
            [*layers.glyph_paths, *(_character(box) for box in black)],
            cell_pt=reach.maximum_label_pt + max(reach.label_gap_pt, reach.glyph_gap_pt),
        ),
        fractions=layers.stacked_fractions,
        lines=tuple(line.extent for line in detected.lines),
        coloured=(
            *coloured_characters,
            *(
                _point_box(path)
                for path in layers.glyph_paths
                if path.points and not path.drawing_ink
            ),
        ),
    )

    places: list[Place] = []
    for entry in plan.to_read:
        region = entry.region
        places.append(
            _place(
                page,
                _pixel_box(region.image_extent),
                Source.PATH_LABEL,
                settings=settings,
                rotation=None,
                stacked=region.stacked_glyphs,
                layout=None,
            )
        )
    missing_space = _missing_space(reader)
    printed = read_stamp_text(
        pdf,
        page_index,
        document_version_id=version_id,
        dpi=VISION_CROP_DPI,
        missing_space=missing_space,
    ).contents
    for item in printed.texts:
        places.append(
            _place(
                page,
                _pixel_box(item.image_extent),
                Source.PRINTED_TEXT,
                settings=settings,
                rotation=item.rotation_degrees,
                stacked=item.stacked,
                layout=Layout.STACKED_FRACTION if item.stacked else None,
            )
        )
    layouts = {
        SetAsideReason.STACKED_FRACTION: Layout.STACKED_FRACTION,
        SetAsideReason.TWO_LINES: Layout.TWO_LINES,
        SetAsideReason.FRAGMENT: Layout.FRAGMENT,
        SetAsideReason.MISSING_SPACE: Layout.MISSING_SPACE,
    }
    for label in printed.set_aside:
        places.append(
            _place(
                page,
                _pixel_box(label.image_extent),
                Source.PRINTED_TEXT,
                settings=settings,
                rotation=None,
                stacked=label.reason is SetAsideReason.STACKED_FRACTION,
                layout=layouts[label.reason],
            )
        )
    for box in agreed:
        places.append(
            _place(
                page,
                box,
                Source.AGREED,
                settings=settings,
                rotation=None,
                stacked=False,
                layout=None,
            )
        )
    return _PageFrame(
        places=places, sites=_sites(pdf, flattened, page_index, version_id, missing_space)
    )


def _sites(
    pdf: bytes, flattened: bytes, page_index: int, version_id: UUID, missing_space: MissingSpace
) -> list[Site]:
    """Where GV's reviewer corrected a dimension on this page (#850's sites).

    Two ways it is drawn: a note box over the vendor's drawing, and — on a drawing snapped after it
    was marked up — coloured text inside the drawing itself (`extraction/stamp_text.py`). Each is a
    site where #850's rule finds a dimension in GV's own words. **GV's text says where to look,
    never what the vendor's label says.**
    """
    found: list[Site] = []
    notes = read_markup_layer(pdf, page_index, document_version_id=version_id, dpi=VISION_CROP_DPI)
    for note in notes.markup:
        if note.subtype == _FREE_TEXT and _holds_a_dimension(note.text, page_index):
            found.append(Site(page_index, _pixel_box(note.image_extent)))
    corrections = read_page_contents(
        flattened,
        page_index,
        document_version_id=version_id,
        dpi=VISION_CROP_DPI,
        missing_space=missing_space,
        keep_char=lambda char: not drawing_ink(char),
    )
    for item in corrections.texts:
        if _holds_a_dimension(item.text, page_index):
            found.append(Site(page_index, _pixel_box(item.image_extent)))
    return found


def _holds_a_dimension(text: str, page_index: int) -> bool:
    return (
        site_of(GvNote(page_index, text, (Decimal(0), Decimal(0), Decimal(1), Decimal(1))))
        is not None
    )


def _fraction_bar(reader: Mapping[str, str]) -> FractionBarGeometry:
    return FractionBarGeometry(
        bar_thickness_max_pt=Decimal(reader["GV_READER_FRACTION_BAR_THICKNESS_MAX_PT"]),
        bar_length_min_pt=Decimal(reader["GV_READER_FRACTION_BAR_LENGTH_MIN_PT"]),
        reach_pt=Decimal(reader["GV_READER_FRACTION_REACH_PT"]),
        glyph_min_pt=Decimal(reader["GV_READER_FRACTION_GLYPH_MIN_PT"]),
        glyph_max_pt=Decimal(reader["GV_READER_FRACTION_GLYPH_MAX_PT"]),
        proportion_max=Decimal(reader["GV_READER_FRACTION_PROPORTION_MAX"]),
        character_gap_pt=Decimal(reader["GV_READER_FRACTION_CHARACTER_GAP_PT"]),
        turned_aspect_min=Decimal(reader["GV_READER_FRACTION_TURNED_ASPECT_MIN"]),
    )


def agreed_boxes(
    rows: Sequence[tuple[int, Sequence[Sequence[int]], int]],
) -> dict[int, list[tuple[int, int, int, int]]]:
    """Each agreed region's box, by page, in pixels at `VISION_CROP_DPI`.

    `rows` are the page index, the polygon and the resolution it was recorded at, as `AGREED_SQL`
    returns them: where a run agreed, and nothing it agreed on. The box is rounded outward, so it
    holds every recorded pixel.
    """
    boxes: dict[int, list[tuple[int, int, int, int]]] = {}
    for page_index, polygon, dpi in rows:
        if not polygon or isinstance(dpi, bool) or not isinstance(dpi, int) or dpi <= 0:
            raise ScaffoldError("an agreed region was recorded without a polygon or a resolution")
        scale = Fraction(VISION_CROP_DPI, dpi)
        xs = [Fraction(int(point[0])) * scale for point in polygon]
        ys = [Fraction(int(point[1])) * scale for point in polygon]
        box = (
            int(min(xs) // 1),
            int(min(ys) // 1),
            -int(-max(xs) // 1),
            -int(-max(ys) // 1),
        )
        if box[2] > box[0] and box[3] > box[1]:
            boxes.setdefault(int(page_index), []).append(box)
    return boxes


def _agreed_from_database(url: str, sha256: str) -> dict[int, list[tuple[int, int, int, int]]]:
    """The agreed regions a run recorded on this drawing, read inside a read-only transaction."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import SQLAlchemyError

    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            # First in the transaction, so nothing after it can write.
            connection.execute(text("SET TRANSACTION READ ONLY"))
            rows = connection.execute(text(AGREED_SQL), {"sha256": sha256}).all()
    except SQLAlchemyError as error:
        raise ScaffoldError(
            f"the run's database could not be read: {type(error).__name__}"
        ) from error
    finally:
        engine.dispose()
    return agreed_boxes([(int(page), polygon, int(dpi)) for page, polygon, dpi in rows])


def _read_pages(
    pdf: bytes,
    jobs: Sequence[tuple[int, PageTransform]],
    *,
    reader: Mapping[str, str],
    settings: _FrameSettings,
    agreed: Mapping[int, Sequence[tuple[int, int, int, int]]],
    workers: int,
) -> list[tuple[int, _PageFrame | Exception]]:
    """Each page's frame, or why it could not be read, in page order.

    **Read in as many processes as `workers` asks, and put together in page order**, so the frame is
    the same however many there are: one page of a dense sheet takes minutes. One unreadable page
    is reported and does not lose the rest.
    """
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ScaffoldError("--workers must be a whole number, one or more")

    def one(page_index: int, transform: PageTransform) -> _PageFrame:
        return _page_frame(
            pdf,
            page_index,
            transform=transform,
            reader=reader,
            settings=settings,
            agreed=agreed.get(page_index, ()),
        )

    found: list[tuple[int, _PageFrame | Exception]] = []
    if workers == 1:
        for page_index, transform in jobs:
            try:
                found.append((page_index, one(page_index, transform)))
            except Exception as error:  # noqa: BLE001 - one unreadable page must not lose the rest
                found.append((page_index, error))
        return found
    with ProcessPoolExecutor(max_workers=workers) as pool:
        pending: list[tuple[int, Future[_PageFrame]]] = [
            (
                page_index,
                pool.submit(
                    _page_frame,
                    pdf,
                    page_index,
                    transform=transform,
                    reader=reader,
                    settings=settings,
                    agreed=agreed.get(page_index, ()),
                ),
            )
            for page_index, transform in jobs
        ]
        for page_index, future in pending:
            try:
                found.append((page_index, future.result()))
            except Exception as error:  # noqa: BLE001 - one unreadable page must not lose the rest
                found.append((page_index, error))
    return found


def _quotas(given: Sequence[str]) -> dict[Kind, int]:
    """`kind=N` for every kind, each stated once; none has a default."""
    quotas: dict[Kind, int] = {}
    for entry in given:
        name, equals, count = entry.partition("=")
        try:
            kind = Kind(name.strip())
            number = int(count)
        except ValueError as error:
            raise ScaffoldError(
                f"{entry!r} is not KIND=N; the kinds are {', '.join(kind.value for kind in Kind)}"
            ) from error
        if not equals or number < 0 or kind in quotas:
            raise ScaffoldError(f"{entry!r}: give each kind once, as a whole number zero or more")
        quotas[kind] = number
    missing = [kind.value for kind in Kind if kind not in quotas]
    if missing:
        raise ScaffoldError(f"every kind needs a stated --quota, and {missing} have none")
    return quotas


def _under_data(out: Path) -> None:
    if "data" not in out.resolve().parts:
        raise ScaffoldError(
            f"{out} is not under data/: the crops are the client's drawing, and data/ is the one "
            "place in the repository git never takes them from"
        )


def sample_command(arguments: argparse.Namespace) -> int:
    """Build the dimension-first frame on the pages, draw each kind's quota, and write the sheet."""
    out = Path(arguments.out)
    _under_data(out)
    pdf_path = Path(arguments.pdf)
    try:
        pdf = pdf_path.read_bytes()
    except OSError as error:
        raise ScaffoldError(f"could not read {pdf_path}: {error}") from error
    reader = _frame_settings(Path(arguments.reader_settings))
    try:
        shape = LabelShape(arguments.min_glyphs, arguments.max_glyphs, arguments.height_ratio)
    except (TypeError, ValueError) as error:
        raise ScaffoldError(str(error)) from error
    quotas = _quotas(arguments.quota)
    settings = _FrameSettings(
        reach=LabelReach(
            Decimal(reader["GV_AGENT_LABEL_GAP_PT"]),
            Decimal(reader["GV_AGENT_MAX_LABEL_PT"]),
            Decimal(reader["GV_READER_GLYPH_GAP_PT"]),
        ),
        proximity_limit=Decimal(reader["GV_READER_PROXIMITY_LIMIT"]),
        ambiguity_margin=Decimal(reader["GV_READER_AMBIGUITY_MARGIN"]),
    )
    agreed = (
        _agreed_from_database(arguments.database, hashlib.sha256(pdf).hexdigest())
        if arguments.database
        else {}
    )
    pages = {page.index: page for page in read_pages(pdf)}

    if isinstance(arguments.workers, bool) or arguments.workers < 1:
        raise ScaffoldError("--workers must be a whole number, one or more")
    jobs: list[tuple[int, PageTransform]] = []
    for page_index in _page_indexes(arguments.pages):
        raw = pages.get(page_index)
        if raw is None or raw.media_box is None or raw.crop_box is None:
            print(f"  page {page_index + 1}: skipped (not in the drawing, or no page boxes)")
            continue
        jobs.append(
            (
                page_index,
                PageTransform(
                    dpi=VISION_CROP_DPI,
                    rotation=raw.rotation,
                    media_box=raw.media_box,
                    crop_box=raw.crop_box,
                ),
            )
        )
    places: list[Place] = []
    sites: list[Site] = []
    for page_index, found in _read_pages(
        pdf, jobs, reader=reader, settings=settings, agreed=agreed, workers=arguments.workers
    ):
        if isinstance(found, Exception):
            print(f"  page {page_index + 1}: skipped ({type(found).__name__}: {found})")
            continue
        places += found.places
        sites += found.sites

    built = frame(places, sites, shape=shape, site_reach_px=arguments.site_reach_px)
    if not built.places:
        raise ScaffoldError("the frame holds no place on those pages; nothing can be sampled")
    drawn = sample(
        built.places,
        quotas=quotas,
        shape=shape,
        small_px=arguments.small_px,
        seed=arguments.seed,
    )
    out.mkdir(parents=True, exist_ok=True)
    rows = [
        SheetRow(
            crop_id=f"{arguments.prefix}-p{entry.place.page_index + 1}-{number:03d}",
            page=entry.place.page_index + 1,
            region=entry.place.box,
            crop=_grown(
                entry.place.box,
                margin_pt=VISION_CROP_CONTEXT_MARGIN_PT,
                page_px=_page_px(pdf, entry.place.page_index),
            ),
            stratum=entry.kind.value,
            extra=(" + ".join(sorted(source.value for source in entry.place.sources)),),
        )
        for number, entry in enumerate(drawn.drawn, start=1)
    ]
    written, unrenderable = _render_crops(pdf, out, rows)
    if not written:
        raise ScaffoldError("none of the sampled places could be rendered as a crop")
    _write_sheet(out, written, extra_columns=("sources",))
    _write_contact_sheet(
        out,
        [
            (
                row.crop_id,
                (
                    f"p{row.page} · label {row.region[2] - row.region[0]}x"
                    f"{row.region[3] - row.region[1]}px"
                ),
            )
            for row in written
        ],
    )
    (out / HOW_TO).write_text(_HOW_TO_TEXT, encoding="utf-8")
    (out / BAKEOFF_METADATA).write_text(
        json.dumps({"frame": _FRAME.model_dump(mode="json"), "tags": {}}, indent=2) + "\n",
        encoding="utf-8",
    )
    kinds_written = Counter(row.stratum for row in written)
    sources_kept = Counter(source.value for place in built.places for source in place.sources)
    # **What the frame was built under, and what it held**: counts and settings, no text and no
    # value, so the record can be quoted where the drawing cannot.
    record = {
        "drawing_sha256": hashlib.sha256(pdf).hexdigest(),
        "pages": arguments.pages,
        "reader_settings": str(arguments.reader_settings),
        "label_shape": {
            "minimum_glyphs": shape.minimum_glyphs,
            "maximum_glyphs": shape.maximum_glyphs,
            "height_ratio": str(shape.height_ratio),
        },
        "small_px": arguments.small_px,
        "site_reach_px": arguments.site_reach_px,
        "seed": arguments.seed,
        "agreed_regions_read": arguments.database is not None,
        "places_found": built.found,
        "places_not_label_shaped": built.not_label_shaped,
        "places_in_frame": len(built.places),
        "places_by_source": dict(sorted(sources_kept.items())),
        "gv_sites": len(sites),
        "gv_sites_without_a_place": built.sites_without_a_place,
        "quotas": {kind.value: quotas[kind] for kind in Kind},
        "available": {kind.value: drawn.available[kind] for kind in Kind},
        "written": {kind.value: kinds_written[kind.value] for kind in Kind},
        "unrenderable": unrenderable,
    }
    (out / FRAME_RECORD).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    print(f"\n  {len(written)} crops written to {out}")
    print(f"  frame: {len(built.places)} places from {built.found} found; by source:")
    for source, count in sorted(sources_kept.items()):
        print(f"    {source:<16} {count:>5}")
    print(f"  GV sites: {len(sites)}, {built.sites_without_a_place} with no place at them")
    print("\n  kind          drawn   quota   in the frame")
    for kind in Kind:
        print(
            f"    {kind.value:<12} {kinds_written[kind.value]:>5}   {quotas[kind]:>5}   "
            f"{drawn.available[kind]:>5}"
        )
    if unrenderable:
        print(f"\n  {unrenderable} sampled crop(s) could not be rendered and were dropped")
    print(f"\n  Contact sheet: {out / CONTACT_SHEET}\n  Next: open {out / HOW_TO}\n")
    return 0


def _share(text: str) -> Fraction:
    try:
        share = Fraction(Decimal(text))
    except (InvalidOperation, ValueError) as error:
        raise ScaffoldError(f"{text!r} is not a share between 0 and 1") from error
    if not 0 <= share <= 1:
        raise ScaffoldError(f"{text!r} is not a share between 0 and 1")
    return share


def triage_command(arguments: argparse.Namespace) -> int:
    """Per drawing, how many crops are dimensions, from the `not_a_dimension` marks a person made.

    Exit status 1 when any drawing is below `--minimum-share`, so a sample that is mostly symbols
    stops here rather than after a hundred crops of reading.
    """
    minimum = _share(arguments.minimum_share)
    below = False
    print(f"\n  {'drawing':<40} {'crops':>6} {'not dim.':>9} {'share':>7}")
    for directory in arguments.keys:
        folder = Path(directory)
        sheet = folder / arguments.answers
        try:
            with sheet.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except OSError as error:
            raise ScaffoldError(f"could not read {sheet}: {error}") from error
        if rows and "not_a_dimension" not in rows[0]:
            raise ScaffoldError(f"{sheet} has no `not_a_dimension` column to count")
        counted = triage(folder.name, (_ticked(row, "not_a_dimension") for row in rows))
        share = counted.share
        shown = "-" if share is None else f"{float(share) * 100:.1f}%"
        verdict = "" if counted.meets(minimum) else "  below the minimum"
        below = below or not counted.meets(minimum)
        print(
            f"  {counted.drawing:<40} {counted.crops:>6} {counted.not_a_dimension:>9} "
            f"{shown:>7}{verdict}"
        )
    print(f"\n  minimum share of dimensions: {float(minimum) * 100:.1f}%\n")
    return 1 if below else 0


def _printed_value(text: str) -> Fraction | None:
    """The one exact value a printed run holds, in inches, or `None` where it holds none."""
    if is_compound(text):
        return None
    try:
        return normalise_to_inches(canonical_notation(text)[0]).exact
    except UnitNormalisationError:
        return None


def _centre_in(points: Sequence[ImagePoint], crop: tuple[int, int, int, int]) -> bool:
    left, top, right, bottom = _pixel_box(points)
    return 2 * crop[0] <= left + right <= 2 * crop[2] and 2 * crop[1] <= top + bottom <= 2 * crop[3]


def _witnessed(
    pdf: bytes,
    page_index: int,
    dpi: int,
    crops: Mapping[str, tuple[int, int, int, int]],
    missing_space: MissingSpace,
) -> dict[str, Witnessed]:
    """What the file itself prints inside each crop on one page: black numbers and GV's coloured ones."""
    version_id = uuid4()
    black = [
        *read_stamp_text(
            pdf, page_index, document_version_id=version_id, dpi=dpi, missing_space=missing_space
        ).contents.texts,
        *read_page_contents(
            pdf,
            page_index,
            document_version_id=version_id,
            dpi=dpi,
            missing_space=missing_space,
            keep_char=drawing_ink,
        ).texts,
    ]
    coloured = read_page_contents(
        stamps_only(pdf, page_index),
        page_index,
        document_version_id=version_id,
        dpi=dpi,
        missing_space=missing_space,
        keep_char=lambda char: not drawing_ink(char),
    ).texts
    seen: dict[str, Witnessed] = {}
    for crop_id, crop in crops.items():
        printed = {
            value
            for item in black
            if _centre_in(item.image_extent, crop)
            and (value := _printed_value(item.text)) is not None
        }
        gv = {
            found.gv_value
            for item in coloured
            if _centre_in(item.image_extent, crop)
            and (
                found := site_of(
                    GvNote(page_index, item.text, (Decimal(0), Decimal(0), Decimal(1), Decimal(1)))
                )
            )
            is not None
            and found.gv_value is not None
        }
        seen[crop_id] = Witnessed(crop_id, frozenset(printed), frozenset(gv))
    return seen


def check_command(arguments: argparse.Namespace) -> int:
    """The typist's check: a blind "look again" list, by crop, never by value (#867).

    Prints which crops to look at again and which of the file's texts differs there — the drawing's
    own printed text, or GV's — and nothing of what either says, nor of what was typed.
    """
    out = Path(arguments.out)
    rows = _rows(out)
    keyframe = _sheet_frame(out)
    pdf_path = Path(arguments.pdf)
    try:
        pdf = pdf_path.read_bytes()
    except OSError as error:
        raise ScaffoldError(f"could not read {pdf_path}: {error}") from error
    missing_space = _stated_missing_space(Path(arguments.reader_settings))
    typed: list[Typed] = []
    by_page: dict[int, dict[str, tuple[int, int, int, int]]] = {}
    for row in rows:
        crop_id = (row.get("crop_id") or "").strip()
        vendor = _vendor_text(row)
        value = _parsed(vendor, crop_id=crop_id) if vendor else None
        typed.append(
            Typed(
                crop_id=crop_id,
                vendor_value=None if value is None else value.exact,
                gv_value_seen=_gv_number(row.get("gv_value_seen") or _BLANK, crop_id=crop_id),
            )
        )
        try:
            page = int(row["page"]) - 1
            box = tuple(int(row[name]) for name in ("left_px", "top_px", "right_px", "bottom_px"))
        except (KeyError, ValueError) as error:
            raise ScaffoldError(
                f"{crop_id}: the page and polygon columns are unreadable"
            ) from error
        by_page.setdefault(page, {})[crop_id] = (box[0], box[1], box[2], box[3])
    witnessed: dict[str, Witnessed] = {}
    for page, crops in sorted(by_page.items()):
        witnessed.update(_witnessed(pdf, page, keyframe.polygon_dpi, crops, missing_space))
    listed: tuple[LookAgain, ...] = look_again(typed, witnessed)
    if not listed:
        print("\n  Nothing to look at again: what was typed matches the file wherever it prints.\n")
        return 0
    print(
        f"\n  Look again at these {len(listed)} crop(s). Nothing here says what either text says.\n"
    )
    for entry in listed:
        print(f"    {entry.crop_id}   differs from {entry.differs.value}")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    make = subcommands.add_parser("scaffold", help="render crops and an empty sheet")
    make.add_argument("pdf", help="the drawing to take crops from; it is not modified")
    make.add_argument("--pages", required=True, help="one-based, e.g. 3 or 3-5 or 1,4,9")
    make.add_argument("--out", required=True, help="output directory; must be under data/")
    make.add_argument("--count", type=int, default=50, help="how many crops (default: 50)")
    make.add_argument("--seed", type=int, default=0, help="sampling seed; a re-run repeats the set")
    make.add_argument(
        "--reader-settings",
        required=True,
        help=(
            "the settings file the worker is started with, e.g. scripts/demo.sh: every GV_READER_* "
            "threshold is read from it, and none has a default"
        ),
    )
    make.set_defaults(handler=scaffold)

    draw = subcommands.add_parser("sample", help="a dimension-first sample, kind by kind (#867)")
    draw.add_argument("pdf", help="the drawing to take crops from; it is not modified")
    draw.add_argument("--pages", required=True, help="one-based, e.g. 3 or 3-5 or 1,4,9")
    draw.add_argument("--out", required=True, help="output directory; must be under data/")
    draw.add_argument(
        "--reader-settings",
        required=True,
        help=(
            "the settings file the worker is started with, e.g. scripts/demo.sh: every reader, "
            "association and label-gathering setting is read from it, and none has a default"
        ),
    )
    draw.add_argument("--prefix", required=True, help="what each crop id starts with, e.g. s2")
    draw.add_argument(
        "--min-glyphs", type=int, required=True, help="fewest glyphs a label-shaped region holds"
    )
    draw.add_argument(
        "--max-glyphs", type=int, required=True, help="most glyphs a label-shaped region holds"
    )
    draw.add_argument(
        "--height-ratio",
        type=Decimal,
        required=True,
        help="how many times taller one glyph may be than another and the two be of one height",
    )
    draw.add_argument(
        "--small-px",
        type=int,
        required=True,
        help=f"longest side, in pixels at {VISION_CROP_DPI} dpi, below which a label is small",
    )
    draw.add_argument(
        "--site-reach-px",
        type=int,
        required=True,
        help=f"how near, in pixels at {VISION_CROP_DPI} dpi, a place must be to a GV site to be at it",
    )
    draw.add_argument(
        "--quota",
        action="append",
        default=[],
        metavar="KIND=N",
        help=f"how many of each kind to draw; every kind is required: {', '.join(Kind)}",
    )
    draw.add_argument("--seed", type=int, default=0, help="sampling seed; a re-run repeats the set")
    draw.add_argument(
        "--workers",
        type=int,
        default=1,
        help="how many pages to read at once, each in its own process; the sample is the same",
    )
    draw.add_argument(
        "--database",
        help="a run's database URL, read inside a read-only transaction for where it agreed",
    )
    draw.set_defaults(handler=sample_command)

    count = subcommands.add_parser("triage", help="per drawing, how many crops are dimensions")
    count.add_argument("keys", nargs="+", help="one sampled directory per drawing")
    count.add_argument(
        "--minimum-share",
        required=True,
        help="the least share of crops that must be dimensions, e.g. 0.6",
    )
    count.add_argument(
        "--answers",
        default=CROPS_CSV,
        help=f"the sheet holding the `not_a_dimension` marks, in each directory (default {CROPS_CSV})",
    )
    count.set_defaults(handler=triage_command)

    look = subcommands.add_parser("check", help="a blind look-again list for the typist")
    look.add_argument("out", help="the sampled directory, once values are typed")
    look.add_argument("--pdf", required=True, help="the same drawing the crops were cut from")
    look.add_argument(
        "--reader-settings",
        required=True,
        help=(
            "the settings file the worker is started with, e.g. scripts/demo.sh: the drawing's "
            f"printed text is read with its {MISSING_SPACE_SETTING}, which has no default"
        ),
    )
    look.set_defaults(handler=check_command)

    finish = subcommands.add_parser("build", help="turn the filled sheet into an answer key")
    finish.add_argument("out", help="the directory scaffold wrote")
    finish.add_argument("--pdf", required=True, help="the same drawing scaffold read")
    finish.add_argument("--annotator", required=True, help="who read the crops")
    finish.add_argument("--on", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    finish.add_argument("--case-id", default="reading-answer-key", help="gold case id")
    finish.set_defaults(handler=build)

    arguments = parser.parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except ScaffoldError as error:
        print(f"\n  {error}\n", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
