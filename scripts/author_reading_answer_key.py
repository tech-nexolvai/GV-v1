"""Prepare a crop set so that authoring a reading answer key is reading, not building (#689).

**This script never authors a value.** It produces the crops and the empty columns; a person reads
each crop and types what it says. That division is the point: #666 is admin-owned because the answer
key is only worth having if a human established it, and a key our own pipeline agreed with measures
agreement with ourselves. `eval/experiments/model_bakeoff.py` refuses such a key outright.

Two steps.

    scaffold  DRAWING.pdf --pages 3-5 --out data/goldset/reading-key/ --count 50 \\
              --reader-settings scripts/demo.sh
              -> numbered PNGs, a wide view of each, and crops.csv with an empty `value` column

    build     data/goldset/reading-key/ --pdf DRAWING.pdf --annotator "..." --on 2026-09-27
              -> answer_key.json + model_bakeoff_metadata.json, verified by loading them back

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
import json
import random
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

import pypdfium2 as pdfium  # type: ignore[import-untyped]

from eval.experiments.model_bakeoff import (
    HARD_CASE_TAGS,
    KeyFrame,
    ModelBakeoffError,
    key_frame,
    load_crops,
    render_crop,
)
from evidence.crop import POINTS_PER_INCH, decode_rgb_png, encode_png
from extraction.annotations import read_annotation_layers
from extraction.geometry.dimension_lines import detect
from extraction.geometry.text_association import lines_within
from extraction.glyph_bands import FractionBarGeometry
from extraction.rasterise import VISION_CROP_DPI
from extraction.vector_first import plan_reads
from units.normalise import UnitNormalisationError, normalise_to_inches
from units.notation import canonical_notation, is_compound
from workflow.stages import VISION_CROP_CONTEXT_MARGIN_PT

CROPS_CSV: Final = "crops.csv"
ANSWER_KEY: Final = "answer_key.json"
BAKEOFF_METADATA: Final = "model_bakeoff_metadata.json"
HOW_TO: Final = "HOW_TO_READ_THESE.md"
CONTACT_SHEET: Final = "contact_sheet.html"

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


def _wide_name(candidate: Candidate) -> str:
    return f"{candidate.crop_id}_wide.png"


def _wide_view(
    pdf: bytes, candidate: Candidate, *, crop_size: tuple[int, int], page_px: tuple[int, int]
) -> bytes:
    """The region with `WIDE_VIEW_PT` of drawing round it, and the crop's edge drawn in red.

    Rendered as the crop is, from the vendor's layer only. The line runs on the pixels just outside
    the crop, so it covers nothing a model is shown; `crop_size` is the crop as rendered, which can
    be a pixel short of its polygon.
    """
    wide = _grown(candidate.region, margin_pt=WIDE_VIEW_PT, page_px=page_px)
    width, height, rgb = decode_rgb_png(
        render_crop(
            pdf,
            page=candidate.page,
            polygon=wide,
            polygon_dpi=VISION_CROP_DPI,
            output_dpi=VISION_CROP_DPI,
        )
    )
    pixels = bytearray(rgb)
    left = candidate.crop[0] - wide[0]
    top = candidate.crop[1] - wide[1]
    right = left + crop_size[0]
    bottom = top + crop_size[1]
    edge = [(x, y) for x in range(left - 1, right + 1) for y in (top - 1, bottom)]
    edge += [(x, y) for y in range(top, bottom) for x in (left - 1, right)]
    for x, y in edge:
        if 0 <= x < width and 0 <= y < height:
            offset = (y * width + x) * 3
            pixels[offset : offset + 3] = _EDGE
    return encode_png(width, height, bytes(pixels))


def _write_sheet(out: Path, candidates: list[Candidate]) -> None:
    """The sheet a person fills in. Two empty columns and nothing else to decide.

    The four box columns are the crop, in pixels at `VISION_CROP_DPI` — the polygon `build` records.
    """
    with (out / CROPS_CSV).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
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
                "near_dimension_lines",
                "path_count",
                "value",
                "unreadable",
                "not_a_single_value",
                "rotated",
                "note",
            ]
        )
        for candidate in candidates:
            writer.writerow(
                [
                    candidate.crop_id,
                    f"{candidate.crop_id}.png",
                    _wide_name(candidate),
                    candidate.page,
                    *candidate.crop,
                    candidate.crop[2] - candidate.crop[0],
                    candidate.crop[3] - candidate.crop[1],
                    candidate.stratum,
                    candidate.line_count,
                    candidate.path_count,
                    "",
                    "",
                    "",
                    "",
                ]
            )


def _write_contact_sheet(out: Path, candidates: list[Candidate]) -> None:
    """A browseable sheet of the crops, each beside its wide view, before anyone types answers."""
    cards = []
    for candidate in candidates:
        crop_id = html.escape(candidate.crop_id)
        image = html.escape(f"{candidate.crop_id}.png")
        wide = html.escape(_wide_name(candidate))
        meta = html.escape(
            f"p{candidate.page} · {candidate.stratum} · "
            f"label {candidate.width_px}x{candidate.height_px}px · "
            f"{candidate.line_count} line(s)"
        )
        cards.append(
            f'<figure><img src="{image}" alt="{crop_id}">'
            f'<img src="{wide}" alt="{crop_id}, wide view"><figcaption>'
            f"<strong>{crop_id}</strong><br>{meta}</figcaption></figure>"
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
    document += "\n".join(cards)
    document += "\n</div>\n</html>\n"
    (out / CONTACT_SHEET).write_text(document, encoding="utf-8")


def _legible(candidates: list[Candidate]) -> tuple[list[Candidate], int]:
    """Drop specks before sampling, and report the count to the person running the scaffold."""
    kept = [candidate for candidate in candidates if candidate.long_axis_px >= MIN_LEGIBLE_AXIS_PX]
    return kept, len(candidates) - len(kept)


_HOW_TO_TEXT: Final = """# Reading these crops

