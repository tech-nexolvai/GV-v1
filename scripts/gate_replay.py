#!/usr/bin/env python3
"""Replay the two-reader agreement gate against a human-read key, reading only (#851).

**No model is called, and nothing is written but the report and its working file.** Readings come
from runs' databases, each read inside a read-only transaction, and from agent scorecard working
files; `--database` and `--scorecard` may each be given more than once. The drawing is read for its
geometry and its coloured markup only, by the stage's own code
(`eval.experiments.agent_scorecard.ScorecardPage`, `extraction.stamp_text`), with every threshold
taken from a `scripts/demo.sh`-style file — none has a default.

    python scripts/gate_replay.py data/goldset/reading-key-2026-09-30 \\
        --key-dpi 600 --key-margin-pt 9 --reader-settings scripts/demo.sh --stage-dpi 300 \\
        --database postgresql+psycopg://gv:gv@localhost:5433/gvfirst \\
        --scorecard data/goldset/reading-key-2026-09-30/agent_scorecard_2026-10-01_783.json \\
        --output data/goldset/reading-key-2026-09-30/gate_replay.md

`--key-dpi` and `--key-margin-pt` are for a key that records no frame (#835): its polygons' dpi, and
how much page each holds round its region — 0 for a key cut round the region itself. The report
holds counts only; the per-crop working file beside it holds the readings, so the output must be
under `data/`, which is never committed.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from collections.abc import Sequence
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.experiments.agent_scorecard import (
    PageGeometry,
    ScorecardError,
    ScorecardPage,
    build_pages,
    load_key,
)
from eval.experiments.gate_replay import (
    Box,
    Facts,
    Reading,
    Region,
    Replay,
    ReplayError,
    StoredRow,
    frame_of,
    group_rows,
    join,
    pixels,
    region_of,
    regions_from_scorecard,
    render_markdown,
    results_json,
    stacked_catch,
)
from evidence.coordinates import ImagePoint
from extraction.agent.geometry import LabelReach
from extraction.glyph_bands import FractionBarGeometry
from extraction.reader import read_page_contents

if TYPE_CHECKING:
    from sqlalchemy import Engine

Pixels = tuple[int, int, int, int]
"""left, top, right, bottom, in a page's pixels at the stage's dpi."""

#: The reading agent's label lengths, which `scripts/glyph_inventory.READER_SETTINGS` does not read.
#: Required like the rest: they decide where a label ends, so whether a crop cut it.
AGENT_SETTINGS: Final = ("GV_AGENT_LABEL_GAP_PT", "GV_AGENT_MAX_LABEL_PT")

#: Every stored reading of one drawing, with where it was recorded. Selects nothing it does not use.
ROWS_SQL: Final = """
    SELECT p.index, oc.polygon, er.dpi, er.extractor, er.extractor_version, oc.raw_text,
           oc.value_numerator, oc.value_denominator, oc.unit, oc.ambiguity_flags
      FROM observation_candidates oc
      JOIN extraction_runs er ON er.id = oc.extraction_run_id
      JOIN pages p ON p.id = oc.page_id
     WHERE oc.document_version_id = :version
     ORDER BY p.index, oc.created_at, oc.id
"""

VERSIONS_SQL: Final = """
    SELECT dv.id
      FROM document_versions dv
     WHERE dv.sha256 = :sha256
       AND EXISTS (SELECT 1 FROM observation_candidates oc WHERE oc.document_version_id = dv.id)
"""


def read_settings(path: Path) -> dict[str, str]:
    """The reader settings and the agent's label lengths; every one required."""
    from scripts.glyph_inventory import InventoryError, read_reader_settings

    try:
        found = read_reader_settings(path)
    except InventoryError as error:
        raise ReplayError(str(error)) from error
    text = path.read_text(encoding="utf-8")
    for name in AGENT_SETTINGS:
        match = re.search(rf"^\s*{name}=(\S+)", text, flags=re.MULTILINE)
        if match is None:
            raise ReplayError(f"{path} does not state {name}, and it has no default")
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


def coloured_text(pdf: bytes, page_index: int, *, version_id: UUID, dpi: int) -> tuple[Pixels, ...]:
    """Where the page's pasted drawings set text in colour, in its pixels at `dpi`.

    Read by the stamp-text route's own reader, keeping only the characters `stamp_text.drawing_ink`
    refuses. That module never reads them as values, because coloured text inside a snapshot is
    somebody's markup (measured on the client's first set); a vision reader is still shown it.
    """
    from extraction.stamp_text import drawing_ink, stamps_only

    contents = read_page_contents(
        stamps_only(pdf, page_index),
        page_index,
        document_version_id=version_id,
        dpi=dpi,
        keep_char=lambda char: not drawing_ink(char),
    )
    extents = [item.image_extent for item in contents.texts] + [
        label.image_extent for label in contents.set_aside
    ]
    return tuple(
        (
            min(point.x for point in extent),
            min(point.y for point in extent),
            max(point.x for point in extent),
            max(point.y for point in extent),
        )
        for extent in extents
        if extent
    )


