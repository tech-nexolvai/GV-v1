"""The dimension-first sample on synthetic drawings, the sheet's new columns, and several keys (#867).

**No client drawing is read here.** Every drawing is built from `tests/extraction`'s own helpers: a
pasted drawing holding one dimension line, glyph-sized path clusters beside it, and printed text in
Courier — a font whose every character occupies the same box, so two drawings can say different
things in exactly the same places. That is what lets these tests show the sample is built from where
things are and never from what they say.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final
from uuid import uuid4

import pytest

from eval.experiments.agent_scorecard import Kind as KeyKind
from eval.experiments.agent_scorecard import load_key
from eval.experiments.model_bakeoff import ModelBakeoffError, load_crops, load_keys, render_crop
from evidence.crop import decode_rgb_png
from extraction.rasterise import VISION_CROP_DPI
from extraction.stamp_text import read_stamp_text
from scripts.author_reading_answer_key import (
    AGREED_SQL,
    CROPS_CSV,
    FRAME_RECORD,
    PERSON_COLUMNS,
    agreed_boxes,
    main,
)
from tests.eval.test_author_reading_answer_key import SETTINGS, _glyph_clusters
from tests.extraction.test_annotations import _free_text, _pdf, _stamp
from tests.extraction.test_stamp_text import _text_appearance
from workflow.config import READER_RASTER_DPI

COURIER: Final = b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>"

#: The frame's own settings, beside the reader's, as `scripts/demo.sh` states them. Shaped to these
#: synthetic drawings and measured on none.
FRAME_SETTINGS: Final = {
    **SETTINGS,
    "GV_READER_AMBIGUITY_MARGIN": "0.005",
    "GV_AGENT_LABEL_GAP_PT": "4",
    "GV_AGENT_MAX_LABEL_PT": "40",
}

#: What each drawing prints, in the same three places: upright, sideways, upright. The second set
#: says nothing that is a dimension, in exactly the boxes the first set's dimensions occupy; the
#: bracket is the mark of an inch beside its millimetres, which a kind read from the text would see.
DIMENSIONS: Final = (b'24"', b'36"', b"[18]")
WORDS: Final = (b"ABC", b"XYZ", b"DEFG")


#: A second dimension, running up the page, for the sideways run to sit beside: the line and its two
#: witness lines, in the pasted drawing's own space.
UPRIGHT_DIMENSION: Final = b"304 580 m 304 640 l S\n280 580 m 330 580 l S\n280 640 m 330 640 l S\n"


def _drawing(texts: Sequence[bytes] = DIMENSIONS) -> bytes:
    """Two dimension lines, four glyph clusters beside the first, and three printed runs.

    The pasted drawing's appearance space runs 100..400 by 500..700 and lands on the page at
    50..350 by 50..250. The second run is set sideways, turned a quarter by its text matrix, beside
    the dimension that runs up the page.
    """
    upright, sideways, upper = texts
    stream = (
        _glyph_clusters(4)
        + UPRIGHT_DIMENSION
        + b"BT /F1 6 Tf 1 0 0 1 120 620 Tm ("
        + upright
        + b") Tj ET\n"
        + b"BT /F1 6 Tf 0 1 -1 0 300 600 Tm ("
        + sideways
        + b") Tj ET\n"
        + b"BT /F1 6 Tf 1 0 0 1 200 665 Tm ("
        + upper
        + b") Tj ET\n"
    )
    return _pdf(
        annotations=[_free_text(), _stamp(appearance_object=7)],
        extra_objects=[_text_appearance(stream, 8), COURIER],
    )


def _settings_file(folder: Path, settings: Mapping[str, str] = FRAME_SETTINGS) -> Path:
    path = folder / "demo.sh"
    path.write_text("".join(f"{name}={value} \\\n" for name, value in settings.items()), "utf-8")
    return path


#: Quotas for these drawings: every kind stated, as the script requires.
QUOTAS: Final = {
    "stacked": 0,
    "dual": 0,
    "sideways": 1,
    "cut": 0,
    "gv_mark": 0,
    "small": 0,
    "plain": 3,
}


def _sample(
    drawing: Path,
    out: Path,
    *,
    quotas: Mapping[str, int] = QUOTAS,
    prefix: str = "t1",
    settings: Path | None = None,
    workers: int = 1,
) -> int:
    return main(
        [
            "sample",
            str(drawing),
            "--pages",
            "1",
            "--out",
            str(out),
            "--reader-settings",
            str(settings or _settings_file(drawing.parent)),
            "--prefix",
            prefix,
            "--min-glyphs",
            "2",
            "--max-glyphs",
            "12",
            "--height-ratio",
            "1.5",
            "--small-px",
            "60",
            "--site-reach-px",
            "0",
            "--workers",
            str(workers),
            *(item for kind, count in quotas.items() for item in ("--quota", f"{kind}={count}")),
        ]
    )


@pytest.fixture
def drawing(tmp_path: Path) -> Path:
    path = tmp_path / "shop.pdf"
    path.write_bytes(_drawing())
    return path


@pytest.fixture
def out(tmp_path: Path) -> Path:
    return tmp_path / "data" / "goldset" / "key"


def _rows(folder: Path, sheet: str = CROPS_CSV) -> list[dict[str, str]]:
    with (folder / sheet).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_rows(folder: Path, rows: list[dict[str, str]]) -> None:
    with (folder / CROPS_CSV).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _build(folder: Path, drawing: Path, case_id: str = "synthetic-key") -> int:
    return main(
        [
            "build",
            str(folder),
            "--pdf",
            str(drawing),
            "--annotator",
            "A Person",
            "--on",
            "2026-10-03",
            "--case-id",
            case_id,
        ]
    )


# --- the sample --------------------------------------------------------------------------------


def test_each_kind_is_drawn_to_its_quota_and_the_sheet_is_empty_for_a_person(
    drawing: Path, out: Path
) -> None:
    assert _sample(drawing, out) == 0

    rows = _rows(out)
    assert Counter(row["stratum"] for row in rows) == Counter({"sideways": 1, "plain": 3})
    assert all(row[column] == "" for row in rows for column in PERSON_COLUMNS)
    assert all((out / row["image"]).read_bytes()[:4] == b"\x89PNG" for row in rows)
    assert all(re.fullmatch(r"t1-p1-\d{3}", row["crop_id"]) for row in rows)
    record = json.loads((out / FRAME_RECORD).read_text(encoding="utf-8"))
    assert record["written"] == {**dict.fromkeys(QUOTAS, 0), "sideways": 1, "plain": 3}
    assert record["available"]["plain"] >= 3
    assert (out / "HOW_TO_READ_THESE.md").exists()
    assert json.loads((out / "model_bakeoff_metadata.json").read_text(encoding="utf-8"))["frame"]


def test_a_kind_the_frame_holds_too_few_of_is_drawn_short(drawing: Path, out: Path) -> None:
    assert _sample(drawing, out, quotas={**QUOTAS, "sideways": 5}) == 0

    record = json.loads((out / FRAME_RECORD).read_text(encoding="utf-8"))
    assert record["written"]["sideways"] == record["available"]["sideways"] == 1
    assert record["quotas"]["sideways"] == 5


def test_the_sample_is_the_same_whatever_the_drawing_says(tmp_path: Path) -> None:
    """**The sample never selects on a reading.** Two drawings print different things in exactly the
    same places — dimensions in one, words in the other. A frame built from where things are draws
    the same crops from both; one that read the text would keep the dimensions and drop the words.
    """
    samples = []
    for name, texts in (("dimensions", DIMENSIONS), ("words", WORDS)):
        folder = tmp_path / name
        folder.mkdir()
        path = folder / "shop.pdf"
        path.write_bytes(_drawing(texts))
        out = tmp_path / "data" / name
        assert _sample(path, out) == 0
        record = json.loads((out / FRAME_RECORD).read_text(encoding="utf-8"))
        del record["drawing_sha256"], record["reader_settings"]
        samples.append(((out / CROPS_CSV).read_text(encoding="utf-8"), record))

    assert samples[0] == samples[1]
    assert samples[0][1]["places_by_source"]["printed text"] == 3


def test_pages_read_in_other_processes_give_the_same_sample(drawing: Path, tmp_path: Path) -> None:
    """Pages are read apart and put together in page order, so the number of processes changes how
    long a sample takes and nothing it holds."""
    alone, together = tmp_path / "data" / "alone", tmp_path / "data" / "together"

    assert _sample(drawing, alone) == 0
    assert _sample(drawing, together, workers=2) == 0

    assert (alone / CROPS_CSV).read_text(encoding="utf-8") == (together / CROPS_CSV).read_text(
        encoding="utf-8"
    )


def test_the_sample_is_written_only_under_data(
    drawing: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _sample(drawing, tmp_path / "elsewhere") == 2
    assert "not under data/" in capsys.readouterr().err
    assert not (tmp_path / "elsewhere").exists()


def test_every_kind_needs_a_stated_quota(
    drawing: Path, out: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    quotas = {kind: count for kind, count in QUOTAS.items() if kind != "small"}

    assert _sample(drawing, out, quotas=quotas) == 2
    assert "['small'] have none" in capsys.readouterr().err


def test_every_frame_setting_comes_from_the_settings_file(
    drawing: Path, out: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    partial = {
        name: value
        for name, value in FRAME_SETTINGS.items()
        if name != "GV_READER_AMBIGUITY_MARGIN"
    }
    path = _settings_file(drawing.parent, partial)

    assert _sample(drawing, out, settings=path) == 2
    assert "GV_READER_AMBIGUITY_MARGIN" in capsys.readouterr().err


def test_where_a_run_agreed_is_read_by_place_alone() -> None:
    """The query names no column a reading is in — not to select, not to filter — so what a run
    agreed on cannot reach the frame, only where; the polygon is rescaled to the key's frame and
    rounded outward."""
    for column in ("raw_text", "value_numerator", "value_denominator", "unit", "extractor"):
        assert column not in AGREED_SQL

    boxes = agreed_boxes([(2, [[10, 21], [31, 21], [31, 40], [10, 40]], 300)])

    assert boxes == {2: [(20, 42, 62, 80)]}


# --- the person's checks -----------------------------------------------------------------------


def _triage(folders: Sequence[Path], minimum: str, answers: str | None = None) -> int:
    extra = [] if answers is None else ["--answers", answers]
    return main(["triage", *map(str, folders), "--minimum-share", minimum, *extra])


def test_triage_prints_the_share_of_dimensions_and_fails_a_drawing_below_it(
    drawing: Path, out: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _sample(drawing, out) == 0
    rows = _rows(out)
    rows[0]["not_a_dimension"] = "x"
    _write_rows(out, rows)

    assert _triage([out], "0.75") == 0
    assert "75.0%" in capsys.readouterr().out
    assert _triage([out], "0.8") == 1
    assert "below the minimum" in capsys.readouterr().out


def test_triage_reads_marks_kept_apart_from_the_persons_sheet(
    drawing: Path, out: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _sample(drawing, out) == 0
    rows = _rows(out)
    with (out / "triage.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["crop_id", "not_a_dimension"])
        writer.writeheader()
        writer.writerows({"crop_id": row["crop_id"], "not_a_dimension": "x"} for row in rows)

    assert _triage([out], "0.6", "triage.csv") == 1
    assert _triage([out], "0.6") == 0


def _crop_printing(folder: Path, drawing: Path, text: str) -> str:
    """The id of the crop that holds the run printing `text` — the test knows what it printed."""
    (run,) = (
        item
        for item in read_stamp_text(
            drawing.read_bytes(), 0, document_version_id=uuid4(), dpi=VISION_CROP_DPI
        ).contents.texts
        if item.text == text
    )
    xs = [point.x for point in run.image_extent]
    ys = [point.y for point in run.image_extent]
    for row in _rows(folder):
        box = [int(row[name]) for name in ("left_px", "top_px", "right_px", "bottom_px")]
        if box[0] <= min(xs) and box[1] <= min(ys) and max(xs) <= box[2] and max(ys) <= box[3]:
            return row["crop_id"]
    raise AssertionError("no crop holds the run")


def test_the_look_again_list_names_the_crop_and_never_a_value(
    drawing: Path, out: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The drawing prints `24"` there and the typist typed `25"`: the crop is listed, by id, with
    which text differs — and neither number appears anywhere in what is printed."""
    assert _sample(drawing, out, quotas={**QUOTAS, "plain": 6}) == 0
    crop_id = _crop_printing(out, drawing, '24"')
    rows = _rows(out)
    for row in rows:
        row["vendor_value"] = '25"' if row["crop_id"] == crop_id else ""
    _write_rows(out, rows)
    capsys.readouterr()

    assert main(["check", str(out), "--pdf", str(drawing)]) == 0

    printed = capsys.readouterr().out
    assert crop_id in printed and "the drawing's own printed text" in printed
    assert "24" not in printed.replace(crop_id, "") and "25" not in printed.replace(crop_id, "")

    for row in rows:
        row["vendor_value"] = '24"' if row["crop_id"] == crop_id else ""
    _write_rows(out, rows)
    assert main(["check", str(out), "--pdf", str(drawing)]) == 0
    assert "Nothing to look at again" in capsys.readouterr().out


