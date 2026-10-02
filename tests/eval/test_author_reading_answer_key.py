"""scaffold -> a person types -> build -> the bake-off loads it (#689).

**No value in this file came from the pipeline**, which is the property the whole exercise rests on:
`eval/experiments/model_bakeoff.py` refuses a key our own reader agreed with, because scoring a model
against one measures agreement with ourselves. Here the "person" is the test typing into the sheet.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final
from uuid import uuid4

import pytest

from eval.experiments.model_bakeoff import load_crops
from evidence.crop import decode_rgb_png
from extraction.annotations import OutlinedTextRegion, read_annotation_layers
from extraction.glyph_bands import FractionBarGeometry
from extraction.rasterise import VISION_CROP_DPI
from extraction.vector_first import plan_reads, region_crop
from scripts.author_reading_answer_key import (
    BAKEOFF_METADATA,
    CROPS_CSV,
    Candidate,
    NotASingleValue,
    ScaffoldError,
    _canonical,
    _legible,
    _parsed,
    _reader_settings,
    _stratified,
    main,
)
from tests.extraction.test_annotations import _appearance, _free_text, _pdf, _stamp
from workflow.stages import VISION_CROP_CONTEXT_MARGIN_PT


#: A vendor drawing with one long line and several glyph clusters beside it.
#:
#: Built from `tests/extraction/test_annotations.py`'s own helpers rather than re-invented, for the
#: reason `test_vector_first.py` gives when it does the same: the reader takes its paths from an
#: annotation **appearance stream**, not from page content, and a hand-rolled PDF that drew
#: rectangles on the page finds nothing at all. Reusing the helpers means this test cannot drift
#: from what the reader actually accepts.
def _glyph_clusters(count: int) -> bytes:
    """One detected dimension, then `count` five-stroke clusters spaced along it."""
    strokes = [
        b"1 w 100 550 m 340 550 l S\n",
        b"100 510 m 100 590 l S\n",
        b"340 510 m 340 590 l S\n",
    ]
    for cluster in range(count):
        origin = 110 + cluster * 30
        for stroke in range(5):
            x = origin + stroke * 3
            strokes.append(f"{x} 570 m {x + 2} 574 l S\n".encode())
    return b"".join(strokes)


def _dimension_and_box_noise() -> bytes:
    """Dimension-adjacent glyphs plus more glyphs near a cabinet edge."""
    strokes = [
        *_glyph_clusters(4).splitlines(keepends=True),
        b"100 650 m 340 650 l S\n",
        b"100 650 m 100 720 l S\n",
        b"340 650 m 340 720 l S\n",
    ]
    for cluster in range(8):
        origin = 110 + cluster * 24
        for stroke in range(5):
            x = origin + stroke * 3
            strokes.append(f"{x} 670 m {x + 2} 674 l S\n".encode())
    return b"".join(strokes)


def _drawing_bytes(clusters: int = 6) -> bytes:
    """The `BOTH_LAYERS` shape exactly, with the appearance stream swapped.

    The `/FreeText` annotation is kept even though nothing here reads it: `_stamp` references its
    appearance by **object number**, and dropping an annotation renumbers the file so the stamp
    points at itself. That is a recursion error, not a missing region, and it cost a detour to find.
    """
    return _pdf(
        annotations=[_free_text(), _stamp(appearance_object=7)],
        extra_objects=[_appearance(_glyph_clusters(clusters))],
    )


def _noisy_drawing_bytes() -> bytes:
    return _pdf(
        annotations=[_free_text(), _stamp(appearance_object=7)],
        extra_objects=[_appearance(_dimension_and_box_noise())],
    )


def _whole_pixel_drawing_bytes() -> bytes:
    """The same drawing on a page that is a whole number of pixels at the crop resolution.

    360 x 288 pt is 3000 x 2400 pixels at 600 dpi. See the first #835 test for why that matters.
    """
    return _pdf(
        annotations=[_free_text(), _stamp(appearance_object=7)],
        extra_objects=[_appearance(_glyph_clusters(6))],
        box=b"[0 0 360 288]",
    )


def _reviewed_drawing_bytes() -> bytes:
    """The drawing with a reviewer's note painted solid red inside the first crop, under its label.

    The note takes the `/FreeText` slot `_drawing_bytes` keeps, so the stamp still finds its
    appearance at object 7; the note's own appearance is object 8. The first cluster's paths sit at
    page (60..74, 120..124) pt, so a 9 pt crop round them spans (51..83, 111..133).
    """
    note = (
        b"<< /Type /Annot /Subtype /FreeText /Rect [62 112 80 118] /Contents (46 1/2) "
        b"/DA (/Helv 10 Tf 1 0 0 rg) /AP << /N 8 0 R >> >>"
    )
    return _pdf(
        annotations=[note, _stamp(appearance_object=7)],
        extra_objects=[
            _appearance(_glyph_clusters(6)),
            _appearance(b"1 0 0 rg 0 0 18 6 re f\n", bbox=b"[0 0 18 6]", matrix=b"[1 0 0 1 0 0]"),
        ],
    )


@pytest.fixture
def drawing(tmp_path: Path) -> Path:
    path = tmp_path / "shop.pdf"
    path.write_bytes(_drawing_bytes())
    return path


#: The reader settings for these synthetic pages, in the `NAME=value` form `scripts/demo.sh` states
#: them. Shaped to this drawing and measured on none: the scaffold takes whatever file it is given.
SETTINGS: Final = {
    "GV_READER_LINE_MINIMUM_PT": "50",
    "GV_READER_GLYPH_MAXIMUM_PT": "12",
    "GV_READER_GLYPH_GAP_PT": "2.5",
    "GV_READER_PROXIMITY_LIMIT": "0.5",
    "GV_READER_LOCALIZED_MINIMUM_PATHS": "2",
    "GV_READER_LOCALIZED_MAXIMUM_SPAN": "0.5",
    "GV_READER_WITNESS_TOLERANCE": "0.004",
    "GV_READER_MINIMUM_SPAN": "0.01",
    "GV_READER_STRAIGHTNESS": "0.0005",
    "GV_READER_CROSSING_MARGIN": "0.0005",
    "GV_READER_FRACTION_BAR_THICKNESS_MAX_PT": "0.3",
    "GV_READER_FRACTION_BAR_LENGTH_MIN_PT": "1",
    "GV_READER_FRACTION_REACH_PT": "3",
    "GV_READER_FRACTION_GLYPH_MIN_PT": "1",
    "GV_READER_FRACTION_GLYPH_MAX_PT": "12",
    "GV_READER_FRACTION_PROPORTION_MAX": "2.5",
}


def _settings_file(folder: Path, settings: Mapping[str, str]) -> Path:
    path = folder / "demo.sh"
    path.write_text("".join(f"{name}={value} \\\n" for name, value in settings.items()), "utf-8")
    return path


def _scaffold(
    drawing: Path, out: Path, *extra: str, settings: Mapping[str, str] | None = None
) -> int:
    """`settings` overrides `SETTINGS` in the file, which is the only way to give a threshold."""
    path = _settings_file(drawing.parent, {**SETTINGS, **(settings or {})})
    return main(
        [
            "scaffold",
            str(drawing),
            "--pages",
            "1",
            "--out",
            str(out),
            "--count",
            "6",
            "--reader-settings",
            str(path),
            *extra,
        ]
    )


def _production_regions(pdf: bytes) -> list[OutlinedTextRegion]:
    """The regions production's planner finds on page 1, in its order — what `crop_id` counts."""
    layers = read_annotation_layers(
        pdf,
        0,
        document_version_id=uuid4(),
        dpi=VISION_CROP_DPI,
        line_minimum_pt=Decimal(SETTINGS["GV_READER_LINE_MINIMUM_PT"]),
        glyph_maximum_pt=Decimal(SETTINGS["GV_READER_GLYPH_MAXIMUM_PT"]),
        glyph_gap_pt=Decimal(SETTINGS["GV_READER_GLYPH_GAP_PT"]),
        fraction_bar=FractionBarGeometry(
            bar_thickness_max_pt=Decimal(SETTINGS["GV_READER_FRACTION_BAR_THICKNESS_MAX_PT"]),
            bar_length_min_pt=Decimal(SETTINGS["GV_READER_FRACTION_BAR_LENGTH_MIN_PT"]),
            reach_pt=Decimal(SETTINGS["GV_READER_FRACTION_REACH_PT"]),
            glyph_min_pt=Decimal(SETTINGS["GV_READER_FRACTION_GLYPH_MIN_PT"]),
            glyph_max_pt=Decimal(SETTINGS["GV_READER_FRACTION_GLYPH_MAX_PT"]),
            proportion_max=Decimal(SETTINGS["GV_READER_FRACTION_PROPORTION_MAX"]),
        ),
    )
    plan = plan_reads(
        layers,
        proximity_limit=Decimal(SETTINGS["GV_READER_PROXIMITY_LIMIT"]),
        minimum_paths=int(SETTINGS["GV_READER_LOCALIZED_MINIMUM_PATHS"]),
        maximum_span=Decimal(SETTINGS["GV_READER_LOCALIZED_MAXIMUM_SPAN"]),
    )
    return [entry.region for entry in plan.to_read]