def _overlaps(first: Sequence[int], second: Sequence[int]) -> bool:
    return (
        first[0] <= second[2]
        and second[0] <= first[2]
        and first[1] <= second[3]
        and second[1] <= first[3]
    )


def _shows_colour(page: ScorecardPage, text: Sequence[Pixels], crop_px: Pixels) -> bool:
    """Whether markup drawn in colour lies in the crop, wholly or in part.

    Two places it can be, each found by the stage's own test of what is the vendor's black or grey:
    text set in colour (`coloured_text`), and a glyph-sized path drawn in colour
    (`VectorPath.drawing_ink`, #834). A page with no transform places no path, and its paths then
    say nothing, as the stage's own geometry says nothing there.
    """
    if any(_overlaps(box, crop_px) for box in text):
        return True
    if page.transform is None:
        return False
    left, top, right, bottom = crop_px
    corners = [page.transform.to_pdf(ImagePoint(x, y)) for x, y in ((left, top), (right, bottom))]
    low_x, high_x = min(c.x for c in corners), max(c.x for c in corners)
    low_y, high_y = min(c.y for c in corners), max(c.y for c in corners)
    for path in page.layers.glyph_paths:
        if path.drawing_ink or not path.points:
            continue
        xs = [point[0] for point in path.points]
        ys = [point[1] for point in path.points]
        if min(xs) <= high_x and low_x <= max(xs) and min(ys) <= high_y and low_y <= max(ys):
            return True
    return False


def facts_of(
    page: ScorecardPage, text: Sequence[Pixels], box_px: Pixels, margin_pt: Decimal
) -> Facts:
    """A region's geometry, by `workflow.stages.region_facts` through the scorecard's page, and
    whether the crop production cuts round it shows markup in colour."""
    from workflow.reading_agent import crop_box_px

    found, _ = page.facts(box_px, (), margin_pt)
    polygon = page.polygon(box_px)
    return Facts(
        cut_at_edge=found.cut_at_edge,
        sideways=found.rotation_degrees != 0,
        stacked=found.stacked_fraction,
        gv_mark=polygon is not None
        and _shows_colour(page, text, crop_box_px(page.rendered, polygon, margin_pt)),
    )


def only_version(versions: Sequence[object]) -> object:
    """The one document version whose readings are replayed; never a choice among several.

    Two uploads of one drawing are two runs, and their regions mixed would be judged as one.
    """
    if len(versions) != 1:
        raise ReplayError(
            f"{len(versions)} document versions with readings have the key's content hash; the "
            "replay needs exactly one, or regions from different runs would be mixed"
        )
    return versions[0]


