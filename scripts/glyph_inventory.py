"""Group the vendor's character shapes so a person labels each one once (#756 phase B).

**This script never decides what a character is.** It groups alike shapes, suggests a character for
each group from standard font shapes, and writes a page where a person confirms or corrects every
group. Only what the person confirmed becomes a template, and only templates ever read anything.

Two steps.

    inventory  DRAWING.pdf --pages 1,6,9 --out data/glyph_inventory/<name>/ --reader-settings scripts/demo.sh ...
               -> label.html (the page to label in a browser), HOW_TO_LABEL.md, clusters.csv,
                  inventory.json, glyphs.json, rasters.npy

    build      data/glyph_inventory/<name>/ --labelled-by "..." --on 2026-10-01
               -> data/glyph_templates/<set-id>/ : templates.npz + manifest.json (with the set's hash)

**Where the shapes come from.** The regions the production reader plans to read (`plan_reads`), and
each region's member paths — one path object is one character on the client's drawing (#756). So
the inventory is the shapes the reader will actually meet, not a separate idea of where text is.

**Where the suggestions come from.** Standard font shapes, compared by chamfer distance. Built in:
OpenCV's Hershey fonts, which are single-line fonts and open. A deployment may add fonts it is
licensed for with `--font`; nothing it adds is committed. Each font is drawn as a centre-line and as
an outline, because the client uses both lettering styles. A suggestion is shown only when it clears
the stated distance and beats the nearest *different* character by the stated margin — otherwise the
cell is empty, because a weak pre-fill anchors the person (docs/NEXT_BUILD_PLAN.md step 5).

**Nothing it writes belongs in the repository.** Every output lives under `data/`, which is
gitignored, and `tests/test_repo_hygiene.py` asks git rather than reading this file.

Source: issue #756 · Verification: `tests/extraction/test_glyph_inventory.py`
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import html
import io
import json
import re
import string
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from functools import cache
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import cv2
import numpy as np
import pdfplumber
from numpy.typing import NDArray

from evidence.coordinates import PageTransform, PdfPoint
from extraction.annotations import OutlinedTextRegion, read_annotation_layers
from extraction.glyph_bands import FractionBarGeometry
from extraction.glyph_shapes import (
    GlyphShape,
    ShapeSettings,
    UndrawablePath,
    chamfer,
    cluster_shapes,
    describe,
    size_ratio,
    subpaths,
)
from extraction.rasterise import VISION_CROP_DPI
from extraction.reader import page_boxes_in_pdf_space
from extraction.vector_first import plan_reads

INVENTORY: Final = "inventory.json"
GLYPHS: Final = "glyphs.json"
RASTERS: Final = "rasters.npy"
CLUSTERS_CSV: Final = "clusters.csv"
LABEL_PAGE: Final = "label.html"
HOW_TO: Final = "HOW_TO_LABEL.md"
TEMPLATES: Final = "templates.npz"
MANIFEST: Final = "manifest.json"

#: The characters a suggestion may name: what the client's dimension labels are written in.
REFERENCE_CHARACTERS: Final = "0123456789\"'/-.[]()x"

#: What a person may write in `label`. Characters, plus two words that are not characters.
#:
#: `not_a_character` — line-work or a symbol, never part of a label. `sideways` — a real character
#: turned on its side: a sideways `6` and a sideways `9` are the same shape, so no one can label one
#: alone, and phase C turns the whole label upright before matching instead (#756). `dot` names no
#: character on its own either — a person who knows it is a decimal point writes `.`.
NOT_A_CHARACTER: Final = "not_a_character"
SIDEWAYS: Final = "sideways"
WORDS: Final = frozenset({NOT_A_CHARACTER, SIDEWAYS})
ALPHABET: Final = frozenset(set("0123456789\"'/-.[]()") | set(string.ascii_letters) | WORDS)

#: The reader settings this script plans regions with, read from a `scripts/demo.sh`-style file so
#: the inventory sees the regions production sees. Required: no default is supplied for any.
READER_SETTINGS: Final = (
    "GV_READER_LINE_MINIMUM_PT",
    "GV_READER_GLYPH_MAXIMUM_PT",
    "GV_READER_GLYPH_GAP_PT",
    "GV_READER_PROXIMITY_LIMIT",
    "GV_READER_LOCALIZED_MINIMUM_PATHS",
    "GV_READER_LOCALIZED_MAXIMUM_SPAN",
    "GV_READER_FRACTION_BAR_THICKNESS_MAX_PT",
    "GV_READER_FRACTION_BAR_LENGTH_MIN_PT",
    "GV_READER_FRACTION_REACH_PT",
    "GV_READER_FRACTION_GLYPH_MIN_PT",
    "GV_READER_FRACTION_GLYPH_MAX_PT",
    "GV_READER_FRACTION_PROPORTION_MAX",
)

_HERSHEY: Final = {
    "SIMPLEX": cv2.FONT_HERSHEY_SIMPLEX,
    "PLAIN": cv2.FONT_HERSHEY_PLAIN,
    "DUPLEX": cv2.FONT_HERSHEY_DUPLEX,
    "COMPLEX": cv2.FONT_HERSHEY_COMPLEX,
    "TRIPLEX": cv2.FONT_HERSHEY_TRIPLEX,
}

#: The resolution a reference character is drawn at before it is normalised, as a multiple of the
#: comparison raster. Drawing small and scaling up would blur exactly the detail a match turns on.
_REFERENCE_SCALE: Final = 16


class InventoryError(Exception):
    """The inventory could not be made, or the labelled sheet could not be trusted."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def read_reader_settings(path: Path) -> dict[str, str]:
    """`NAME=value` lines from a demo.sh-style file, for exactly the settings this script needs."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise InventoryError(f"could not read the reader settings in {path}: {error}") from error
    found: dict[str, str] = {}
    for name in READER_SETTINGS:
        match = re.search(rf"^\s*{name}=(\S+)", text, flags=re.MULTILINE)
        if match is None:
            raise InventoryError(f"{path} does not state {name}, and it has no default")
        found[name] = match.group(1).rstrip("\\").strip()
    return found


def _fraction_bar(settings: dict[str, str]) -> FractionBarGeometry:
    return FractionBarGeometry(
        bar_thickness_max_pt=Decimal(settings["GV_READER_FRACTION_BAR_THICKNESS_MAX_PT"]),
        bar_length_min_pt=Decimal(settings["GV_READER_FRACTION_BAR_LENGTH_MIN_PT"]),
        reach_pt=Decimal(settings["GV_READER_FRACTION_REACH_PT"]),
        glyph_min_pt=Decimal(settings["GV_READER_FRACTION_GLYPH_MIN_PT"]),
        glyph_max_pt=Decimal(settings["GV_READER_FRACTION_GLYPH_MAX_PT"]),
        proportion_max=Decimal(settings["GV_READER_FRACTION_PROPORTION_MAX"]),
    )


# ---------------------------------------------------------------------------
# Collecting the shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Glyph:
    """One member path of one planned region, as the inventory records it."""

    page: int
    """One-based, as the answer key's crops are."""

    region: int
    path: int
    box_px: tuple[int, int, int, int]
    """left, top, right, bottom in page pixels at the crop DPI — the answer key's frame."""

    shape: GlyphShape