def _region_of(crop_id: str, regions: list[OutlinedTextRegion]) -> OutlinedTextRegion:
    """`p1-r0003` is the fourth region the planner found."""
    return regions[int(crop_id.rpartition("-r")[2])]


def _rows(out: Path) -> list[dict[str, str]]:
    with (out / CROPS_CSV).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _fill(
    out: Path,
    values: list[str],
    *,
    unreadable: frozenset[str] | set[str] = frozenset(),
    rotate: frozenset[str] | set[str] = frozenset(),
) -> None:
    """Stand in for the person: type a value per row, tick the odd box."""
    with (out / CROPS_CSV).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
        fields = list(rows[0])
    for row, value in zip(rows, values, strict=False):
        if row["crop_id"] in unreadable:
            row["unreadable"] = "x"
            continue
        row["value"] = value
        if row["crop_id"] in rotate:
            row["rotated"] = "x"
    with (out / CROPS_CSV).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _build(out: Path, drawing: Path, *extra: str) -> int:
    return main(
        [
            "build",
            str(out),
            "--pdf",
            str(drawing),
            "--annotator",
            "Anant Bisht",
            "--on",
            "2026-09-27",
            *extra,
        ]
    )


def _candidate(crop_id: str, *, stratum: str, width: int = 80, height: int = 20) -> Candidate:
    return Candidate(
        crop_id=crop_id,
        page=1,
        region=(0, 0, width, height),
        crop=(0, 0, width + 75, height + 75),
        stratum=stratum,
        path_count=5,
        line_count=1,
    )


