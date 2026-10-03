#!/usr/bin/env python3
"""Check the reading agent's sideways and cut-off detection against a person, off the key (#778).

The agent decides from the drawing's own lines whether a crop cut a label off and whether a label
runs sideways (`extraction/agent/geometry.py`). On the 51-crop key the geometry found 1 of 14
sideways labels and 24 cut-offs against a person's 7 (#757). Fixing that by looking at the key would
leave the key measuring our own tuning, so this measures it on **crops the key does not hold**, read
by a person.

    sheet  DRAWING.pdf --key KEY_DIR --reader-settings scripts/demo.sh --out data/…/check/
           -> cNN.png (the crop the reader is shown) + cNN_wide.png (a wider view, the crop's edge in
              red) + crops.csv with empty `sideways` and `cut_off` columns + sheet.html

    score  data/…/check/ --pdf DRAWING.pdf --reader-settings scripts/demo.sh
           -> how each rule agrees with the person's answers

**Only crops that show a number.** Most planned regions are drawing symbols — outlets, hardware,
shelving — and a sheet of those would measure nothing about labels. A crop is kept only where the
local OCR engine (RapidOCR, no paid call) finds a numeral in it, read as it stands or turned either
way, so a sideways label is not dropped for being sideways. That decides only which crops are shown;
every answer is the person's.

**Blind.** The sheet shows no geometry call; which group each crop was drawn from is written to
`geometry.json`, which the sheet does not show. **Crops are the stage's own**: cut by
`workflow.reading_agent.RegionCrops` round each planned region, judged by
`workflow.stages.region_facts`. **Nothing it writes belongs in the repository**: the output directory
must be under `data/`. **One page at a time**: a 300 dpi render of one of these sheets is about 50 MB.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import random
import re
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Final
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.experiments.agent_scorecard import (
    PageGeometry,
    ScorecardPage,
    build_pages,
    load_key,
)
from eval.experiments.model_bakeoff import ModelBakeoffError, key_polygon_dpi
from evidence.coordinates import ImagePoint
from evidence.crop import CropSpec, crop_pixel_box, encode_png
from extraction.agent.geometry import Box, LabelReach
from extraction.annotations import VectorPath, glyph_runs
from extraction.geometry.dimension_lines import detect
from extraction.geometry.text_association import lines_within
from extraction.glyph_bands import FractionBarGeometry
from extraction.glyph_reader import gather_label
from extraction.vector_first import plan_reads
from storage.store import ArtifactStore

CROPS_CSV: Final = "crops.csv"
GEOMETRY_JSON: Final = "geometry.json"
HOW_TO: Final = "HOW_TO_CHECK.md"
SHEET: Final = "sheet.html"
ANSWERS: Final = frozenset({"yes", "no"})
ANSWER_COLUMNS: Final = ("label", "sideways", "cut_off")
STRATA: Final = ("sideways", "tall", "cut", "whole")
"""The groups crops are drawn from, by what the geometry says: sideways; upright but taller than
wide (where a missed sideways label would be); cut off; neither."""

#: How much wider than the crop the person's second view is, so a label the crop cut can be seen
#: continuing past its edge. A viewing aid; nothing is decided by it.
WIDE_VIEW_PT: Final = Decimal(30)

#: The label gaps `score` compares, fixed before any answer was read: the scorecard's 4 pt, the shape
#: reader's 3 pt (#756 phase D), and 2 pt.
LABEL_GAPS_PT: Final = ("4", "3", "2")

#: The dimension-line detector's settings, which `scripts/glyph_inventory.READER_SETTINGS` does not
#: read. Required like the rest: each decides which strokes count as dimension lines.
DETECTOR_SETTINGS: Final = (
    "GV_READER_WITNESS_TOLERANCE",
    "GV_READER_MINIMUM_SPAN",
    "GV_READER_STRAIGHTNESS",
    "GV_READER_CROSSING_MARGIN",
)

HOW_TO_TEXT: Final = """# Checking these crops (#778)

Each crop `cNN.png` is exactly what the reading agent is shown. `cNN_wide.png` is the same place with
more of the drawing around it; the red box is the crop's edge. Open `sheet.html` to see them side by
side.