Open each crop (`p3-r0012.png`), type what it says in the `value` column of `crops.csv`, and save.
That is the whole task. Nothing here has read them: every value in the file will be one you put
there. Each crop is exactly the picture the readers you are marking will be shown.

**Each crop has a wide view beside it** (`p3-r0012_wide.png`): the same place with more of the
drawing round it, and the crop's edge drawn in red. Use it to see whether the label carries on past
the red line. If it does, the crop has cut it off: put any character in `unreadable`, write `cut off`
in `note`, and leave `value` empty. Type a value only from what is inside the line. Both pictures
show the vendor's drawing alone; the reviewer's markup is left out of both, as it is for a model.

**Write it exactly as the drawing writes it, with its unit.**

    28 3/4"        yes
    648 mm         yes
    28.75"         no  — the verdict is exact match, so a rounded answer is a wrong answer
    28 3/4         no  — a value with no unit is refused

**If the crop is not one measurement, tick `not_a_single_value` instead.** Some dimensions are
written as an instruction to add — `39 1/4"+6"`. There is no single number to type, and working it
out yourself would put your arithmetic into the answers the readers are marked against. Tick the
column and leave `value` empty.

Everything else is accepted exactly as the drawing writes it: `25-1/2"` with a hyphen, `381 [15]`
with millimetres and the inch in brackets, and a trailing site note like `2" (VIF)`.

**If you cannot read it, put any character in `unreadable` and leave `value` empty.** That is a real
answer and a useful one. A crop no person can read is a crop no model should be trusted on, and the
count of them is part of the result.

**Tick `rotated` when the text runs sideways or upside down.** The machine cannot tell: across eight
pages of AI_Set_2 every region reported zero rotation while a quarter of them were taller than wide.
Rotated text is the case where two models read the same crop as `60` and as `4`, both confidently,
so this column is the one that makes those measurable.