def test_sampling_keeps_rotated_stacked_and_small_glyph_cases() -> None:
    candidates = [
        _candidate("easy-1", stratum="dimension_label"),
        _candidate("easy-2", stratum="dimension_label"),
        _candidate("rotated", stratum="rotated"),
        _candidate("stacked", stratum="stacked_fraction"),
        _candidate("small", stratum="small_glyph", width=40),
    ]

    chosen = _stratified(candidates, count=4, seed=0)

    assert {candidate.stratum for candidate in chosen} >= {
        "rotated",
        "stacked_fraction",
        "small_glyph",
    }


def test_regions_too_small_to_read_are_excluded_before_sampling() -> None:
    kept, dropped = _legible(
        [
            _candidate("speck", stratum="small_glyph", width=12, height=5),
            _candidate("small-but-readable", stratum="small_glyph", width=35, height=8),
        ]
    )

    assert [candidate.crop_id for candidate in kept] == ["small-but-readable"]
    assert dropped == 1


def test_scaffold_writes_crops_and_an_empty_sheet(drawing: Path, tmp_path: Path) -> None:
    """The deliverable is images plus blank columns. Nothing here has read anything."""
    out = tmp_path / "key"

    assert _scaffold(drawing, out) == 0

    rows = list(csv.DictReader((out / CROPS_CSV).open(encoding="utf-8", newline="")))
    assert rows, "no crops were planned on the synthetic page"
    assert all((out / row["image"]).exists() for row in rows)
    assert all((out / row["image"]).read_bytes()[:4] == b"\x89PNG" for row in rows)
    # The two columns a person fills, and nothing pre-filled in either.
    assert all(row["value"] == "" for row in rows)
    assert all(row["unreadable"] == "" for row in rows)
    assert (out / "HOW_TO_READ_THESE.md").exists()
    assert (out / "contact_sheet.html").exists()
    assert "Contact Sheet" in (out / "contact_sheet.html").read_text(encoding="utf-8")


