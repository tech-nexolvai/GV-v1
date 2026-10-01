"""The check sheet for the agent's sideways and cut-off detection, and its scoring (#778).

Verification for: `scripts/agent_geometry_check.py`.

The one that matters most is `test_a_region_touching_a_key_crop_is_never_on_the_sheet`: the check
exists to measure the geometry away from the key, and a crop the key holds on the sheet would make
it measure the key again.

The drawing is made up: one dimension with its witness lines and a `10192"` label above it, drawn as
paths in the font of `tests/extraction/test_glyph_reader.py`. No client drawing is read.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

import scripts.agent_geometry_check as check
from scripts.agent_geometry_check import main
from tests.eval.test_agent_scorecard import _row, _write_key
from tests.extraction.test_annotations import _appearance, _pdf, _stamp
from tests.extraction.test_glyph_reader import _row as _glyph_row
from tests.workflow.test_glyph_route import _content

#: Appearance space maps to page by −(50, 450). A 100 pt dimension at appearance y 560 with 60 pt
#: witness lines, and `10192"` above it — all inside the stamp's box `[100 500 400 700]`, which the
#: reader clips to (a witness overshooting it would be cut and the junction lost).
LABEL, _ = _glyph_row('10192"', 110, 565, scale=0.9)
SHEET = _pdf(
    annotations=[_stamp(appearance_object=6)],
    extra_objects=[
        _appearance(
            b"1 w 105 560 m 205 560 l S\n1 w 105 530 m 105 590 l S\n1 w 205 530 m 205 590 l S\n"
            + _content(LABEL)
        )
    ],
)

SETTINGS = """
GV_READER_LINE_MINIMUM_PT=50 \\
GV_READER_GLYPH_MAXIMUM_PT=10 \\
GV_READER_GLYPH_GAP_PT=4 \\
GV_READER_PROXIMITY_LIMIT=0.9 \\
GV_READER_AMBIGUITY_MARGIN=0.005 \\
GV_READER_WITNESS_TOLERANCE=0.01 \\
GV_READER_MINIMUM_SPAN=0.02 \\
GV_READER_STRAIGHTNESS=0.0005 \\
GV_READER_CROSSING_MARGIN=0.001 \\
GV_READER_LOCALIZED_MINIMUM_PATHS=1 \\
GV_READER_LOCALIZED_MAXIMUM_SPAN=0.5 \\
GV_READER_LOCALIZED_CROP_MARGIN_PT=2 \\
GV_READER_FRACTION_BAR_THICKNESS_MAX_PT=0.3 \\
GV_READER_FRACTION_BAR_LENGTH_MIN_PT=1 \\
GV_READER_FRACTION_REACH_PT=3 \\
GV_READER_FRACTION_GLYPH_MIN_PT=1 \\
GV_READER_FRACTION_GLYPH_MAX_PT=12 \\
GV_READER_FRACTION_PROPORTION_MAX=2.5 \\
"""


@pytest.fixture(autouse=True)
def _no_ocr_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The label here is drawn as paths, and loading the ONNX models to prove it shows a number
    would test the OCR engine, not the sheet; `test_crops_showing_no_number_are_left_off` checks
    the filter itself."""
    monkeypatch.setattr(check, "ocr_shows_a_number", lambda _png: True)


def _setup(tmp_path: Path, key_rows: list[dict[str, str]]) -> tuple[Path, Path, Path]:
    pdf = tmp_path / "sheet.pdf"
    pdf.write_bytes(SHEET)
    settings = tmp_path / "demo.sh"
    settings.write_text(SETTINGS, encoding="utf-8")
    key = _write_key(tmp_path / "key", key_rows)
    return pdf, settings, key


def _sheet(tmp_path: Path, pdf: Path, settings: Path, key: Path, out: Path) -> int:
    return main(
        [
            "sheet",
            str(pdf),
            "--key",
            str(key),
            "--key-dpi",
            "600",
            "--reader-settings",
            str(settings),
            "--stage-dpi",
            "150",
            "--label-gap-pt",
            "4",
            "--max-label-pt",
            "40",
            "--per-stratum",
            "5",
            "--seed",
            "1",
            "--out",
            str(out),
        ]
    )


#: A key crop in the page's far corner, touching nothing on the drawing.
FAR = _row("k0", value='12"', exact="12", left_px="5", top_px="5", right_px="40", bottom_px="40")


