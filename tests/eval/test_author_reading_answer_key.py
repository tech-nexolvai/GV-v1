"""scaffold -> a person types -> build -> the bake-off loads it (#689).

**No value in this file came from the pipeline**, which is the property the whole exercise rests on:
`eval/experiments/model_bakeoff.py` refuses a key our own reader agreed with, because scoring a model
against one measures agreement with ourselves. Here the "person" is the test typing into the sheet.
"""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import pytest

from eval.experiments.model_bakeoff import load_crops
from extraction.rasterise import VISION_CROP_DPI
from scripts.author_reading_answer_key import CROPS_CSV, Candidate, _legible, _stratified, main
from tests.extraction.test_annotations import _appearance, _free_text, _pdf, _stamp


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


@pytest.fixture
def drawing(tmp_path: Path) -> Path:
    path = tmp_path / "shop.pdf"
    path.write_bytes(_drawing_bytes())
    return path


def _scaffold(drawing: Path, out: Path, *extra: str) -> int:
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
            "--proximity-limit",
            "0.5",
            "--minimum-paths",
            "2",
            "--maximum-span",
            "0.5",
            "--glyph-maximum-pt",
            "12",
            "--glyph-gap-pt",
            "2.5",
            "--line-minimum-pt",
            "50",
            *extra,
        ]
    )


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
        polygon=(0, 0, width, height),
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

    assert _scaffold(drawing, out, "--proximity-limit", "0.15") == 0

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

    crops = load_crops(out, polygon_dpi=VISION_CROP_DPI)
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

    crops = load_crops(out, polygon_dpi=VISION_CROP_DPI)
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

    crops = load_crops(out, polygon_dpi=VISION_CROP_DPI)
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
    # person running it sees — so that is what this asserts, rather than reaching past it.
    assert (
        main(
            [
                "scaffold",
                str(blank),
                "--pages",
                "1",
                "--out",
                str(out),
                # Nothing on the page is this small, so no cluster is ever a glyph.
                "--glyph-maximum-pt",
                "0.01",
                "--glyph-gap-pt",
                "2.5",
                "--line-minimum-pt",
                "50",
            ]
        )
        == 2
    )
    assert "no regions were planned" in capsys.readouterr().err
    assert not (out / CROPS_CSV).exists(), "an empty sheet is worse than no sheet"