def test_scaffold_samples_regions_next_to_detected_dimension_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cabinet-edge clutter is line-adjacent, but it is not dimension-line-adjacent."""
    drawing = tmp_path / "shop.pdf"
    drawing.write_bytes(_noisy_drawing_bytes())
    out = tmp_path / "key"

    assert _scaffold(drawing, out, settings={"GV_READER_PROXIMITY_LIMIT": "0.15"}) == 0

    rows = list(csv.DictReader((out / CROPS_CSV).open(encoding="utf-8", newline="")))
    assert len(rows) == 4
    assert {row["stratum"] for row in rows} == {"dimension_label"}
    assert all(int(row["near_dimension_lines"]) >= 1 for row in rows)
    assert "Contact sheet:" in capsys.readouterr().out


def test_the_whole_round_trip_ends_in_a_key_the_bakeoff_loads(
    drawing: Path, tmp_path: Path
) -> None:
    """scaffold, type, build — and the bake-off renders the crops back out of it."""
    out = tmp_path / "key"
    _scaffold(drawing, out)
    rows = list(csv.DictReader((out / CROPS_CSV).open(encoding="utf-8", newline="")))
    _fill(out, ['24"', '28 3/4"', '36"', "648 mm", '12 1/2"', '30"'][: len(rows)])

    assert _build(out, drawing) == 0

    crops = load_crops(out)
    assert len(crops) == len(rows)
    assert all(crop.image[:4] == b"\x89PNG" for crop in crops)
    # Exact, and derived from what was typed rather than from any reading.
    assert {str(crop.expected.exact) for crop in crops} >= {"24", "115/4"}


def test_a_decimal_is_refused_and_the_row_is_named(drawing: Path, tmp_path: Path) -> None:
    """Q2 is exact match, so `28.75"` would score a model wrong for reading `28 3/4"` right."""
    out = tmp_path / "key"
    _scaffold(drawing, out)
    _fill(out, ['28.75"'] * 6)

    assert _build(out, drawing) == 2


def test_a_value_with_no_unit_is_refused(drawing: Path, tmp_path: Path) -> None:
    out = tmp_path / "key"
    _scaffold(drawing, out)
    _fill(out, ["28 3/4"] * 6)

    assert _build(out, drawing) == 2


def test_an_unreadable_crop_is_recorded_rather_than_dropped(
    drawing: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A crop no person can read is a crop no model should be trusted on.

    Excluding it silently would shrink the scored set and leave the count only in the difference
    between two numbers nobody compares.
    """
    out = tmp_path / "key"
    _scaffold(drawing, out)
    rows = list(csv.DictReader((out / CROPS_CSV).open(encoding="utf-8", newline="")))
    skipped = {rows[0]["crop_id"]}
    _fill(out, ['24"'] * len(rows), unreadable=skipped)

    assert _build(out, drawing) == 0

    crops = load_crops(out)
    assert len(crops) == len(rows) - 1
    assert "unreadable" in capsys.readouterr().out


def test_the_reader_tick_is_what_makes_a_crop_rotated(drawing: Path, tmp_path: Path) -> None:
    """The geometry cannot assign it: every region of the real client set reports zero rotation.

    `rotated` is the category where two models read the same crop as `60` and as `4`, both
    confidently, so a key that could not carry the tag could not measure the case that matters most.
    """
    out = tmp_path / "key"
    _scaffold(drawing, out)
    rows = list(csv.DictReader((out / CROPS_CSV).open(encoding="utf-8", newline="")))
    _fill(out, ['24"'] * len(rows), rotate={rows[1]["crop_id"]})

    _build(out, drawing)

    crops = load_crops(out)
    assert sum(1 for crop in crops if "rotated" in crop.tags) == 1


def test_provenance_names_the_person_and_cannot_claim_the_pipeline_read_it(
    drawing: Path, tmp_path: Path
) -> None:
    """The four markers the harness refuses must be impossible to end up with by accident.

    A key marked `self-verified` is one our own pipeline established, and scoring a model against it
    measures agreement with ourselves. This asserts the written file carries none of them.
    """
    out = tmp_path / "key"
    _scaffold(drawing, out)
    _fill(out, ['24"'] * 6)
    _build(out, drawing)

    key = json.loads((out / "answer_key.json").read_text(encoding="utf-8"))
    key_text = json.dumps(key).lower()
    for marker in (
        "self-verified",
        "machine-self-verified",
        "not per-case human-read",
        "heuristic-unconfirmed",
    ):
        assert marker not in key_text
    provenance = key["provenance"]
    assert provenance["annotator"] == "Anant Bisht"
    assert provenance["annotated_on"] == date(2026, 9, 27).isoformat()