`note` is free text for anything worth saying — an ambiguous glyph, a crop that caught two labels.
Nothing reads it; it is for the next person.

Leave every other column alone. They locate the crop on the page and `build` needs them unchanged.
"""


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

    # **A crop that will not render is not a crop**, and the count is reported rather than absorbed.
    # Before the margin, 29 of 361 planned regions on page 3 of `AI_Set_2.pdf` were one or two pixels
    # across and PDFium refused them. Each crop now holds the margin round its region, so a refusal
    # here is rarer, and still the renderer's to make rather than a minimum size invented here.
    #
    # **Rendered by `render_crop`, the bake-off's own renderer**, from the polygon the key will
    # record, so the bytes written here are the bytes `load_crops` renders from it for a model.
    written: list[Candidate] = []
    unrenderable = 0
    page_px = {page: _page_px(pdf, page - 1) for page in {candidate.page for candidate in chosen}}
    for candidate in chosen:
        try:
            image = render_crop(
                pdf,
                page=candidate.page,
                polygon=candidate.crop,
                polygon_dpi=VISION_CROP_DPI,
                output_dpi=VISION_CROP_DPI,
            )
            width, height, _rgb = decode_rgb_png(image)
            wide = _wide_view(
                pdf, candidate, crop_size=(width, height), page_px=page_px[candidate.page]
            )
        except (ModelBakeoffError, ValueError):
            unrenderable += 1
            continue
        (out / f"{candidate.crop_id}.png").write_bytes(image)
        (out / _wide_name(candidate)).write_bytes(wide)
        written.append(candidate)

    if not written:
        raise ScaffoldError(
            f"none of the {len(chosen)} sampled regions could be rendered as a crop; the renderer "
            "refused every one."
        )

    chosen = written
    _write_sheet(out, chosen)
    _write_contact_sheet(out, chosen)
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


def _parsed(raw: str, *, crop_id: str) -> object:
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
            "`not_a_single_value` and leave `value` empty. Do not add them up — arithmetic we "
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
    not_a_single_value: list[str] = []
    empty: list[str] = []

    for index, row in enumerate(rows):
        crop_id = (row.get("crop_id") or "").strip()
        if not crop_id:
            raise ScaffoldError(f"row {index} has no crop_id; do not reorder or edit those columns")
        if (row.get("unreadable") or _BLANK).strip():
            # **Recorded, not dropped.** A crop no person can read is a crop no model should be
            # trusted on, and the count belongs in the result rather than in the difference between
            # two numbers nobody compares.
            unreadable.append(crop_id)
            continue
        if (row.get("not_a_single_value") or _BLANK).strip():
            # **A different fact from `unreadable`, and kept apart from it deliberately (#730).**
            # "I could not read it" is about legibility; "this is not one dimension" is about what
            # the drawing says. Counting them together would hide how often these sheets carry a
            # compound like `39 1/4"+6"`, which is itself a finding, and would make the key's
            # unreadable rate look worse than the drawings are.
            not_a_single_value.append(crop_id)
            continue
        if not (row.get("value") or _BLANK).strip():
            empty.append(crop_id)
            continue

        value = _parsed(row["value"], crop_id=crop_id)
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
        # index as well as by name — `_tags_for` reads either.
        bakeoff_id = str(len(observations))
        chosen = ["rotated"] if (row.get("rotated") or _BLANK).strip() else []
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
                "value": _measurement_payload(value, row["value"].strip()),
                "page": page,
                "polygon": list(polygon),
                "item_id": crop_id,
            }
        )

    if not observations:
        raise ScaffoldError(
            f"no values were typed in {out / CROPS_CSV}. "
            f"{len(unreadable)} marked unreadable, {len(not_a_single_value)} marked not a "
            f"single value, {len(empty)} left blank."
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