# --- the new columns, round trip ---------------------------------------------------------------


def _typed(folder: Path, ticks: Mapping[int, Mapping[str, str]]) -> list[dict[str, str]]:
    """Every crop typed `24"`, then each row's own changes."""
    rows = _rows(folder)
    for number, row in enumerate(rows):
        row["vendor_value"] = '24"'
        row.update(ticks.get(number, {}))
    _write_rows(folder, rows)
    return rows


def test_the_new_columns_survive_build_into_both_loaders(drawing: Path, out: Path) -> None:
    """**Typed, built, loaded back.** The person's ticks reach the bake-off as tags and the agent
    scorecard as fields; a crop marked cut off or not a dimension is recorded and not scored."""
    assert _sample(drawing, out) == 0
    rows = _typed(
        out,
        {
            0: {"stacked": "x", "dual_unit": "x", "gv_value_seen": '30-1/2"'},
            1: {"rotated": "x"},
            2: {"vendor_value": "", "cut_off": "x", "note": "the wide view shows more"},
            3: {"vendor_value": "", "not_a_dimension": "x"},
        },
    )

    assert _build(out, drawing) == 0

    crops = load_crops(out)
    assert [crop.tags for crop in crops] == [
        frozenset({"stacked", "dual_unit", "gv_seen"}),
        frozenset({"rotated"}),
    ]
    keyed = {crop.crop_id: crop for crop in load_key(out)}
    first, second, cut, symbol = (keyed[row["crop_id"]] for row in rows)
    assert (first.stacked, first.dual_unit, first.gv_value_seen) == (True, True, '30-1/2"')
    assert second.kind is KeyKind.SCORED and not second.dual_unit
    assert cut.kind is KeyKind.UNREADABLE and cut.cut_off
    assert symbol.kind is KeyKind.NOT_A_DIMENSION