def test_a_sheet_with_nothing_typed_in_it_is_refused(drawing: Path, tmp_path: Path) -> None:
    """Better than emitting an empty key that the bake-off would score as a perfect run of zero."""
    out = tmp_path / "key"
    _scaffold(drawing, out)

    assert _build(out, drawing) == 2


def test_the_sample_is_the_same_on_a_second_run(drawing: Path, tmp_path: Path) -> None:
    """A person half way through fifty crops must not have them renumbered underneath them."""
    first, second = tmp_path / "a", tmp_path / "b"
    _scaffold(drawing, first)
    _scaffold(drawing, second)

    assert (first / CROPS_CSV).read_text(encoding="utf-8") == (second / CROPS_CSV).read_text(
        encoding="utf-8"
    )


def test_pages_that_plan_nothing_say_so_rather_than_writing_an_empty_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blank = tmp_path / "blank.pdf"
    blank.write_bytes(_drawing_bytes())
    out = tmp_path / "key"

    # `main` turns a ScaffoldError into an exit code and a message on stderr, which is what a
    # person running it sees — so that is what this asserts, rather than reaching past it. Nothing
    # on the page is as small as this glyph maximum, so no cluster is ever a glyph.
    assert _scaffold(blank, out, settings={"GV_READER_GLYPH_MAXIMUM_PT": "0.01"}) == 2
    assert "no regions were planned" in capsys.readouterr().err
    assert not (out / CROPS_CSV).exists(), "an empty sheet is worse than no sheet"


# --- #835: a person reads the crop production cuts, and the key says what frame it is in --------


def _reviewer_red(png: bytes) -> int:
    """Pixels of the reviewer's red note. The wide view's edge is red too, so its exact colour is
    left out; the note is painted pure red."""
    width, height, rgb = decode_rgb_png(png)
    pixels = (rgb[offset : offset + 3] for offset in range(0, width * height * 3, 3))
    return sum(1 for p in pixels if p[0] > 200 and p[1] < 60 and p[2] < 60 and p != b"\xdd\x11\x11")


def test_a_crop_is_the_pixels_production_cuts_round_the_same_region(tmp_path: Path) -> None:
    """**One region, two crops, same pixels.** Each crop the scaffold writes is compared with what
    production's region cropper (`extraction.vector_first.region_crop`) cuts round the same planned
    region, with the stage's vision margin, at `VISION_CROP_DPI`, from the vendor's layer.

    Before #835 the scaffold cut the bare region, and a person read a picture no model is shown.

    **The page is a whole number of pixels at that resolution**, 3000 x 2400. On such a page the two
    agree pixel for pixel. On other sizes production's region cropper, which places a region by its
    share of the page rather than by its pixels, can land a fraction of a pixel away, and the
    renderer can round that into a pixel more or less. So the page is chosen where the pixels can
    show the recipe — region, margin, resolution, layer — and not the renderer's rounding.
    """
    drawing = tmp_path / "shop.pdf"
    drawing.write_bytes(_whole_pixel_drawing_bytes())
    out = tmp_path / "key"

    assert _scaffold(drawing, out) == 0

    pdf = drawing.read_bytes()
    regions = _production_regions(pdf)
    rows = _rows(out)
    assert rows, "no crops were planned on the synthetic page"
    for row in rows:
        production = region_crop(
            pdf,
            0,
            _region_of(row["crop_id"], regions),
            dpi=VISION_CROP_DPI,
            margin_pt=VISION_CROP_CONTEXT_MARGIN_PT,
        )
        scaffolded = (out / row["image"]).read_bytes()
        assert decode_rgb_png(scaffolded) == decode_rgb_png(production), row["crop_id"]


def test_the_bakeoff_shows_a_model_the_very_bytes_the_person_read(
    drawing: Path, tmp_path: Path
) -> None:
    """The bake-off renders each crop again from the PDF, so the two images could drift apart in
    frame, extent or layers — and three times in one run they did. Rendered by one function from
    one polygon in one frame, they cannot."""
    out = tmp_path / "key"
    _scaffold(drawing, out)
    rows = _rows(out)
    _fill(out, ['24"'] * len(rows))
    assert _build(out, drawing) == 0

    crops = load_crops(out)

    assert [crop.image for crop in crops] == [(out / row["image"]).read_bytes() for row in rows]


