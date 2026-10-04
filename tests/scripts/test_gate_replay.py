"""The gate replay's script: read-only, counts only in its report, and refusing what it can't place.

Verification for: `scripts/gate_replay.py`.

The drawing and the key are the made-up ones of `tests/eval/test_agent_scorecard.py`, and the
readings are typed in here. No model is called and no client drawing is read.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from sqlalchemy import Engine, event, text

import scripts.gate_replay as gate_replay_script
from eval.experiments.gate_replay import Guard, ReplayError
from scripts.gate_replay import (
    ROWS_SQL,
    VERSIONS_SQL,
    main,
    only_version,
    stored_rows,
)
from tests.eval.test_agent_scorecard import _row, _write_key
from tests.extraction.test_annotations import _appearance, _pdf, _stamp
from tests.extraction.test_stamp_text import _sheet as _text_sheet
from tests.scripts.test_agent_geometry_check import SETTINGS
from tests.workflow.test_glyph_route import _content
from tests.workflow.test_reading_agent import LABEL
from workflow import stages

AGENT = "GV_AGENT_LABEL_GAP_PT=4 \\\nGV_AGENT_MAX_LABEL_PT=40 \\\n"


#: The made-up sheet's `10192"` label with the key crop round it: 46–119 × 201–244 pt at 600 dpi,
#: so its region, 9 pt in on each side, holds the label.
ROUND_THE_LABEL = {"left_px": "383", "top_px": "1675", "right_px": "992", "bottom_px": "2033"}


def _setup(
    tmp_path: Path,
    pair: list[list[str | None]],
    *,
    sheet: bytes | None = None,
    crop: dict[str, str] = ROUND_THE_LABEL,
) -> tuple[Path, Path, Path]:
    key = _write_key(tmp_path / "data" / "key", [_row("k0", value='12"', exact="12", **crop)])
    if sheet is not None:
        (key / "sheet.pdf").write_bytes(sheet)
    settings = tmp_path / "demo.sh"
    settings.write_text(SETTINGS + AGENT, encoding="utf-8")
    scorecard = tmp_path / "data" / "scorecard.json"
    scorecard.write_text(json.dumps([{"crop": "k0", "pair": pair}]), encoding="utf-8")
    return key, settings, scorecard


def _run(key: Path, settings: Path, scorecard: Path, output: Path) -> int:
    return main(
        [
            str(key),
            "--key-dpi",
            "600",
            "--key-margin-pt",
            "9",
            "--reader-settings",
            str(settings),
            "--stage-dpi",
            "150",
            "--scorecard",
            str(scorecard),
            "--output",
            str(output),
        ]
    )


def test_an_agreed_wrong_pair_is_reported_as_a_count_and_kept_out_of_the_report(
    tmp_path: Path,
) -> None:
    key, settings, scorecard = _setup(
        tmp_path,
        [["bedrock-nova-2-lite", '9173"', None], ["bedrock-ministral-3-3b", '9173"', None]],
    )
    output = tmp_path / "data" / "replay.md"

    assert _run(key, settings, scorecard, output) == 0

    report = output.read_text(encoding="utf-8")
    assert f"| {Guard.NONE.value} | 0 | **1** | 0 | 1/1 (100.0%) | not bounded" in report
    assert "9173" not in report, "the readings stay in the working file, which is never committed"
    working = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
    (rows,) = working.values()
    assert rows[0]["outcomes"][Guard.NONE.value] == "agreed, wrong"


def test_the_output_must_be_under_data(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    key, settings, scorecard = _setup(tmp_path, [])

    assert _run(key, settings, scorecard, tmp_path / "replay.md") == 2
    assert "under data/" in capsys.readouterr().err


def test_a_key_with_no_frame_and_no_margin_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key, settings, _ = _setup(tmp_path, [])

    code = main(
        [
            str(key),
            "--key-dpi",
            "600",
            "--reader-settings",
            str(settings),
            "--stage-dpi",
            "150",
            "--scorecard",
            "unused.json",
            "--output",
            str(tmp_path / "data" / "replay.md"),
        ]
    )

    assert code == 2
    assert "how much page" in capsys.readouterr().err


@pytest.mark.parametrize("sql", [VERSIONS_SQL, ROWS_SQL])
def test_the_queries_only_read(sql: str) -> None:
    assert sql.strip().upper().startswith("SELECT")
    assert not re.search(
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE)\b", sql, re.IGNORECASE
    )


def test_the_queries_run_on_the_migrated_schema_and_refuse_a_drawing_never_read(
    postgres_engine: Engine,
) -> None:
    """A renamed column fails here rather than on a real run; an empty database has no version."""
    with postgres_engine.connect() as connection:
        connection.execute(
            text(ROWS_SQL), {"version": "00000000-0000-4000-8000-000000000000"}
        ).all()

    with pytest.raises(ReplayError, match="0 document versions"):
        stored_rows(postgres_engine, "a" * 64)


def test_the_database_is_read_inside_a_read_only_transaction(postgres_engine: Engine) -> None:
    """**Before anything is selected**, so no statement after it could write to a run's database."""
    statements: list[str] = []

    def record(*arguments: object) -> None:
        statements.append(str(arguments[2]).strip())

    event.listen(postgres_engine, "before_cursor_execute", record)
    try:
        with pytest.raises(ReplayError):
            stored_rows(postgres_engine, "a" * 64)
    finally:
        event.remove(postgres_engine, "before_cursor_execute", record)

    assert statements[0] == "SET TRANSACTION READ ONLY"
    assert all(statement.upper().startswith("SELECT") for statement in statements[1:])


