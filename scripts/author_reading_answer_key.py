"""Prepare a crop set so that authoring a reading answer key is reading, not building (#689).

**This script never authors a value.** It produces the crops and the empty columns; a person reads
each crop and types what it says. That division is the point: #666 is admin-owned because the answer
key is only worth having if a human established it, and a key our own pipeline agreed with measures
agreement with ourselves. `eval/experiments/model_bakeoff.py` refuses such a key outright.

Two steps.

    scaffold  DRAWING.pdf --pages 3-5 --out data/goldset/reading-key/ --count 50
              -> numbered PNGs + crops.csv with an empty `value` column

    build     data/goldset/reading-key/ --annotator "..." --on 2026-09-27
              -> answer_key.json + model_bakeoff_metadata.json, verified by loading them back

The crops come from the pipeline's own region finder, so they are the crops the reader will actually
face rather than a separate idea of where text is. The sample is stratified, because a key made only
of legible crops measures nothing that matters.

**Nothing it writes belongs in the repository.** The output directory lives under `data/`, which is
gitignored, and `tests/test_repo_hygiene.py` asks git rather than reading this file.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

from eval.experiments.model_bakeoff import (
    HARD_CASE_TAGS,
    ModelBakeoffError,
    load_crops,
    render_crop,
)
from extraction.annotations import read_annotation_layers
from extraction.rasterise import VISION_CROP_DPI
from extraction.vector_first import plan_reads
from units.normalise import UnitNormalisationError, normalise_to_inches

CROPS_CSV: Final = "crops.csv"
ANSWER_KEY: Final = "answer_key.json"
BAKEOFF_METADATA: Final = "model_bakeoff_metadata.json"
HOW_TO: Final = "HOW_TO_READ_THESE.md"

#: Longest axis, in pixels at the crop DPI, below which a region is a small glyph rather than a
#: label. Measured, not chosen: on `AI_Set_2.pdf` the median planned region is 37px and a legible
#: dimension label runs past 100px, so the boundary separates the two populations rather than
#: splitting one. It is a sampling aid only — it decides which crops a person is shown, never what
#: any of them says.
SMALL_GLYPH_PX: Final = 60

#: The reader thresholds this script plans with. Arguments rather than defaults for the reason
#: `extraction/geometry/text_association.py` gives at length: they are empirical, one sheet cannot
#: fix them, and a default in a script is how today's guess becomes tomorrow's ground truth. These
#: are the values `--help` quotes for AI_Set 2, so a run can be repeated and not so it can be assumed.
DEFAULT_THRESHOLDS: Final = {
    "line_minimum_pt": "12",
    "glyph_maximum_pt": "12",
    "glyph_gap_pt": "2.5",
    "proximity_limit": "0.01",
    "minimum_paths": 2,
    "maximum_span": "0.05",
}


class ScaffoldError(Exception):
    """The crop set could not be prepared, or the filled sheet could not be trusted."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """One region a person will be asked to read."""

    crop_id: str
    page: int
    """One-based, as `GoldObservation` requires."""

    polygon: tuple[int, int, int, int]
    stratum: str

    @property
    def width_px(self) -> int:
        return self.polygon[2] - self.polygon[0]

    @property
    def height_px(self) -> int:
        return self.polygon[3] - self.polygon[1]


def _candidates(pdf: bytes, *, page_index: int, thresholds: dict[str, object]) -> list[Candidate]:
    """Every region the reader would plan on one page, as boxes at the crop DPI."""
    layers = read_annotation_layers(
        pdf,
        page_index,
        document_version_id=uuid4(),
        dpi=VISION_CROP_DPI,
        line_minimum_pt=Decimal(str(thresholds["line_minimum_pt"])),
        glyph_maximum_pt=Decimal(str(thresholds["glyph_maximum_pt"])),
        glyph_gap_pt=Decimal(str(thresholds["glyph_gap_pt"])),
    )
    plan = plan_reads(
        layers,
        proximity_limit=Decimal(str(thresholds["proximity_limit"])),
        minimum_paths=int(str(thresholds["minimum_paths"])),
        maximum_span=Decimal(str(thresholds["maximum_span"])),
    )
    found: list[Candidate] = []
    for index, entry in enumerate(plan.to_read):
        points = entry.region.image_extent
        left = min(point.x for point in points)
        right = max(point.x for point in points)
        top = min(point.y for point in points)
        bottom = max(point.y for point in points)
        if right <= left or bottom <= top:
            continue
        long_axis = max(right - left, bottom - top)
        found.append(
            Candidate(
                crop_id=f"p{page_index + 1}-r{index:04d}",
                page=page_index + 1,
                polygon=(left, top, right, bottom),
                stratum="small_glyph" if long_axis < SMALL_GLYPH_PX else "label",
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


def _write_sheet(out: Path, candidates: list[Candidate]) -> None:
    """The sheet a person fills in. Two empty columns and nothing else to decide."""
    with (out / CROPS_CSV).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "crop_id",
                "image",
                "page",
                "left_px",
                "top_px",
                "right_px",
                "bottom_px",
                "width_px",
                "height_px",
                "value",
                "unreadable",
                "rotated",
                "note",
            ]
        )
        for candidate in candidates:
            writer.writerow(
                [
                    candidate.crop_id,
                    f"{candidate.crop_id}.png",
                    candidate.page,
                    *candidate.polygon,
                    candidate.width_px,
                    candidate.height_px,
                    "",
                    "",
                    "",
                    "",
                ]
            )


_HOW_TO_TEXT: Final = """# Reading these crops

Open each PNG, type what it says in the `value` column of `crops.csv`, and save. That is the whole
task. Nothing here has read them: every value in the file will be one you put there.