def test_a_crop_and_its_wide_view_show_the_vendor_drawing_and_not_the_reviewer_note(
    tmp_path: Path,
) -> None:
    """**#742, for the person as well as the model.** A reviewer's number painted into a crop is one
    a person could type as the vendor's. The control proves the note is there to be left out."""
    drawing = tmp_path / "shop.pdf"
    drawing.write_bytes(_reviewed_drawing_bytes())
    out = tmp_path / "key"

    assert _scaffold(drawing, out) == 0

    pdf = drawing.read_bytes()
    regions = _production_regions(pdf)
    rows = _rows(out)
    painted = [
        row["crop_id"]
        for row in rows
        if _reviewer_red(
            region_crop(
                pdf,
                0,
                _region_of(row["crop_id"], regions),
                margin_pt=VISION_CROP_CONTEXT_MARGIN_PT,
                include_markup=True,
            )
        )
    ]
    assert painted, "the control: with the markup left in, some crop shows the note"
    for row in rows:
        assert _reviewer_red((out / row["image"]).read_bytes()) == 0, row["crop_id"]
        assert _reviewer_red((out / row["wide_image"]).read_bytes()) == 0, row["crop_id"]
    width, height, rgb = decode_rgb_png((out / f"{painted[0]}.png").read_bytes())
    assert any(max(rgb[offset : offset + 3]) < 60 for offset in range(0, width * height * 3, 3))