def _page_indexes(pages: str, page_count: int) -> list[int]:
    indexes: set[int] = set()
    for part in pages.split(","):
        part = part.strip()
        if not part:
            continue
        low, _, high = part.partition("-")
        first, last = int(low), int(high or low)
        if first < 1 or last > page_count or first > last:
            raise InventoryError(f"pages {part!r} is outside the drawing's 1–{page_count}")
        indexes.update(range(first - 1, last))
    if not indexes:
        raise InventoryError("no pages were named")
    return sorted(indexes)


def _run_height(region: OutlinedTextRegion) -> Decimal:
    ys = [y for path in region.glyph_paths for _, y in path.points]
    return max(ys) - min(ys)


def read_crop_boxes(path: Path) -> dict[int, list[tuple[int, int, int, int]]]:
    """Every crop of an answer key's `crops.csv`, by one-based page, in the key's pixel frame."""
    boxes: dict[int, list[tuple[int, int, int, int]]] = {}
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                boxes.setdefault(int(row["page"]), []).append(
                    (
                        int(row["left_px"]),
                        int(row["top_px"]),
                        int(row["right_px"]),
                        int(row["bottom_px"]),
                    )
                )
    except (OSError, KeyError, ValueError) as error:
        raise InventoryError(f"{path} is not an answer key's crops.csv: {error}") from error
    return boxes


def _overlaps(box: tuple[int, int, int, int], crop: tuple[int, int, int, int]) -> bool:
    return box[0] <= crop[2] and crop[0] <= box[2] and box[1] <= crop[3] and crop[1] <= box[3]