def test_the_sheet_holds_the_crops_blind_with_empty_answers(tmp_path: Path) -> None:
    """Outcome: each crop and its wide view, a sheet with empty `sideways` and `cut_off` and no
    geometry call in it, and the group each was drawn from kept apart in `geometry.json`."""
    pdf, settings, key = _setup(tmp_path, [FAR])
    out = tmp_path / "data" / "check"

    assert _sheet(tmp_path, pdf, settings, key, out) == 0

    with (out / "crops.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows, "the drawing's label was not planned as a region"
    assert set(rows[0]) == {
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
    }
    assert all(row["label"] == row["sideways"] == row["cut_off"] == "" for row in rows)
    for row in rows:
        assert (out / f"{row['crop_id']}.png").exists()
        assert (out / f"{row['crop_id']}_wide.png").exists()
    recorded = json.loads((out / "geometry.json").read_text(encoding="utf-8"))
    assert set(recorded["drawn_from"]) == {row["crop_id"] for row in rows}
    assert "drawn_from" not in (out / "sheet.html").read_text(encoding="utf-8")


def test_a_region_touching_a_key_crop_is_never_on_the_sheet(tmp_path: Path) -> None:
    """**The point of the check.** Outcome: a key crop over the label leaves it off the sheet, and
    the count left out says so."""
    over = _row(
        "k0",
        value='10192"',
        exact="10192",
        left_px="0",
        top_px="0",
        right_px="3300",
        bottom_px="2400",
    )
    pdf, settings, key = _setup(tmp_path, [over])
    out = tmp_path / "data" / "check"

    assert _sheet(tmp_path, pdf, settings, key, out) == 0

    with (out / "crops.csv").open(encoding="utf-8") as handle:
        assert list(csv.DictReader(handle)) == []
    assert (
        json.loads((out / "geometry.json").read_text(encoding="utf-8"))["left_out_touching_the_key"]
        >= 1
    )


def test_the_sheet_is_written_only_under_data_and_never_over_one_being_checked(
    tmp_path: Path,
) -> None:
    pdf, settings, key = _setup(tmp_path, [FAR])

    assert _sheet(tmp_path, pdf, settings, key, tmp_path / "elsewhere") == 2
    out = tmp_path / "data" / "check"
    assert _sheet(tmp_path, pdf, settings, key, out) == 0
    assert _sheet(tmp_path, pdf, settings, key, out) == 2


def _score(tmp_path: Path, pdf: Path, settings: Path, out: Path) -> int:
    return main(
        [
            "score",
            str(out),
            "--pdf",
            str(pdf),
            "--reader-settings",
            str(settings),
            "--max-label-pt",
            "40",
            "--output",
            str(tmp_path / "result.md"),
        ]
    )


def test_scoring_refuses_a_sheet_not_fully_answered(tmp_path: Path) -> None:
    pdf, settings, key = _setup(tmp_path, [FAR])
    out = tmp_path / "data" / "check"
    _sheet(tmp_path, pdf, settings, key, out)

    assert _score(tmp_path, pdf, settings, out) == 2


def test_scoring_reports_every_rule_against_the_persons_answers(tmp_path: Path) -> None:
    """Outcome: with the answers in, one row for each rule — the current and the alternative
    sideways rule, and the cut-off at each label gap — counting agreements, misses and false alarms.
    """
    pdf, settings, key = _setup(tmp_path, [FAR])
    out = tmp_path / "data" / "check"
    _sheet(tmp_path, pdf, settings, key, out)
    with (out / "crops.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["label"], row["sideways"], row["cut_off"] = "yes", "no", "no"
    with (out / "crops.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    assert _score(tmp_path, pdf, settings, out) == 0

    result = (tmp_path / "result.md").read_text(encoding="utf-8")
    for rule in (
        "sideways, current rule",
        "sideways, longest run up",
        "cut off, label gap 4 pt",
        "cut off, label gap 3 pt",
        "cut off, label gap 2 pt",
    ):
        assert f"| {rule} |" in result
    assert "on those the person answered sideways on 0 and cut off on 0. 0 show no label." in result


def test_crops_with_no_label_are_scored_apart(tmp_path: Path) -> None:
    """Outcome: a crop the person says holds no label counts towards no rule's agreement; what each
    rule called on it is reported as firing on a crop with no label."""
    pdf, settings, key = _setup(tmp_path, [FAR])
    out = tmp_path / "data" / "check"
    _sheet(tmp_path, pdf, settings, key, out)
    with (out / "crops.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["label"], row["sideways"], row["cut_off"] = "no", "no", "no"
    with (out / "crops.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    assert _score(tmp_path, pdf, settings, out) == 0

    result = (tmp_path / "result.md").read_text(encoding="utf-8")
    assert "0 show a label" in result
    assert f"| sideways, current rule | 0 | 0 | 0 | 0 of {len(rows)} |" in result


def test_a_settings_file_missing_a_detector_setting_is_refused(tmp_path: Path) -> None:
    _pdf_path, settings, _key = _setup(tmp_path, [FAR])
    settings.write_text(
        SETTINGS.replace("GV_READER_STRAIGHTNESS=0.0005 \\\n", ""), encoding="utf-8"
    )

    with pytest.raises(Exception, match="GV_READER_STRAIGHTNESS"):
        from scripts.agent_geometry_check import read_settings

        read_settings(settings)


def test_crops_showing_no_number_are_left_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outcome: a crop the OCR engine finds no numeral in is not on the sheet — a sheet of drawing
    symbols would measure nothing about labels — and the count left out says so."""
    monkeypatch.setattr(check, "ocr_shows_a_number", lambda _png: False)
    pdf, settings, key = _setup(tmp_path, [FAR])
    out = tmp_path / "data" / "check"

    assert _sheet(tmp_path, pdf, settings, key, out) == 0

    with (out / "crops.csv").open(encoding="utf-8") as handle:
        assert list(csv.DictReader(handle)) == []
    assert (
        json.loads((out / "geometry.json").read_text(encoding="utf-8"))[
            "left_out_showing_no_number"
        ]
        >= 1
    )