For every row of `crops.csv`, type `yes` or `no` in three columns:

- **label**: is there a dimension label — a number — in the red box, or running into it? Many crops
  are drawing symbols (outlets, shelving); for those type `no`, and `no` in the other two.
- **sideways**: does the label's text run up or down the page, rather than across it?
- **cut_off**: does the label carry on past the red box, so the crop shows only part of it?

Use `note` for anything else (two labels, can't tell). Nothing in these files says
what the machine thought, on purpose: your answer is the measurement.
"""


class CheckError(Exception):
    """The check cannot run as asked."""


@dataclass(frozen=True, slots=True)
class CheckCrop:
    crop_id: str
    page_index: int
    region: tuple[int, int, int, int]
    """left, top, right, bottom of the planned region, in the stage's pixels."""

    stratum: str


def read_settings(path: Path) -> dict[str, str]:
    """The reader settings from a `scripts/demo.sh`-style file; every one required."""
    from scripts.glyph_inventory import read_reader_settings

    found = read_reader_settings(path)
    text = path.read_text(encoding="utf-8")
    for name in DETECTOR_SETTINGS:
        match = re.search(rf"^\s*{name}=(\S+)", text, flags=re.MULTILINE)
        if match is None:
            raise CheckError(f"{path} does not state {name}, and it has no default")
        found[name] = match.group(1).rstrip("\\").strip()
    return found


def _geometry(reader: dict[str, str]) -> PageGeometry:
    return PageGeometry(
        line_minimum_pt=Decimal(reader["GV_READER_LINE_MINIMUM_PT"]),
        glyph_maximum_pt=Decimal(reader["GV_READER_GLYPH_MAXIMUM_PT"]),
        glyph_gap_pt=Decimal(reader["GV_READER_GLYPH_GAP_PT"]),
        fraction_bar=FractionBarGeometry(
            bar_thickness_max_pt=Decimal(reader["GV_READER_FRACTION_BAR_THICKNESS_MAX_PT"]),
            bar_length_min_pt=Decimal(reader["GV_READER_FRACTION_BAR_LENGTH_MIN_PT"]),
            reach_pt=Decimal(reader["GV_READER_FRACTION_REACH_PT"]),
            glyph_min_pt=Decimal(reader["GV_READER_FRACTION_GLYPH_MIN_PT"]),
            glyph_max_pt=Decimal(reader["GV_READER_FRACTION_GLYPH_MAX_PT"]),
            proportion_max=Decimal(reader["GV_READER_FRACTION_PROPORTION_MAX"]),
            character_gap_pt=Decimal(reader["GV_READER_FRACTION_CHARACTER_GAP_PT"]),
            turned_aspect_min=Decimal(reader["GV_READER_FRACTION_TURNED_ASPECT_MIN"]),
        ),
    )


def _overlaps(first: Sequence[int], second: Sequence[int]) -> bool:
    return (
        first[0] <= second[2]
        and second[0] <= first[2]
        and first[1] <= second[3]
        and second[1] <= first[3]
    )


def _grown(box: tuple[int, int, int, int], by: int) -> tuple[int, int, int, int]:
    return (box[0] - by, box[1] - by, box[2] + by, box[3] + by)


def _page(
    pdf: bytes, index: int, *, dpi: int, geometry: PageGeometry, reach: LabelReach
) -> ScorecardPage:
    return build_pages(
        pdf, [index], version_id=UUID(int=778), dpi=dpi, geometry=geometry, reach=reach
    )[index]


def regions(page: ScorecardPage, reader: dict[str, str]) -> list[tuple[int, int, int, int]]:
    """Every region the stage plans on the page with a dimension line near it — the key's pool."""
    plan = plan_reads(
        page.layers,
        proximity_limit=Decimal(reader["GV_READER_PROXIMITY_LIMIT"]),
        minimum_paths=int(reader["GV_READER_LOCALIZED_MINIMUM_PATHS"]),
        maximum_span=Decimal(reader["GV_READER_LOCALIZED_MAXIMUM_SPAN"]),
    )
    detected = detect(
        page.layers.drawing_segments,
        witness_tolerance=Decimal(reader["GV_READER_WITNESS_TOLERANCE"]),
        minimum_span=Decimal(reader["GV_READER_MINIMUM_SPAN"]),
        straightness=Decimal(reader["GV_READER_STRAIGHTNESS"]),
        crossing_margin=Decimal(reader["GV_READER_CROSSING_MARGIN"]),
    )
    lines = tuple(line.extent for line in detected.lines)
    found = []
    for entry in plan.to_read:
        if not lines_within(
            entry.region.extent,
            lines,
            proximity_limit=Decimal(reader["GV_READER_PROXIMITY_LIMIT"]),
        ):
            continue
        xs = [point.x for point in entry.region.image_extent]
        ys = [point.y for point in entry.region.image_extent]
        box = (min(xs), min(ys), max(xs), max(ys))
        if box[2] > box[0] and box[3] > box[1]:
            found.append(box)
    return found


def stratum(page: ScorecardPage, box: tuple[int, int, int, int], margin: Decimal) -> str:
    facts, _ = page.facts(box, (), margin)
    if facts.rotation_degrees != 0:
        return "sideways"
    if box[3] - box[1] > box[2] - box[0]:
        return "tall"
    return "cut" if facts.cut_at_edge else "whole"


def _wide_view(page: ScorecardPage, box: tuple[int, int, int, int], margin: Decimal) -> bytes:
    """The region with `WIDE_VIEW_PT` of drawing round it, and the crop's own edge drawn in red."""
    rendered = page.rendered
    polygon = page.polygon(box)
    assert polygon is not None
    crop = crop_pixel_box(
        rendered, CropSpec(polygon=polygon, context_margin_pt=margin, dpi=rendered.dpi)
    )
    left, top, right, bottom = crop_pixel_box(
        rendered, CropSpec(polygon=polygon, context_margin_pt=WIDE_VIEW_PT, dpi=rendered.dpi)
    )
    stride = rendered.width_px * 3
    rows = []
    for y in range(top, bottom):
        row = bytearray(rendered.rgb_bytes[y * stride + left * 3 : y * stride + right * 3])
        if crop[1] <= y < crop[3]:
            edge_row = y in (crop[1], crop[3] - 1)
            for x in range(crop[0], crop[2]):
                if edge_row or x in (crop[0], crop[2] - 1):
                    row[(x - left) * 3 : (x - left) * 3 + 3] = b"\xdd\x11\x11"
        rows.append(bytes(row))
    return encode_png(right - left, bottom - top, b"".join(rows))


def ocr_shows_a_number(png: bytes) -> bool:
    """Whether the local OCR engine finds a numeral in a crop, as it stands or turned either way."""
    from evidence.crop import decode_rgb_png
    from extraction.ocr import RapidOcrEngine, could_be_a_reading
    from extraction.vector_first import upright_png

    global _ENGINE
    if _ENGINE is None:
        _ENGINE = RapidOcrEngine()
    for image in (
        png,
        upright_png(png, label_rotation_degrees=90),
        upright_png(png, label_rotation_degrees=270),
    ):
        width, height, rgb = decode_rgb_png(image)
        if any(
            could_be_a_reading(item.text) for item in _ENGINE.read(rgb, width=width, height=height)
        ):
            return True
    return False


_ENGINE: Any = None


def _crop_png(
    page: ScorecardPage, box: tuple[int, int, int, int], store: ArtifactStore, margin: Decimal
) -> bytes:
    from workflow.reading_agent import RegionCrops

    polygon = page.polygon(box)
    assert polygon is not None
    crops = RegionCrops(
        store=store,
        render=page.render,
        base=page.rendered,
        polygon=polygon,
        margin_pt=margin,
        sharper_dpi=page.rendered.dpi + 1,
        whole_run=None,
        rotation_degrees=0,
        stacked=lambda _polygon: False,
        layouts=lambda _polygon: (),
    )
    first = crops.first()
    assert first is not None
    return crops.png(first)


def _write_crop(
    out: Path, crop: CheckCrop, page: ScorecardPage, store: ArtifactStore, margin: Decimal
) -> None:
    """The crop as the reader is shown it, cut by the stage's own code, and the wider view."""
    (out / f"{crop.crop_id}.png").write_bytes(_crop_png(page, crop.region, store, margin))
    (out / f"{crop.crop_id}_wide.png").write_bytes(_wide_view(page, crop.region, margin))


def sheet(
    arguments: argparse.Namespace, *, shows_a_number: Callable[[bytes], bool] | None = None
) -> int:
    import pypdfium2 as pdfium  # type: ignore[import-untyped]

    from storage.local import LocalStore
    from workflow.stages import VISION_CROP_CONTEXT_MARGIN_PT

    out: Path = arguments.out
    if "data" not in out.resolve().parts:
        raise CheckError("the output directory must be under data/, which is never committed")
    if out.exists() and any(out.iterdir()):
        raise CheckError(f"{out} is not empty; a re-run must not renumber a sheet being checked")
    pdf = arguments.pdf.read_bytes()
    reader = read_settings(arguments.reader_settings)
    geometry = _geometry(reader)
    reach = LabelReach(arguments.label_gap_pt, arguments.max_label_pt, geometry.glyph_gap_pt)
    try:
        key_dpi = key_polygon_dpi(arguments.key, polygon_dpi=arguments.key_dpi)
    except ModelBakeoffError as error:
        raise CheckError(str(error)) from error
    scale = Fraction(arguments.stage_dpi, key_dpi)
    exclude: dict[int, list[tuple[int, ...]]] = {}
    for key_crop in load_key(arguments.key):
        exclude.setdefault(key_crop.page_index, []).append(
            tuple(int(value * scale) for value in key_crop.crop_px)
        )
    margin = VISION_CROP_CONTEXT_MARGIN_PT
    margin_px = int(Fraction(margin) * arguments.stage_dpi / 72) + 1

    number = shows_a_number or ocr_shows_a_number
    pool: dict[str, list[CheckCrop]] = {name: [] for name in STRATA}
    left_out = 0
    no_number = 0
    with tempfile.TemporaryDirectory() as directory:
        scratch = LocalStore(root=Path(directory), ticket_secret=b"check crops are never served")
        for index in range(len(pdfium.PdfDocument(pdf))):
            page = _page(pdf, index, dpi=arguments.stage_dpi, geometry=geometry, reach=reach)
            for box in regions(page, reader):
                if any(_overlaps(_grown(box, margin_px), key) for key in exclude.get(index, [])):
                    left_out += 1
                    continue
                if not number(_crop_png(page, box, scratch, margin)):
                    no_number += 1
                    continue
                name = stratum(page, box, margin)
                pool[name].append(CheckCrop("", index, box, name))
            del page

    rng = random.Random(arguments.seed)
    drawn: list[CheckCrop] = []
    for name in STRATA:
        rng.shuffle(pool[name])
        drawn += pool[name][: arguments.per_stratum]
    rng.shuffle(drawn)
    chosen = [
        CheckCrop(f"c{number:02d}", crop.page_index, crop.region, crop.stratum)
        for number, crop in enumerate(drawn)
    ]

    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        store = LocalStore(root=Path(directory), ticket_secret=b"check crops are never served")
        for index in sorted({crop.page_index for crop in chosen}):
            page = _page(pdf, index, dpi=arguments.stage_dpi, geometry=geometry, reach=reach)
            for crop in chosen:
                if crop.page_index == index:
                    _write_crop(out, crop, page, store, margin)
            del page

    with (out / CROPS_CSV).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "crop_id",
                "page",
                "left_px",
                "top_px",
                "right_px",
                "bottom_px",
                "label",
                "sideways",
                "cut_off",
                "note",
            ]
        )
        for crop in chosen:
            writer.writerow([crop.crop_id, crop.page_index + 1, *crop.region, "", "", "", ""])
    (out / GEOMETRY_JSON).write_text(
        json.dumps(
            {
                "stage_dpi": arguments.stage_dpi,
                "seed": arguments.seed,
                "label_gap_pt": str(arguments.label_gap_pt),
                "left_out_touching_the_key": left_out,
                "left_out_showing_no_number": no_number,
                "pool": {name: len(pool[name]) for name in STRATA},
                "drawn_from": {crop.crop_id: crop.stratum for crop in chosen},
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    (out / HOW_TO).write_text(HOW_TO_TEXT, encoding="utf-8")
    cards = "\n".join(
        f"<figure><figcaption>{html.escape(crop.crop_id)} · page {crop.page_index + 1}"
        f'</figcaption><img src="{crop.crop_id}.png" alt=""><img src="{crop.crop_id}_wide.png" '
        'alt=""></figure>'
        for crop in chosen
    )
    (out / SHEET).write_text(
        "<!doctype html><meta charset=utf-8><title>Geometry Check</title><style>"
        "body{font-family:system-ui,sans-serif;margin:24px}figure{display:inline-block;margin:8px;"
        "border:1px solid #ccc;padding:8px;vertical-align:top}img{height:140px;margin:4px;"
        "image-rendering:pixelated}</style><h1>Geometry check (#778)</h1>"
        "<p>For each crop: does the label run sideways, and does it carry on past the red box? "
        "Answer yes or no in crops.csv.</p>" + cards,
        encoding="utf-8",
    )
    counts = {name: len(pool[name]) for name in STRATA}
    print(
        f"{len(chosen)} crops written to {out}; left out {left_out} regions touching a key crop "
        f"and {no_number} showing no number; pool {counts}",
        file=sys.stderr,
    )
    return 0


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _boxes(paths: Sequence[VectorPath]) -> list[Box]:
    return [
        (
            min(x for x, _ in path.points),
            min(y for _, y in path.points),
            max(x for x, _ in path.points),
            max(y for _, y in path.points),
        )
        for path in paths
        if path.points
    ]


def _region_box_pt(page: ScorecardPage, box: tuple[int, int, int, int]) -> Box:
    assert page.transform is not None
    points = [
        page.transform.to_pdf(ImagePoint(x=x, y=y)) for x, y in ((box[0], box[1]), (box[2], box[3]))
    ]
    return (
        min(point.x for point in points),
        min(point.y for point in points),
        max(point.x for point in points),
        max(point.y for point in points),
    )


def longest_run_up(page: ScorecardPage, box: tuple[int, int, int, int], reach: LabelReach) -> bool:
    """The alternative sideways rule, fixed before any answer was read: the label's longest run of
    characters goes up the page. Every sideways label on this drawing has pieces side by side — an
    inch mark's ticks, a fraction's parts — so the current rule's "no run across" can seldom hold.
    """
    region_box = _region_box_pt(page, box)
    glyphs = page.layers.glyph_paths
    seeds = [
        path
        for path, path_box in zip(glyphs, _boxes(glyphs), strict=True)
        if _overlaps(path_box, region_box)  # type: ignore[arg-type]
    ]
    if not seeds:
        return False
    members, _ = gather_label(seeds, glyphs, settings=reach)
    boxes = _boxes(members)
    across, _ = glyph_runs(boxes, reach.glyph_gap_pt, 0)
    up, _ = glyph_runs(boxes, reach.glyph_gap_pt, 90)
    longest_across = max((len(run) for run in across), default=0)
    longest_up = max((len(run) for run in up), default=0)
    return longest_up >= 2 and longest_up > longest_across


def _yes(answer: str) -> bool:
    return answer.strip().lower() == "yes"


def score(arguments: argparse.Namespace) -> int:
    from workflow.stages import VISION_CROP_CONTEXT_MARGIN_PT

    out: Path = arguments.out
    with (out / CROPS_CSV).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    unanswered = [
        row["crop_id"]
        for row in rows
        if any(row[column].strip().lower() not in ANSWERS for column in ANSWER_COLUMNS)
    ]
    if unanswered:
        raise CheckError(
            f"{len(unanswered)} of {len(rows)} crops are not answered yes or no in every column: "
            + ", ".join(unanswered[:12])
        )
    # **Scored on labels only.** A crop with no label in it says nothing about reading a label's
    # direction or extent; what the geometry calls on those is reported apart, as false alarms that
    # would send the agent to look at a drawing symbol.
    labelled = [row for row in rows if _yes(row["label"])]
    recorded = json.loads((out / GEOMETRY_JSON).read_text(encoding="utf-8"))
    pdf = arguments.pdf.read_bytes()
    reader = read_settings(arguments.reader_settings)
    geometry = _geometry(reader)
    margin = VISION_CROP_CONTEXT_MARGIN_PT
    tallies: dict[str, Counter[str]] = {}
    on_symbols: Counter[str] = Counter()

    def count(rule: str, said: bool, person: bool, *, label: bool) -> None:
        if not label:
            on_symbols[rule] += said
            return
        tally = tallies.setdefault(rule, Counter())
        tally["agree" if said == person else ("missed" if person else "false alarm")] += 1

    for gap in LABEL_GAPS_PT:
        reach = LabelReach(Decimal(gap), arguments.max_label_pt, geometry.glyph_gap_pt)
        for index in sorted({int(row["page"]) - 1 for row in rows}):
            page = _page(pdf, index, dpi=int(recorded["stage_dpi"]), geometry=geometry, reach=reach)
            for row in rows:
                if int(row["page"]) - 1 != index:
                    continue
                box = (
                    int(row["left_px"]),
                    int(row["top_px"]),
                    int(row["right_px"]),
                    int(row["bottom_px"]),
                )
                facts, _ = page.facts(box, (), margin)
                label = _yes(row["label"])
                count(
                    f"cut off, label gap {gap} pt",
                    facts.cut_at_edge,
                    _yes(row["cut_off"]),
                    label=label,
                )
                if gap == LABEL_GAPS_PT[0]:
                    sideways = _yes(row["sideways"])
                    count(
                        "sideways, current rule",
                        facts.rotation_degrees != 0,
                        sideways,
                        label=label,
                    )
                    count(
                        "sideways, longest run up",
                        longest_run_up(page, box, reach),
                        sideways,
                        label=label,
                    )
            del page

    sideways_yes = sum(_yes(row["sideways"]) for row in labelled)
    cut_yes = sum(_yes(row["cut_off"]) for row in labelled)
    symbols = len(rows) - len(labelled)
    lines = [
        f"## Geometry check against a person (#778), {len(rows)} crops outside the key",
        "",
        (
            f"{len(labelled)} show a label; on those the person answered sideways on "
            f"{sideways_yes} and cut off on {cut_yes}. {symbols} show no label."
        ),
        "",
        "| Rule | Agrees with the person | Missed | False alarm | Fired on a crop with no label |",
        "|---|---|---|---|---|",
    ]
    for rule in dict.fromkeys([*tallies, *on_symbols]):
        tally = tallies.get(rule, Counter())
        lines.append(
            f"| {rule} | {tally['agree']} | {tally['missed']} | {tally['false alarm']} | "
            f"{on_symbols[rule]} of {symbols} |"
        )
    text = "\n".join(lines) + "\n"
    if arguments.output is not None:
        arguments.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("sheet", help="write crops for a person to check")
    make.add_argument("pdf", type=Path)
    make.add_argument("--key", type=Path, required=True, help="the key whose crops are left out")
    make.add_argument(
        "--key-dpi",
        type=int,
        help="only for a key that does not record its frame (#835); refused if it differs",
    )
    make.add_argument("--reader-settings", type=Path, required=True)
    make.add_argument("--stage-dpi", type=int, required=True)
    make.add_argument("--label-gap-pt", type=Decimal, required=True)
    make.add_argument("--max-label-pt", type=Decimal, required=True)
    make.add_argument("--per-stratum", type=int, required=True)
    make.add_argument("--seed", type=int, required=True)
    make.add_argument("--out", type=Path, required=True)
    make.set_defaults(run=lambda arguments: sheet(arguments))
    check = commands.add_parser("score", help="compare the person's answers with each rule")
    check.add_argument("out", type=Path)
    check.add_argument("--pdf", type=Path, required=True)
    check.add_argument("--reader-settings", type=Path, required=True)
    check.add_argument("--max-label-pt", type=Decimal, required=True)
    check.add_argument("--output", type=Path)
    check.set_defaults(run=score)
    arguments = parser.parse_args(argv)
    try:
        return int(arguments.run(arguments))
    except CheckError as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