def test_a_value_typed_on_a_cut_off_crop_is_refused(
    drawing: Path, out: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _sample(drawing, out) == 0
    _typed(out, {0: {"cut_off": "x"}})

    assert _build(out, drawing) == 2
    assert "cut-off crop is not scored" in capsys.readouterr().err


def test_gv_number_that_is_not_one_number_is_refused(
    drawing: Path, out: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _sample(drawing, out) == 0
    _typed(out, {0: {"gv_value_seen": "see detail"}})

    assert _build(out, drawing) == 2
    assert "is not one number" in capsys.readouterr().err


def test_a_sheet_scaffolded_before_the_split_still_builds_from_its_value_column(
    drawing: Path, out: Path
) -> None:
    assert _sample(drawing, out) == 0
    rows = _typed(out, {})
    renamed = [{("value" if k == "vendor_value" else k): v for k, v in row.items()} for row in rows]
    _write_rows(out, renamed)

    assert _build(out, drawing) == 0
    assert len(load_crops(out)) == len(rows)


# --- several keys, and what a model is shown ---------------------------------------------------


def _key(tmp_path: Path, name: str, prefix: str, case_id: str) -> Path:
    folder = tmp_path / name
    folder.mkdir()
    path = folder / "shop.pdf"
    path.write_bytes(_drawing())
    out = tmp_path / "data" / name
    assert _sample(path, out, prefix=prefix) == 0
    _typed(out, {})
    assert _build(out, path, case_id=case_id) == 0
    return out


def test_several_keys_load_together_and_a_shared_crop_id_is_refused(tmp_path: Path) -> None:
    first = _key(tmp_path, "one", "a", "key-one")
    second = _key(tmp_path, "two", "b", "key-two")
    clash = _key(tmp_path, "three", "c", "key-one")

    crops = load_keys([first, second])

    assert len(crops) == len(load_crops(first)) + len(load_crops(second))
    assert len({crop.crop_id for crop in crops}) == len(crops)
    with pytest.raises(ModelBakeoffError, match="is in both"):
        load_keys([first, clash])


def test_both_scripts_take_several_keys() -> None:
    from scripts.agent_scorecard import build_parser as scorecard_parser
    from scripts.model_bakeoff import build_parser as bakeoff_parser

    bakeoff = bakeoff_parser().parse_args(["models.json", "--key", "a", "--key", "b"])
    scorecard = scorecard_parser().parse_args(
        [
            *("--key", "a", "--key", "b", "--reader-settings", "demo.sh", "--stage-dpi", "300"),
            *("--sharper-dpi", "450", "--label-gap-pt", "4", "--max-label-pt", "40"),
            *("--max-steps", "6", "--output", "out.md"),
        ]
    )

    assert bakeoff.key == scorecard.key == [Path("a"), Path("b")]


def test_a_model_is_shown_the_crop_at_the_readers_dpi_and_the_person_at_the_keys(
    tmp_path: Path,
) -> None:
    """**Score what the model saw** (#728 follow-up). The person reads each crop at the key's 600
    dpi; production's vision readers are cut their crops from a 300 dpi page, so the bake-off renders
    the same polygon at 300 for a model."""
    out = _key(tmp_path, "one", "a", "key-one")
    rows = _rows(out)
    case = json.loads((out / "answer_key.json").read_text(encoding="utf-8"))
    pdf = (out / case["shop"]).read_bytes()

    crops = load_crops(out)

    assert READER_RASTER_DPI == 300 and VISION_CROP_DPI == 600
    for crop, row in zip(crops, rows, strict=True):
        polygon = (
            int(row["left_px"]),
            int(row["top_px"]),
            int(row["right_px"]),
            int(row["bottom_px"]),
        )
        person = (out / row["image"]).read_bytes()
        shown = render_crop(
            pdf, page=1, polygon=polygon, polygon_dpi=VISION_CROP_DPI, output_dpi=READER_RASTER_DPI
        )
        assert person == render_crop(
            pdf, page=1, polygon=polygon, polygon_dpi=VISION_CROP_DPI, output_dpi=VISION_CROP_DPI
        )
        assert crop.image == shown
        person_width, person_height, _ = decode_rgb_png(person)
        model_width, model_height, _ = decode_rgb_png(crop.image)
        assert abs(2 * model_width - person_width) <= 2
        assert abs(2 * model_height - person_height) <= 2