def test_each_crop_has_a_wide_view_with_the_crops_edge_drawn_round_it(
    drawing: Path, tmp_path: Path
) -> None:
    """The wide view is how a person sees that a label runs past the crop. The red line runs on the
    pixels just outside the crop, so it encloses exactly the crop's size and covers none of it."""
    out = tmp_path / "key"
    assert _scaffold(drawing, out) == 0

    rows = _rows(out)
    assert rows
    for row in rows:
        crop_width, crop_height, _ = decode_rgb_png((out / row["image"]).read_bytes())
        width, height, rgb = decode_rgb_png((out / row["wide_image"]).read_bytes())
        edge = [
            (index % width, index // width)
            for index in range(width * height)
            if rgb[index * 3 : index * 3 + 3] == b"\xdd\x11\x11"
        ]
        xs = [x for x, _ in edge]
        ys = [y for _, y in edge]

        assert (width, height) > (crop_width, crop_height)
        assert (max(xs) - min(xs) - 1, max(ys) - min(ys) - 1) == (crop_width, crop_height)
        assert len(edge) == 2 * (crop_width + 2) + 2 * crop_height, "a closed line, one pixel wide"
    sheet = (out / "contact_sheet.html").read_text(encoding="utf-8")
    assert all(row["wide_image"] in sheet for row in rows)


def test_the_key_records_the_frame_its_polygons_are_in(drawing: Path, tmp_path: Path) -> None:
    """The pilot key did not, and read at the wrong frame every crop came from the wrong place. The
    frame is the stage's: the vision crop resolution and the stage's own vision margin."""
    out = tmp_path / "key"
    _scaffold(drawing, out)
    expected = {"polygon_dpi": VISION_CROP_DPI, "margin_pt": str(VISION_CROP_CONTEXT_MARGIN_PT)}
    assert json.loads((out / BAKEOFF_METADATA).read_text(encoding="utf-8"))["frame"] == expected

    _fill(out, ['24"'] * len(_rows(out)))
    assert _build(out, drawing) == 0

    assert json.loads((out / BAKEOFF_METADATA).read_text(encoding="utf-8"))["frame"] == expected


def test_build_carries_the_sheets_own_frame_into_the_key(drawing: Path, tmp_path: Path) -> None:
    """A sheet cut under another margin must not be relabelled with today's: `build` copies what
    `scaffold` recorded beside the crops, rather than the constants it would cut with now."""
    out = tmp_path / "key"
    _scaffold(drawing, out)
    _fill(out, ['24"'] * len(_rows(out)))
    metadata = json.loads((out / BAKEOFF_METADATA).read_text(encoding="utf-8"))
    metadata["frame"]["margin_pt"] = "12"
    (out / BAKEOFF_METADATA).write_text(json.dumps(metadata), encoding="utf-8")

    assert _build(out, drawing) == 0

    recorded = json.loads((out / BAKEOFF_METADATA).read_text(encoding="utf-8"))["frame"]
    assert recorded == {"polygon_dpi": VISION_CROP_DPI, "margin_pt": "12"}


def test_a_sheet_that_records_no_frame_is_not_built(
    drawing: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sheet scaffolded before #835 holds crops no model is shown. Building a key from it would
    have every later loader refuse it, or worse, trust whoever named a frame for it."""
    out = tmp_path / "key"
    _scaffold(drawing, out)
    _fill(out, ['24"'] * len(_rows(out)))
    (out / BAKEOFF_METADATA).unlink()

    assert _build(out, drawing) == 2
    assert "does not record the frame" in capsys.readouterr().err
    assert not (out / "answer_key.json").exists()


def test_every_reader_threshold_comes_from_the_settings_file(
    drawing: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """**Read from one place, never copied** (#835). The scaffold's own copy had drifted from the
    values the worker runs with. A value the file does not state is refused by name, and there is
    no flag left to give one."""
    out = tmp_path / "key"
    partial = {name: value for name, value in SETTINGS.items() if name != "GV_READER_MINIMUM_SPAN"}
    path = _settings_file(tmp_path, partial)

    arguments = ["scaffold", str(drawing), "--pages", "1", "--out", str(out)]

    assert main([*arguments, "--reader-settings", str(path)]) == 2
    assert "GV_READER_MINIMUM_SPAN" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        _scaffold(drawing, out, "--proximity-limit", "0.5")


def test_the_file_the_worker_is_started_with_states_every_threshold() -> None:
    """`scripts/demo.sh` is where the stage's thresholds live. If it stopped stating one, every
    scaffold run would be refused, and this says which."""
    demo = Path(__file__).resolve().parents[2] / "scripts" / "demo.sh"

    assert set(_reader_settings(demo)) == set(SETTINGS)


# --- #730: the notations the client's drawings actually use ------------------------------------


@pytest.mark.parametrize(
    ("typed", "inches"),
    [
        ('39 1/4"', "157/4"),  # plain fraction — worked before
        ('102"', "102"),  # bare whole — worked before
        ("648 mm", "3240/127"),  # millimetres — worked before
        ('25-1/2"', "51/2"),  # hyphenated, how the trade writes it
        ('5-1/4"', "21/4"),
        ("381 [15]", "15"),  # dual unit: the bracketed inch is authoritative (Q12)
        ("724 [28 1/2]", "57/2"),
        ('2" (VIF)', "2"),  # a site note is not part of the number
        ('80-1/2" (VIF)', "161/2"),  # hyphenated *and* annotated
        ("6'-0\"", "72"),  # feet-inches keeps its hyphen
        ("2' - 10\"", "34"),
    ],
)
def test_every_notation_on_the_client_sheets_is_accepted(typed: str, inches: str) -> None:
    """**Input: one typed value. Outcome: the exact inches the drawing means.**

    Before #730 only the first three parsed. The other eight are on the client's own sheets, and a
    person told to "type exactly what the drawing shows" had most of their work refused after the
    fact with *"no unit could be established"* — which `25-1/2"` plainly carries.

    `6'-0"` is here to hold the line the hyphen rule must not cross: a hyphen after a foot mark is
    feet-inches, not a fraction separator.
    """
    from fractions import Fraction

    assert _parsed(typed, crop_id="probe").exact == Fraction(inches)  # type: ignore[attr-defined]


@pytest.mark.parametrize("typed", ['39 1/4"+6"', '48"+3"'])
def test_a_compound_is_refused_as_not_one_value_rather_than_added_up(typed: str) -> None:
    """Adding them up would put *our* arithmetic into the ground truth (#730).

    A reader that returned `39 1/4"+6"` exactly — which is what the drawing says — would then be
    scored wrong against a key that recorded `45 1/4"`. The refusal is its own type so `build` can
    count these apart from crops nobody could read.
    """
    with pytest.raises(NotASingleValue):
        _parsed(typed, crop_id="probe")


def test_a_decimal_is_still_refused_after_the_notation_rewrite() -> None:
    """The rewrite must not open a door for a rounded answer — Q2 is exact match."""
    with pytest.raises(ScaffoldError):
        _parsed('28.75"', crop_id="probe")


def test_the_rewrite_never_computes_a_value() -> None:
    """`_canonical` drops and re-spaces characters; it must never do arithmetic.

    A dual-unit token reports the millimetres it carried so the caller can record them, and returns
    the inch *as written* — it does not convert one into the other. The moment it computes, the key
    stops being a transcription of what a person read.
    """
    token, mm = _canonical("724 [28 1/2]")

    assert token == '28 1/2"'
    assert mm == "724"