def collect(
    pdf: bytes,
    *,
    pages: Sequence[int],
    reader: dict[str, str],
    shape_settings: ShapeSettings,
    excluded: dict[int, list[tuple[int, int, int, int]]] | None = None,
) -> tuple[list[Glyph], Counter[str]]:
    """Every member path of every region production plans to read on `pages`, described.

    `excluded` names crops whose characters must not become templates — an answer key's, so the
    readings it scores are never among the shapes a person labelled (#756 phase D). A glyph that
    touches one at all is left out, and counted.
    """
    glyphs: list[Glyph] = []
    skipped: Counter[str] = Counter()
    for page_index in pages:
        layers = read_annotation_layers(
            pdf,
            page_index,
            document_version_id=UUID(int=0),
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
        with pdfplumber.open(io.BytesIO(pdf)) as plumbed:
            page = plumbed.pages[page_index]
            media_box, crop_box = page_boxes_in_pdf_space(page)
            transform = PageTransform(
                dpi=VISION_CROP_DPI,
                rotation=int(page.rotation or 0) % 360,
                media_box=media_box,
                crop_box=crop_box,
            )
        for region_index, entry in enumerate(plan.to_read):
            region = entry.region
            run_height = _run_height(region) if region.glyph_paths else Decimal(0)
            if run_height <= 0:
                skipped["a region with no height to size its characters against"] += len(
                    region.glyph_paths
                )
                continue
            for path_index, path in enumerate(region.glyph_paths):
                try:
                    shape = describe(path, run_height=run_height, settings=shape_settings)
                except UndrawablePath as error:
                    skipped[str(error)] += 1
                    continue
                corners = [transform.to_image(PdfPoint(x=x, y=y)) for x, y in path.points]
                box = (
                    min(point.x for point in corners),
                    min(point.y for point in corners),
                    max(point.x for point in corners),
                    max(point.y for point in corners),
                )
                if excluded and any(
                    _overlaps(box, crop) for crop in excluded.get(page_index + 1, ())
                ):
                    skipped["inside an excluded answer-key crop"] += 1
                    continue
                glyphs.append(
                    Glyph(
                        page=page_index + 1,
                        region=region_index,
                        path=path_index,
                        box_px=box,
                        shape=shape,
                    )
                )
    return glyphs, skipped


# ---------------------------------------------------------------------------
# Reference shapes from fonts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reference:
    """One character as a font draws it, normalised the way a vendor glyph is."""

    label: str
    source: str
    shape: GlyphShape


def _thin(mask: NDArray[np.bool_]) -> NDArray[np.bool_]:
    """Zhang–Suen thinning: a filled shape down to its one-pixel centre-line.

    Here rather than from a library because neither OpenCV's contrib thinning nor scikit-image is a
    dependency, and a centre-line of an outline font is how the client's single-line lettering is
    compared with one.
    """
    image = np.pad(mask.astype(np.uint8), 1)
    changed = True
    while changed:
        changed = False
        for step in (0, 1):
            p2 = image[:-2, 1:-1]
            p3 = image[:-2, 2:]
            p4 = image[1:-1, 2:]
            p5 = image[2:, 2:]
            p6 = image[2:, 1:-1]
            p7 = image[2:, :-2]
            p8 = image[1:-1, :-2]
            p9 = image[:-2, :-2]
            neighbours = [p2, p3, p4, p5, p6, p7, p8, p9]
            count = sum(neighbours)
            ring = neighbours + [p2]
            transitions = sum(
                ((ring[k] == 0) & (ring[k + 1] == 1)).astype(np.uint8) for k in range(8)
            )
            if step == 0:
                first, second = p2 * p4 * p6, p4 * p6 * p8
            else:
                first, second = p2 * p4 * p8, p2 * p6 * p8
            remove = (
                (image[1:-1, 1:-1] == 1)
                & (count >= 2)
                & (count <= 6)
                & (transitions == 1)
                & (first == 0)
                & (second == 0)
            )
            if remove.any():
                image[1:-1, 1:-1][remove] = 0
                changed = True
    return np.asarray(image[1:-1, 1:-1] > 0, dtype=np.bool_)


def _normalised(points: NDArray[np.int32], settings: ShapeSettings) -> NDArray[np.uint8]:
    """Ink points of a large drawing, placed in the comparison raster the way `rasterise` places them.

    Drawing a reference, not deciding anything, so NumPy's rounding is used rather than exact
    fractions: the decision is the chamfer comparison afterwards, and that is exact.
    """
    left, top = points.min(axis=0)
    right, bottom = points.max(axis=0)
    span = max(int(right - left), int(bottom - top), 1)
    last = settings.size_px - 1
    scale = last / span
    offset_x = (last - (right - left) * scale) / 2
    offset_y = (last - (bottom - top) * scale) / 2
    columns = np.rint((points[:, 0] - left) * scale + offset_x).astype(np.int64)
    rows = np.rint((points[:, 1] - top) * scale + offset_y).astype(np.int64)
    raster = np.zeros((settings.size_px, settings.size_px), dtype=np.uint8)
    raster[rows, columns] = 255
    return raster


def _as_shape(
    mask: NDArray[np.bool_], *, digit_height: int, settings: ShapeSettings, outline: bool
) -> GlyphShape | None:
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return None
    height = int(ys.max() - ys.min())
    width = int(xs.max() - xs.min())
    if outline:
        contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
        edge = np.zeros(mask.shape, dtype=np.uint8)
        cv2.drawContours(edge, contours, -1, 255, 1)
        ys, xs = np.nonzero(edge)
    points = np.stack([xs, ys], axis=1).astype(np.int32)
    raster = _normalised(points, settings)
    if settings.dilate_px:
        kernel = np.ones((2 * settings.dilate_px + 1,) * 2, dtype=np.uint8)
        raster = np.asarray(cv2.dilate(raster, kernel), dtype=np.uint8)
    return GlyphShape(
        raster=np.asarray(raster > 0, dtype=np.bool_),
        relative_height=Fraction(height, digit_height),
        relative_width=Fraction(width, digit_height),
    )


def _hershey_mask(
    character: str, face: int, *, height_px: int, thickness: int
) -> NDArray[np.bool_]:
    scale = height_px / 22
    size = height_px * 3
    canvas = np.zeros((size, size), dtype=np.uint8)
    cv2.putText(canvas, character, (height_px // 2, height_px * 2), face, scale, 255, thickness)
    return np.asarray(canvas > 0, dtype=np.bool_)


def _font_mask(character: str, font: Any, *, height_px: int) -> NDArray[np.bool_]:
    from PIL import Image, ImageDraw

    size = height_px * 3
    image = Image.new("L", (size, size), 0)
    ImageDraw.Draw(image).text((height_px // 2, height_px // 2), character, fill=255, font=font)
    return np.asarray(np.array(image) > 127, dtype=np.bool_)


@cache
def _hershey_references(settings: ShapeSettings) -> tuple[Reference, ...]:
    """The built-in references for one set of drawing settings, drawn once per process."""
    height_px = settings.size_px * _REFERENCE_SCALE
    references: list[Reference] = []
    for name, face in _HERSHEY.items():
        digit = _hershey_mask("0", face, height_px=height_px, thickness=1)
        digit_height = int(np.ptp(np.nonzero(digit)[0])) or 1
        for character in REFERENCE_CHARACTERS:
            for style, thickness, outline in (
                ("line", 1, False),
                ("outline", max(2, height_px // 8), True),
            ):
                mask = _hershey_mask(character, face, height_px=height_px, thickness=thickness)
                shape = _as_shape(
                    mask, digit_height=digit_height, settings=settings, outline=outline
                )
                if shape is not None:
                    references.append(
                        Reference(label=character, source=f"hershey:{name}/{style}", shape=shape)
                    )
    return tuple(references)


def _font_references(settings: ShapeSettings, font_path: Path) -> list[Reference]:
    """One supplied font's references: each character thinned to a centre-line, and outlined."""
    from PIL import ImageFont

    height_px = settings.size_px * _REFERENCE_SCALE
    try:
        font = ImageFont.truetype(str(font_path), height_px)
    except OSError as error:
        raise InventoryError(f"could not load the font {font_path}: {error}") from error
    digit = _font_mask("0", font, height_px=height_px)
    digit_height = int(np.ptp(np.nonzero(digit)[0])) or 1
    references: list[Reference] = []
    for character in REFERENCE_CHARACTERS:
        filled = _font_mask(character, font, height_px=height_px)
        for style, mask, outline in (("line", _thin(filled), False), ("outline", filled, True)):
            shape = _as_shape(mask, digit_height=digit_height, settings=settings, outline=outline)
            if shape is not None:
                references.append(
                    Reference(label=character, source=f"font:{font_path.name}/{style}", shape=shape)
                )
    return references


def reference_shapes(*, settings: ShapeSettings, fonts: Sequence[Path] = ()) -> list[Reference]:
    """Every reference character, in every built-in and supplied font, as line and as outline.

    Sized against the font's own `0`, as a vendor glyph is sized against its label's height.
    """
    references = list(_hershey_references(settings))
    for font_path in fonts:
        references.extend(_font_references(settings, font_path))
    return references


@dataclass(frozen=True, slots=True)
class Suggestion:
    label: str
    source: str
    distance: Fraction
    margin: Fraction


def suggest(
    shape: GlyphShape,
    references: Sequence[Reference],
    *,
    maximum_distance: Decimal,
    minimum_margin: Decimal,
    maximum_size_ratio: Decimal,
) -> Suggestion | None:
    """The nearest reference character, or `None` where the match is weak or ambiguous.

    Weak: its chamfer distance is above `maximum_distance`. Ambiguous: the nearest reference with a
    *different* label is not `minimum_margin` further away. Either way the cell stays empty — a
    suggestion a person would have to argue with costs them more than no suggestion.

    **Never for a dot or a single straight stroke.** Their shape is exactly a font's `.`, `-` or
    `'`, so a suggestion would be confident — and on the client's drawing most of them are
    line-work, not characters. Where only context can decide, the person decides from context.
    """
    if shape.dot or shape.straight:
        return None
    ratio_limit = Fraction(maximum_size_ratio)
    best: dict[str, tuple[Fraction, str]] = {}
    for reference in references:
        if size_ratio(shape, reference.shape) > ratio_limit:
            continue
        distance = chamfer(shape.raster, reference.shape.raster)
        if reference.label not in best or distance < best[reference.label][0]:
            best[reference.label] = (distance, reference.source)
    if not best:
        return None
    ranked = sorted(best.items(), key=lambda entry: (entry[1][0], entry[0]))
    label, (distance, source) = ranked[0]
    runner_up = ranked[1][1][0] if len(ranked) > 1 else None
    margin = Fraction(10**6) if runner_up is None else runner_up - distance
    if distance > Fraction(maximum_distance) or margin < Fraction(minimum_margin):
        return None
    return Suggestion(label=label, source=source, distance=distance, margin=margin)


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------


def _png(image: NDArray[np.uint8]) -> str:
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise InventoryError("a contact image could not be encoded")
    return base64.b64encode(encoded.tobytes()).decode("ascii")


def _ink(raster: NDArray[np.bool_], scale: int) -> NDArray[np.uint8]:
    image = np.where(raster, 0, 255).astype(np.uint8)
    return np.asarray(
        cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST), dtype=np.uint8
    )


def _spread(members: Sequence[int], count: int) -> list[int]:
    """`count` members spread evenly through the cluster, so a stray one anywhere can be seen."""
    if len(members) <= count:
        return list(members)
    step = Fraction(len(members) - 1, count - 1)
    return [members[round(step * k)] for k in range(count)]


def _context(
    pdf_regions: dict[tuple[int, int], OutlinedTextRegion], glyph: Glyph, *, height: int
) -> NDArray[np.uint8]:
    """The whole region the glyph came from, drawn from its paths, with the glyph in red."""
    region = pdf_regions[(glyph.page, glyph.region)]
    points = [point for path in region.glyph_paths for point in path.points]
    left = min(x for x, _ in points)
    top = max(y for _, y in points)
    span_x = max(x for x, _ in points) - left
    span_y = top - min(y for _, y in points)
    scale = Decimal(height - 8) / max(span_y, span_x / 4, Decimal("0.001"))
    width = int(span_x * scale) + 8
    canvas = np.full((height, max(width, 8), 3), 255, dtype=np.uint8)
    for index, path in enumerate(region.glyph_paths):
        colour, thickness = ((0, 0, 220), 3) if index == glyph.path else ((60, 60, 60), 2)
        try:
            parts = subpaths(path, bezier_steps=4)
        except UndrawablePath:
            continue
        for part in parts:
            polygon = np.array(
                [[int((x - left) * scale) + 4, int((top - y) * scale) + 4] for x, y in part.points],
                dtype=np.int32,
            )
            cv2.polylines(canvas, [polygon], part.closed, colour, thickness)
    return canvas


def inventory(arguments: argparse.Namespace) -> int:
    pdf_path = Path(arguments.pdf)
    try:
        pdf = pdf_path.read_bytes()
    except OSError as error:
        raise InventoryError(f"could not read {pdf_path}: {error}") from error
    reader = read_reader_settings(Path(arguments.reader_settings))
    settings = ShapeSettings(
        size_px=arguments.size_px,
        bezier_steps=arguments.bezier_steps,
        stroke_px=arguments.stroke_px,
        dilate_px=arguments.dilate_px,
    )
    with pdfplumber.open(io.BytesIO(pdf)) as plumbed:
        page_count = len(plumbed.pages)
    pages = _page_indexes(arguments.pages, page_count)

    excluded = (
        None if arguments.exclude_crops is None else read_crop_boxes(Path(arguments.exclude_crops))
    )
    glyphs, skipped = collect(
        pdf, pages=pages, reader=reader, shape_settings=settings, excluded=excluded
    )
    if not glyphs:
        raise InventoryError("no glyph shapes were found on those pages")
    clusters = cluster_shapes(
        [glyph.shape for glyph in glyphs],
        minimum_overlap=Decimal(arguments.minimum_overlap),
        maximum_size_ratio=Decimal(arguments.maximum_size_ratio),
    )
    clusters.sort(key=lambda members: (-len(members), members[0]))
    references = reference_shapes(settings=settings, fonts=[Path(f) for f in arguments.font])
    suggestions = [
        suggest(
            glyphs[members[0]].shape,
            references,
            maximum_distance=Decimal(arguments.suggest_max_distance),
            minimum_margin=Decimal(arguments.suggest_margin),
            maximum_size_ratio=Decimal(arguments.suggest_size_ratio),
        )
        for members in clusters
    ]

    out = Path(arguments.out)
    out.mkdir(parents=True, exist_ok=True)
    np.save(out / RASTERS, np.stack([glyph.shape.raster for glyph in glyphs]))
    cluster_of = {index: number for number, members in enumerate(clusters) for index in members}
    (out / GLYPHS).write_text(
        json.dumps(
            [
                {
                    "page": glyph.page,
                    "region": glyph.region,
                    "path": glyph.path,
                    "box_px": list(glyph.box_px),
                    "relative_height": str(glyph.shape.relative_height),
                    "relative_width": str(glyph.shape.relative_width),
                    "dot": glyph.shape.dot,
                    "cluster": _cluster_id(cluster_of[index]),
                }
                for index, glyph in enumerate(glyphs)
            ],
            indent=0,
        ),
        encoding="utf-8",
    )
    reference_by_source = {(ref.label, ref.source): ref for ref in references}
    with (out / CLUSTERS_CSV).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "cluster_id",
                "count",
                "suggested",
                "suggested_from",
                "distance",
                "label",
                "mixed",
                "note",
            ]
        )
        for number, (members, suggestion) in enumerate(zip(clusters, suggestions, strict=True)):
            writer.writerow(
                [
                    _cluster_id(number),
                    len(members),
                    "" if suggestion is None else suggestion.label,
                    "" if suggestion is None else suggestion.source,
                    "" if suggestion is None else f"{float(suggestion.distance):.3f}",
                    "",
                    "",
                    "dot: single points with no shape" if glyphs[members[0]].shape.dot else "",
                ]
            )
    (out / INVENTORY).write_text(
        json.dumps(
            {
                "schema": "glyph-inventory/v1",
                "source_pdf": pdf_path.name,
                "source_sha256": hashlib.sha256(pdf).hexdigest(),
                "pages": [index + 1 for index in pages],
                "excluded_crops": (
                    None if arguments.exclude_crops is None else Path(arguments.exclude_crops).name
                ),
                "reader_settings": reader,
                "shape_settings": settings.config_hash,
                "minimum_overlap": arguments.minimum_overlap,
                "maximum_size_ratio": arguments.maximum_size_ratio,
                "suggestions": {
                    "max_distance": arguments.suggest_max_distance,
                    "margin": arguments.suggest_margin,
                    "size_ratio": arguments.suggest_size_ratio,
                    "fonts": ["hershey (built in)"] + [Path(f).name for f in arguments.font],
                },
                "glyphs": len(glyphs),
                "clusters": len(clusters),
                "suggested": sum(suggestion is not None for suggestion in suggestions),
                "skipped": dict(skipped),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    _write_label_page(out, pdf, pages, reader, glyphs, clusters, suggestions, reference_by_source)
    _write_how_to(out)

    sizes = [len(members) for members in clusters]
    covered = np.cumsum(sizes) / sum(sizes)
    print(f"\n  {len(glyphs)} shapes from pages {arguments.pages}, in {len(clusters)} clusters")
    for fraction in (0.8, 0.95, 0.99):
        print(f"    {int(np.searchsorted(covered, fraction)) + 1:4d} clusters cover {fraction:.0%}")
    print(f"  {sum(s is not None for s in suggestions)} clusters have a suggestion")
    if skipped:
        print(f"  not described: {dict(skipped)}")
    print(f"\n  label them:  open {out / LABEL_PAGE}")
    print(
        f"  then:        python scripts/glyph_inventory.py build {out} --labelled-by ... --on ..."
    )
    return 0


def _cluster_id(number: int) -> str:
    return f"c{number + 1:04d}"


def _write_label_page(
    out: Path,
    pdf: bytes,
    pages: Sequence[int],
    reader: dict[str, str],
    glyphs: Sequence[Glyph],
    clusters: Sequence[Sequence[int]],
    suggestions: Sequence[Suggestion | None],
    references: dict[tuple[str, str], Reference],
) -> None:
    """A page that opens from disk, shows every cluster, and saves the labels as `clusters.csv`.

    Local on purpose: the images are the client's drawing, so the page embeds them and is opened
    from the file system — nothing is uploaded anywhere.
    """
    regions: dict[tuple[int, int], OutlinedTextRegion] = {}
    for page_index in pages:
        layers = read_annotation_layers(
            pdf,
            page_index,
            document_version_id=UUID(int=0),
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
        for index, entry in enumerate(plan.to_read):
            regions[(page_index + 1, index)] = entry.region

    rows: list[str] = []
    for number, (members, suggestion) in enumerate(zip(clusters, suggestions, strict=True)):
        founder = glyphs[members[0]]
        cluster_id = _cluster_id(number)
        strip = "".join(
            f'<img class="m" src="data:image/png;base64,{_png(_ink(glyphs[i].shape.raster, 2))}">'
            for i in _spread(members, 10)
        )
        if founder.shape.dot:
            shape_cell = "<i>a single point</i>"
        else:
            shape_cell = f'<img src="data:image/png;base64,{_png(_ink(founder.shape.raster, 3))}">'
        if suggestion is None:
            suggestion_cell = "<span class=none>no suggestion</span>"
        else:
            reference = references[(suggestion.label, suggestion.source)]
            suggestion_cell = (
                f'<img src="data:image/png;base64,{_png(_ink(reference.shape.raster, 3))}"><br>'
                f"<b>{html.escape(suggestion.label)}</b> "
                f"<small>{html.escape(suggestion.source)} · {float(suggestion.distance):.2f}</small>"
            )
        rows.append(
            f'<tr data-id="{cluster_id}" data-suggested="{html.escape(suggestion.label if suggestion else "")}">'
            f"<td><b>{cluster_id}</b><br>{len(members)} copies</td>"
            f"<td>{shape_cell}</td><td>{suggestion_cell}</td>"
            f'<td><img src="data:image/png;base64,{_png(_context(regions, founder, height=110))}"></td>'
            f"<td>{strip}</td>"
            '<td><input class="label" size="14" placeholder="type it"> '
            + (
                '<button class="accept" type="button">accept</button> '
                if suggestion is not None
                else ""
            )
            + '<button class="word" data-word="not_a_character" type="button">not a character</button> '
            '<button class="word" data-word="sideways" type="button">sideways</button>'
            '<br><label><input class="mixed" type="checkbox"> mixed</label>'
            '<br><input class="note" size="18" placeholder="note"></td></tr>'
        )
    # **A script is not HTML.** Entities are not decoded inside `<script>`, so the alphabet goes in
    # as a JSON string literal — never through `html.escape`, which turned its quote into `&quot;`
    # and stopped every button on the page. `</` is broken so no value can end the script early.
    alphabet = json.dumps("".join(sorted(ALPHABET - WORDS))).replace("</", "<\\/")
    page = _LABEL_PAGE.replace("{{ROWS}}", "\n".join(rows)).replace("{{ALPHABET}}", alphabet)
    (out / LABEL_PAGE).write_text(page, encoding="utf-8")


_LABEL_PAGE: Final = """<!doctype html>
<html><head><meta charset="utf-8"><title>Glyph labels</title>
<style>
body { font: 14px system-ui, sans-serif; margin: 16px; background: #fff; color: #111; }
table { border-collapse: collapse; }
td { border-bottom: 1px solid #ddd; padding: 6px 8px; vertical-align: middle; }
img { image-rendering: pixelated; border: 1px solid #eee; }
img.m { margin-right: 2px; }
.none { color: #999; }
tr.done { background: #eefaf0; }
tr.bad { background: #fdecea; }
#bar { position: sticky; top: 0; background: #fff; padding: 8px 0; border-bottom: 2px solid #333; }
</style></head><body>
<div id="bar"><b>Label each group</b> — type the character it is, or
<code>not_a_character</code>, or <code>sideways</code> (a character turned on its side).
Tick <b>mixed</b> if a group holds two different characters.
<button id="save" type="button">Save clusters.csv</button> <span id="count"></span></div>
<table><tr><th>group</th><th>shape</th><th>suggested</th><th>where it came from</th>
<th>copies (spread through the group)</th><th>your label</th></tr>
{{ROWS}}
</table>
<script>
const alphabet = new Set([...{{ALPHABET}}]);
const words = new Set(["not_a_character", "sideways"]);
const rows = [...document.querySelectorAll("tr[data-id]")];
function valid(v) { return v === "" || words.has(v) || (v.length === 1 && alphabet.has(v)); }
function refresh() {
  let done = 0;
  for (const row of rows) {
    const v = row.querySelector(".label").value.trim();
    row.classList.toggle("done", v !== "" && valid(v));
    row.classList.toggle("bad", !valid(v));
    if (v !== "" && valid(v)) done++;
  }
  document.getElementById("count").textContent = done + " of " + rows.length + " labelled";
}
for (const row of rows) {
  row.querySelector(".label").addEventListener("input", refresh);
  const accept = row.querySelector(".accept");
  if (accept) accept.addEventListener("click", () => {
    row.querySelector(".label").value = row.dataset.suggested; refresh();
  });
  for (const button of row.querySelectorAll(".word")) {
    button.addEventListener("click", () => {
      row.querySelector(".label").value = button.dataset.word; refresh();
    });
  }
}
function cell(v) { return /[",\\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; }
document.getElementById("save").addEventListener("click", () => {
  const bad = rows.filter(r => !valid(r.querySelector(".label").value.trim()));
  if (bad.length) { alert(bad.length + " labels are not in the alphabet"); return; }
  const lines = ["cluster_id,label,mixed,note"];
  for (const row of rows) {
    lines.push([row.dataset.id, row.querySelector(".label").value.trim(),
      row.querySelector(".mixed").checked ? "yes" : "", row.querySelector(".note").value.trim()]
      .map(cell).join(","));
  }
  const blob = new Blob([lines.join("\\n") + "\\n"], { type: "text/csv" });
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
  a.download = "labels.csv"; a.click();
});
refresh();
</script></body></html>
"""


def _write_how_to(out: Path) -> None:
    (out / HOW_TO).write_text(
        """# How to label these

Each row is a group of shapes that look alike on the drawing. You label the group once, and every
copy of that shape reads as what you said.

1. Open `label.html` in your browser. It works from your own computer; nothing is uploaded.
2. For each group, look at the **shape**, the **copies** (spread through the group, so a stray one
   shows), and **where it came from** (the red stroke, inside the label or drawing it sat in).
3. Type what it is:
   - a character: `0`–`9`, `"`, `'`, `/`, `-`, `.`, `[`, `]`, `(`, `)`, `x`, or a letter;
   - `not_a_character` for a piece of the drawing: a line, an arrowhead, a symbol;
   - `sideways` for a real character turned on its side. Do not guess which one — a sideways `6`
     and a sideways `9` are the same shape. The reader turns the whole label upright later.
4. If a suggestion is shown and it is right, press **accept**. If it is wrong, type the right one.
   **A character from a sideways label is often suggested wrongly** — its shape is turned, so the
   nearest font character is a different one. If "where it came from" shows a label running up
   the page, use **sideways**, whatever the suggestion says.
   A suggestion is never used unless you accept or type it. The **not a character** and
   **sideways** buttons fill those words in one click; most groups are pieces of the drawing.
5. You do not have to label every group. The page is sorted with the most common first; a group
   you leave empty reads nothing, and a label that needs it is handed to a reviewer instead.
6. Tick **mixed** if the copies are not all the same character. That group will be split and shown
   again.
7. A straight line may be a `1`, a `-`, or a piece of the drawing. Label it by what it is **when it
   sits in a label** (the red stroke shows you where it came from). The reader checks every label
   it composes, so a line read as `1` in the middle of a drawing is refused, not used.
8. Press **Save clusters.csv**. Your browser downloads `labels.csv`; put it in this folder.
""" + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def _labels(out: Path) -> dict[str, dict[str, str]]:
    path = out / "labels.csv"
    if not path.exists():
        raise InventoryError(f"{path} is not there yet: save it from {out / LABEL_PAGE} first")
    with path.open(encoding="utf-8", newline="") as stream:
        rows = {row["cluster_id"]: row for row in csv.DictReader(stream)}
    return rows


def _key_dependencies(key: Path, glyphs: Sequence[dict[str, object]]) -> dict[str, set[str]]:
    """Which clusters each scored answer-key crop holds a glyph of."""
    needed: dict[str, set[str]] = {}
    with key.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            if not row.get("value"):
                continue
            page = int(row["page"])
            left, top = int(row["left_px"]), int(row["top_px"])
            right, bottom = int(row["right_px"]), int(row["bottom_px"])
            for glyph in glyphs:
                box = glyph["box_px"]
                assert isinstance(box, list)
                if (
                    glyph["page"] == page
                    and left <= box[0]
                    and top <= box[1]
                    and box[2] <= right
                    and box[3] <= bottom
                ):
                    needed.setdefault(row["crop_id"], set()).add(str(glyph["cluster"]))
    return needed


def build(arguments: argparse.Namespace) -> int:
    out = Path(arguments.out)
    try:
        meta = json.loads((out / INVENTORY).read_text(encoding="utf-8"))
        glyphs = json.loads((out / GLYPHS).read_text(encoding="utf-8"))
        rasters = np.load(out / RASTERS)
        with (out / CLUSTERS_CSV).open(encoding="utf-8", newline="") as stream:
            clusters = {row["cluster_id"]: row for row in csv.DictReader(stream)}
    except (OSError, ValueError) as error:
        raise InventoryError(f"{out} is not a complete inventory: {error}") from error
    labels = _labels(out)

    unknown = sorted(set(labels) - set(clusters))
    if unknown:
        raise InventoryError(
            f"labels.csv names clusters this inventory does not have: {unknown[:5]}"
        )
    problems: list[str] = []
    labelled: dict[str, str] = {}
    for cluster_id, row in labels.items():
        label = (row.get("label") or "").strip()
        if (row.get("mixed") or "").strip():
            problems.append(f"{cluster_id} is marked mixed: it must be split before it can read")
            continue
        if not label:
            continue
        if label not in ALPHABET:
            problems.append(f"{cluster_id}: {label!r} is not in the alphabet")
            continue
        labelled[cluster_id] = label
    if arguments.key is not None:
        for crop_id, needed in sorted(_key_dependencies(Path(arguments.key), glyphs).items()):
            missing = sorted(needed - set(labelled))
            if missing:
                problems.append(
                    f"answer-key crop {crop_id} depends on unlabelled clusters {missing[:5]}"
                )
    if problems:
        raise InventoryError("the labels cannot be built:\n    " + "\n    ".join(problems))
    if not labelled:
        raise InventoryError("no cluster is labelled")

    kinds: dict[str, str] = {}
    for cluster_id, label in labelled.items():
        suggested = clusters[cluster_id]["suggested"]
        kinds[cluster_id] = (
            "typed" if not suggested else "accepted" if suggested == label else "corrected"
        )
    members = [index for index, glyph in enumerate(glyphs) if glyph["cluster"] in labelled]
    template_labels = [labelled[str(glyphs[index]["cluster"])] for index in members]
    template_rasters = rasters[members]
    heights = [str(glyphs[index]["relative_height"]) for index in members]
    widths = [str(glyphs[index]["relative_width"]) for index in members]
    dots = [bool(glyphs[index]["dot"]) for index in members]

    content = {
        "schema": "glyph-templates/v1",
        "source_sha256": meta["source_sha256"],
        "pages": meta["pages"],
        "excluded_crops": meta.get("excluded_crops"),
        "shape_settings": meta["shape_settings"],
        "reader_settings": meta["reader_settings"],
        "labels": [
            {"cluster": cluster_id, "label": labelled[cluster_id], "source": kinds[cluster_id]}
            for cluster_id in sorted(labelled)
        ],
    }
    digest = hashlib.sha256()
    digest.update(json.dumps(content, sort_keys=True).encode("utf-8"))
    digest.update(json.dumps([template_labels, heights, widths, dots]).encode("utf-8"))
    digest.update(np.ascontiguousarray(template_rasters).tobytes())
    set_hash = digest.hexdigest()

    target = Path(arguments.templates_root) / set_hash[:12]
    target.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        target / TEMPLATES,
        rasters=template_rasters,
        labels=np.array(template_labels),
        relative_heights=np.array(heights),
        relative_widths=np.array(widths),
        dots=np.array(dots),
    )
    counts = Counter(kinds.values())
    (target / MANIFEST).write_text(
        json.dumps(
            {
                **content,
                "sha256": set_hash,
                "labelled_by": arguments.labelled_by,
                "labelled_on": arguments.on.isoformat(),
                "clusters_labelled": len(labelled),
                "clusters_unlabelled": len(clusters) - len(labelled),
                "templates": len(members),
                "label_sources": dict(counts),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n  template set {set_hash[:12]}: {len(labelled)} clusters, {len(members)} templates")
    print(
        f"  accepted {counts['accepted']}, corrected {counts['corrected']}, typed {counts['typed']}"
    )
    print(f"  written to {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    make = subcommands.add_parser("inventory", help="group the shapes and write the label page")
    make.add_argument("pdf", help="the drawing; it is not modified")
    make.add_argument("--pages", required=True, help="one-based, e.g. 1,6,9-12")
    make.add_argument("--out", required=True, help="output directory; must be under data/")
    make.add_argument(
        "--reader-settings",
        required=True,
        help="a demo.sh-style file stating the GV_READER_* values, e.g. scripts/demo.sh",
    )
    for name, meaning in (
        ("--size-px", "comparison raster size"),
        ("--bezier-steps", "points per Bézier segment"),
        ("--stroke-px", "drawn stroke width"),
        ("--dilate-px", "stroke thickening before comparing"),
    ):
        make.add_argument(name, type=int, required=True, help=f"{meaning} (no default)")
    for name, meaning in (
        ("--minimum-overlap", "shared ink needed to join a group, 0–1"),
        ("--maximum-size-ratio", "how far two sizes may differ in a group, ≥ 1"),
        ("--suggest-max-distance", "largest chamfer distance a suggestion may have, in pixels"),
        ("--suggest-margin", "how much nearer than a different character it must be, in pixels"),
        ("--suggest-size-ratio", "how far a reference's size may differ from the shape's, ≥ 1"),
    ):
        make.add_argument(name, required=True, help=f"{meaning} (no default)")
    make.add_argument(
        "--exclude-crops",
        help="an answer key's crops.csv: no character inside any of its crops becomes a template",
    )
    make.add_argument(
        "--font",
        action="append",
        default=[],
        help="an extra font file to suggest from, which the deployment is licensed for; repeatable",
    )
    make.set_defaults(handler=inventory)

    finish = subcommands.add_parser("build", help="turn the saved labels into a template set")
    finish.add_argument("out", help="the directory inventory wrote")
    finish.add_argument("--labelled-by", required=True, help="who labelled the groups")
    finish.add_argument("--on", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    finish.add_argument(
        "--templates-root",
        default="data/glyph_templates",
        help="where template sets are kept (default: data/glyph_templates, gitignored)",
    )
    finish.add_argument(
        "--key",
        help="an answer key's crops.csv: refuse to build while a scored crop's shapes are unlabelled",
    )
    finish.set_defaults(handler=build)

    arguments = parser.parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except InventoryError as error:
        print(f"\n  {error}\n", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