def stored_rows(engine: Engine, sha256: str) -> list[StoredRow]:
    """Every reading a run stored for the drawing with this content hash, in one read-only pass."""
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    from units.measurement import Measurement, Unit

    try:
        with engine.connect() as connection:
            # First in the transaction, so nothing after it can write.
            connection.execute(text("SET TRANSACTION READ ONLY"))
            versions = connection.execute(text(VERSIONS_SQL), {"sha256": sha256}).scalars().all()
            rows = connection.execute(text(ROWS_SQL), {"version": only_version(versions)}).all()
    except SQLAlchemyError as error:
        raise ReplayError(f"the database could not be read: {type(error).__name__}") from error
    readings: list[StoredRow] = []
    for (
        page_index,
        polygon,
        dpi,
        extractor,
        version,
        raw,
        numerator,
        denominator,
        unit,
        flags,
    ) in rows:
        if dpi is None:
            raise ReplayError("a stored reading records no resolution, so it cannot be placed")
        readings.append(
            StoredRow(
                page_index=int(page_index),
                polygon=tuple((int(x), int(y)) for x, y in polygon),
                dpi=int(dpi),
                reading=Reading(
                    extractor=str(extractor),
                    extractor_version=str(version),
                    raw_text=str(raw),
                    value=(
                        None
                        if numerator is None
                        else Measurement(Fraction(numerator, denominator), Unit(unit), str(raw))
                    ),
                    flags=tuple(str(flag) for flag in flags),
                ),
            )
        )
    return readings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("key", type=Path, help="a key directory: answer_key.json, crops.csv, PDF")
    parser.add_argument("--key-dpi", type=int, help="the key's polygon dpi, if it records none")
    parser.add_argument(
        "--key-margin-pt",
        type=Decimal,
        help="the key's margin round each region, if it records none",
    )
    parser.add_argument("--reader-settings", type=Path, required=True)
    parser.add_argument("--stage-dpi", type=int, required=True, help="the stage's render")
    parser.add_argument(
        "--database", action="append", default=[], help="a run's database URL; read, never written"
    )
    parser.add_argument("--scorecard", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    from extraction.models.nova import VISION_READERS
    from workflow.stages import VISION_CROP_CONTEXT_MARGIN_PT

    margin = VISION_CROP_CONTEXT_MARGIN_PT
    try:
        if "data" not in args.output.resolve().parts:
            raise ReplayError("the output must be under data/: the working file holds readings")
        if not args.database and not args.scorecard:
            raise ReplayError("name a source of readings: --database, --scorecard, or both")
        crops = load_key(args.key)
        frame = frame_of(args.key, polygon_dpi=args.key_dpi, margin_pt=args.key_margin_pt)
        case = json.loads((args.key / "answer_key.json").read_text(encoding="utf-8"))
        pdf = (args.key / case["shop"]).read_bytes()
        document = case["provenance"]["documents"][0]
        sha256 = str(document["content_hash"]).removeprefix("sha256:")
        settings = read_settings(args.reader_settings)
        geometry = _geometry(settings)
        reach = LabelReach(
            Decimal(settings["GV_AGENT_LABEL_GAP_PT"]),
            Decimal(settings["GV_AGENT_MAX_LABEL_PT"]),
            geometry.glyph_gap_pt,
        )
        version_id = UUID(document["document_version_id"])
        pages = build_pages(
            pdf,
            [crop.page_index for crop in crops],
            version_id=version_id,
            dpi=args.stage_dpi,
            geometry=geometry,
            reach=reach,
        )
        colour = {
            index: coloured_text(pdf, index, version_id=version_id, dpi=args.stage_dpi)
            for index in pages
        }

        def geometry_of(page_index: int, box: Box) -> Facts:
            return facts_of(
                pages[page_index], colour[page_index], pixels(box, args.stage_dpi), margin
            )

        key_facts = {
            crop.crop_id: geometry_of(crop.page_index, region_of(crop, frame)) for crop in crops
        }
        replays: list[Replay] = []
        for url in args.database:
            from sqlalchemy import create_engine
            from sqlalchemy.engine import make_url

            engine = create_engine(url)
            try:
                regions = group_rows(stored_rows(engine, sha256))
            finally:
                engine.dispose()
            joined = join(crops, regions, frame=frame, view_margin_pt=margin)
            seen: dict[str, Region] = {
                region.handle: region for found in joined.values() for region in found
            }
            replays.append(
                Replay(
                    source=f"stored readings: database {make_url(url).database}",
                    crops=crops,
                    regions=joined,
                    facts={
                        handle: geometry_of(region.page_index, region.box)
                        for handle, region in seen.items()
                    },
                )
            )
        model_ids = {reader.extractor: reader.model_id for reader in VISION_READERS}
        for path in args.scorecard:
            rows: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
            replays.append(
                Replay(
                    source=f"the reader pair in {path.name}",
                    crops=crops,
                    regions=regions_from_scorecard(
                        rows, crops, frame=frame, stage_dpi=args.stage_dpi, model_ids=model_ids
                    ),
                    facts=key_facts,
                )
            )
    except (ReplayError, ScorecardError, OSError, KeyError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2

    header = (
        f"## Gate replay — {args.key.name}, "
        f"{datetime.datetime.now().astimezone().date().isoformat()}\n\n"
        f"Key frame: {frame.polygon_dpi} dpi, {frame.margin_pt} pt round each region. Geometry "
        f"read by the stage's code at {args.stage_dpi} dpi, thresholds from "
        f"{args.reader_settings}. A stored region joins a crop where its centre lies in the crop "
        f"the person read (the region and {margin} pt). No model was called."
    )
    report = render_markdown(
        header=header,
        crops=crops,
        stacked=stacked_catch(crops, {crop: facts.stacked for crop, facts in key_facts.items()}),
        replays=replays,
    )
    args.output.write_text(report, encoding="utf-8")
    args.output.with_suffix(".json").write_text(
        json.dumps({replay.source: results_json(replay) for replay in replays}, indent=1),
        encoding="utf-8",
    )
    print(f"wrote {args.output} and {args.output.with_suffix('.json')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