**Write it exactly as the drawing writes it, with its unit.**

    28 3/4"        yes
    648 mm         yes
    28.75"         no  — the verdict is exact match, so a rounded answer is a wrong answer
    28 3/4         no  — a value with no unit is refused

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

    thresholds = dict(DEFAULT_THRESHOLDS)
    for name in thresholds:
        supplied = getattr(arguments, name, None)
        if supplied is not None:
            thresholds[name] = supplied

    candidates: list[Candidate] = []
    for page_index in _page_indexes(arguments.pages):
        try:
            candidates.extend(_candidates(pdf, page_index=page_index, thresholds=thresholds))
        except Exception as error:  # noqa: BLE001 - one unreadable page must not lose the rest
            print(f"  page {page_index + 1}: skipped ({type(error).__name__}: {error})")

    if not candidates:
        raise ScaffoldError(
            "no regions were planned on those pages. Either the thresholds exclude everything on "
            "this drawing or the pages carry no outlined text; try --help for the values the first "
            "real set was read with."
        )

    chosen = _stratified(candidates, count=arguments.count, seed=arguments.seed)
    out = Path(arguments.out)
    out.mkdir(parents=True, exist_ok=True)

    # **A region that will not render is not a crop.** 29 of 361 planned regions on page 3 of
    # `AI_Set_2.pdf` are one or two pixels across at 600 dpi — a stray path, not a glyph cluster —
    # and PDFium refuses them. Rather than invent a minimum size, the refusal is the criterion: what
    # cannot be rendered cannot be read, and the count is reported rather than absorbed.
    written: list[Candidate] = []
    unrenderable = 0
    for candidate in chosen:
        try:
            image = render_crop(
                pdf,
                page=candidate.page,
                polygon=candidate.polygon,
                polygon_dpi=VISION_CROP_DPI,
            )
        except (ModelBakeoffError, ValueError):
            unrenderable += 1
            continue
        (out / f"{candidate.crop_id}.png").write_bytes(image)
        written.append(candidate)

    if not written:
        raise ScaffoldError(
            f"none of the {len(chosen)} sampled regions could be rendered as a crop. They are "
            "sub-pixel at this DPI, which means the thresholds are finding stray paths rather than "
            "glyph clusters."
        )

    chosen = written
    _write_sheet(out, chosen)
    (out / HOW_TO).write_text(_HOW_TO_TEXT, encoding="utf-8")

    # **Printed, because a thin stratum is worth seeing before anybody reads fifty crops.** A set
    # that turned out to be all small glyphs would still produce a scorecard, and the scorecard
    # would quietly be about one kind of crop.
    counts = Counter(candidate.stratum for candidate in chosen)
    available = Counter(candidate.stratum for candidate in candidates)
    print(f"\n  {len(chosen)} crops written to {out}")
    if unrenderable:
        print(f"    {unrenderable} sampled region(s) were too small to render and were dropped")
    for stratum in sorted(available):
        print(f"    {stratum:<12} {counts.get(stratum, 0):>4} of {available[stratum]} available")
    # **The size spread, because it predicts how the session will go.** A set whose median crop is
    # 37px is a set where most of the reader's time goes on ticking `unreadable` — which is a real
    # measurement of the region finder, and much better learned here than forty crops in.
    widths = sorted(candidate.width_px for candidate in chosen)
    tiny = sum(1 for width in widths if width < 20)
    print(
        f"\n  crop width px: min {widths[0]}, median {widths[len(widths) // 2]}, max {widths[-1]}"
    )
    if tiny:
        print(
            f"    {tiny} of {len(widths)} are under 20px wide. Those are unlikely to be readable;\n"
            "    raising --minimum-paths drops them, at the cost of a much smaller pool."
        )
    print(
        "\n  rotated is NOT assigned here: the geometry reports zero rotation on every region of\n"
        "  the real client set, so it is a column for the reader to tick (#689).\n"
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


def _parsed(raw: str, *, crop_id: str) -> object:
    """One typed value, exactly, or a refusal that names the crop.

    A decimal is refused before `units/` sees it. `normalise_to_inches` accepts `28.75"` happily and
    it is a wrong answer under exact match (Q2): the drawing says `28 3/4"`, and a key that recorded
    the decimal would score a model wrong for reading the drawing right.
    """
    token = raw.strip()
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
            f'{crop_id}: {refused}. Give the value with its unit, for example 25 1/2" or 648 mm.'
        ) from refused


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

    pdf_path = Path(arguments.pdf)
    try:
        pdf = pdf_path.read_bytes()
    except OSError as error:
        raise ScaffoldError(f"could not read {pdf_path}: {error}") from error

    observations: list[dict[str, object]] = []
    tags: dict[str, list[str]] = {}
    unreadable: list[str] = []
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
            f"{len(unreadable)} marked unreadable, {len(empty)} left blank."
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
        json.dumps({"tags": {k: v for k, v in sorted(tags.items())}}, indent=2) + "\n",
        encoding="utf-8",
    )
    # The bake-off renders from the PDF beside the answer key, so it has to be there.
    destination = out / pdf_path.name
    if not destination.exists():
        destination.write_bytes(pdf)

    # **Loaded back before this returns.** A key the bake-off would refuse must fail here, not after
    # somebody has spent an afternoon typing values into it.
    try:
        crops = load_crops(out, polygon_dpi=VISION_CROP_DPI)
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
    for name, default in DEFAULT_THRESHOLDS.items():
        make.add_argument(
            f"--{name.replace('_', '-')}", help=f"reader threshold ({default} on AI_Set 2)"
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