def test_readings_from_two_uploads_of_one_drawing_are_never_mixed() -> None:
    assert only_version(["v1"]) == "v1"
    with pytest.raises(ReplayError, match="2 document versions"):
        only_version(["v1", "v2"])
    with pytest.raises(ReplayError, match="0 document versions"):
        only_version([])


#: The sheet's `10192"` drawn as paths, in the vendor's black and in a reviewer's red.
BLACK_PATHS = _pdf(
    annotations=[_stamp(appearance_object=6)],
    extra_objects=[_appearance(b"1 w 105 515 m 205 515 l S\n" + _content(LABEL))],
)
RED_PATHS = _pdf(
    annotations=[_stamp(appearance_object=6)],
    extra_objects=[_appearance(b"1 0 0 RG\n1 w 105 515 m 205 515 l S\n" + _content(LABEL))],
)

#: The sheet's `10192"` in the vendor's black, with a red line the whole width of the stamp running
#: through it (#929): appearance `y = 524`, page `y = 74`, inside the key crop; and the same in black.
#: The label is drawn after the line, in black, so only the line is coloured.
LONG_RED_LINE = _pdf(
    annotations=[_stamp(appearance_object=6)],
    extra_objects=[
        _appearance(
            b"q 1 0 0 RG 1 w 100 524 m 400 524 l S Q\n"
            + b"1 w 105 515 m 205 515 l S\n"
            + _content(LABEL)
        )
    ],
)
LONG_BLACK_LINE = _pdf(
    annotations=[_stamp(appearance_object=6)],
    extra_objects=[
        _appearance(
            b"q 0 0 0 RG 1 w 100 524 m 400 524 l S Q\n"
            + b"1 w 105 515 m 205 515 l S\n"
            + _content(LABEL)
        )
    ],
)

#: Font text `36"` over `38"` in a pasted drawing, the lower one black or red, with the key crop
#: round the lower one: 41–99 × 227–265 pt at 600 dpi.
BLACK_TEXT = _text_sheet(b'(36") Tj 0 -20 Td (38") Tj')
RED_TEXT = _text_sheet(b'(36") Tj 1 0 0 rg 0 -20 Td (38") Tj')
ROUND_THE_TEXT = {"left_px": "342", "top_px": "1892", "right_px": "825", "bottom_px": "2208"}


@pytest.mark.parametrize(
    ("black", "red", "crop"),
    [
        (BLACK_PATHS, RED_PATHS, ROUND_THE_LABEL),
        (BLACK_TEXT, RED_TEXT, ROUND_THE_TEXT),
        (LONG_BLACK_LINE, LONG_RED_LINE, ROUND_THE_LABEL),
    ],
    ids=["paths", "text", "long-line"],
)
def test_a_gv_mark_drawn_in_colour_holds_its_crops_agreement_back(
    tmp_path: Path, black: bytes, red: bytes, crop: dict[str, str]
) -> None:
    """Markup in colour, as a reviewer's is when it is baked into a snapshot: the gate's agreement
    is held back by the GV-mark guard, and kept where the same drawing is all black."""
    pair: list[list[str | None]] = [
        ["bedrock-nova-2-lite", '12"', None],
        ["bedrock-ministral-3-3b", '12"', None],
    ]
    rows = {}
    for name, sheet in (("black", black), ("red", red)):
        key, settings, scorecard = _setup(tmp_path / name, pair, sheet=sheet, crop=crop)
        output = tmp_path / name / "data" / "replay.md"
        assert _run(key, settings, scorecard, output) == 0
        rows[name] = next(
            line
            for line in output.read_text(encoding="utf-8").splitlines()
            if line.startswith("| held back where a GV mark")
        )

    assert rows["black"].startswith(f"| {Guard.GV_MARK.value} | 1 |")
    assert rows["red"].startswith(f"| {Guard.GV_MARK.value} | 0 |")


def test_the_replay_asks_the_production_gate_whether_a_crop_shows_a_gv_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**One test, not two copies of it (#901).** The replay's GV-mark fact is the production gate's
    own `workflow.stages.gv_mark_in_crop`, over the markup the stage's own reader finds: whatever
    that function says about a crop is what the replay's guard does with it. Here it is made to say
    "a mark" about the all-black sheet, and the agreement is held back."""
    assert gate_replay_script.gv_mark_in_crop is stages.gv_mark_in_crop
    assert gate_replay_script.coloured_markup is stages.coloured_markup
    asked: list[bool] = []

    def spy(*arguments: object) -> bool:
        asked.append(stages.gv_mark_in_crop(*arguments))  # type: ignore[arg-type]
        return True

    monkeypatch.setattr(gate_replay_script, "gv_mark_in_crop", spy)
    pair: list[list[str | None]] = [
        ["bedrock-nova-2-lite", '12"', None],
        ["bedrock-ministral-3-3b", '12"', None],
    ]
    key, settings, scorecard = _setup(tmp_path, pair, sheet=BLACK_TEXT, crop=ROUND_THE_TEXT)
    output = tmp_path / "data" / "replay.md"

    assert _run(key, settings, scorecard, output) == 0

    assert asked == [False], "asked once, for the key's one crop, which shows no mark"
    report = output.read_text(encoding="utf-8")
    assert f"| {Guard.GV_MARK.value} | 0 |" in report
    assert f"| {Guard.NONE.value} | 1 |" in report


def test_the_replay_gathers_the_markup_by_the_stage_s_own_function(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**One function, not two copies of it (#929).** The markup the replay asks about is
    `workflow.stages.coloured_markup`'s, the function the gate and the part pictures gather theirs
    by, handed the page's transform and glyph paths at the stage's dpi. Made to find nothing on the
    sheet with the long red line, the replay holds nothing back."""
    calls: list[int] = []

    def nothing(*arguments: object, **keywords: object) -> stages.ColouredMarkup:
        found = stages.coloured_markup(*arguments, **keywords)  # type: ignore[arg-type]
        assert found.coloured_paths, "the real function finds the red line"
        calls.append(int(keywords["dpi"]))  # type: ignore[call-overload]
        return stages.ColouredMarkup(
            text=(), paths=(), transform=None, coloured_paths=(), pasted_stamps=()
        )

    monkeypatch.setattr(gate_replay_script, "coloured_markup", nothing)
    pair: list[list[str | None]] = [
        ["bedrock-nova-2-lite", '12"', None],
        ["bedrock-ministral-3-3b", '12"', None],
    ]
    key, settings, scorecard = _setup(tmp_path, pair, sheet=LONG_RED_LINE)
    output = tmp_path / "data" / "replay.md"

    assert _run(key, settings, scorecard, output) == 0

    assert calls == [150], "once for the key's one page, at the stage's dpi"
    assert f"| {Guard.GV_MARK.value} | 1 |" in output.read_text(encoding="utf-8")


#: A key crop round the label's last two digits and its inch mark only: its region, 9 pt in on each
#: side, is 78–91 × 221–230 pt, so the crop production cuts round it starts after the label does.
ROUND_THE_LAST_DIGITS = {"left_px": "575", "top_px": "1767", "right_px": "834", "bottom_px": "1992"}


@pytest.mark.parametrize(
    ("crop", "agreed"),
    [(ROUND_THE_LABEL, 1), (ROUND_THE_LAST_DIGITS, 0)],
    ids=["whole-label", "cut-label"],
)
def test_a_crop_that_cuts_the_label_holds_its_agreement_back(
    tmp_path: Path, crop: dict[str, str], agreed: int
) -> None:
    """**The row the admin's decision rests on (#919).** The same pair, agreeing on the same value:
    held back by the cut-label guard, and by the gate as it now is, where production's crop round
    the region cuts the label the sheet draws; kept where it holds all of it."""
    pair: list[list[str | None]] = [
        ["bedrock-qwen3-vl-235b", '12"', None],
        ["bedrock-nova-2-lite-taught", '12"', None],
    ]
    key, settings, scorecard = _setup(tmp_path, pair, crop=crop)
    output = tmp_path / "data" / "replay.md"

    assert _run(key, settings, scorecard, output) == 0

    report = output.read_text(encoding="utf-8")
    assert f"| {Guard.NONE.value} | 1 |" in report
    assert f"| {Guard.CUT.value} | {agreed} |" in report
    assert f"| {Guard.GATE.value} | {agreed} |" in report


def test_the_replay_asks_the_production_gate_whether_a_crop_cuts_the_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**One test, not two copies of it (#919).** The replay's cut fact is the production gate's own
    `workflow.stages.cut_label_refusal`, handed what the stage hands it: whatever that function says
    about a crop is what the replay's guard does with it. Here it is made to refuse the crop round
    the whole label, and the agreement is held back."""
    assert gate_replay_script.cut_label_refusal is stages.cut_label_refusal
    asked: list[str | None] = []

    def spy(*arguments: object, **named: object) -> str | None:
        asked.append(stages.cut_label_refusal(*arguments, **named))  # type: ignore[arg-type]
        return "held back"

    monkeypatch.setattr(gate_replay_script, "cut_label_refusal", spy)
    pair: list[list[str | None]] = [
        ["bedrock-qwen3-vl-235b", '12"', None],
        ["bedrock-nova-2-lite-taught", '12"', None],
    ]
    key, settings, scorecard = _setup(tmp_path, pair)
    output = tmp_path / "data" / "replay.md"

    assert _run(key, settings, scorecard, output) == 0

    assert asked == [None], "asked once, for the key's one crop, which holds the whole label"
    report = output.read_text(encoding="utf-8")
    assert f"| {Guard.CUT.value} | 0 |" in report
    assert f"| {Guard.NONE.value} | 1 |" in report
