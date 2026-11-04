"""The reading exam: which labels it samples, how it cuts them, its key, and the scorer's gate.

Every fixture is synthetic: invented file names, pages, boxes and printed values. The real key and
crops hold client values and live only under `~/gv-local/exam/reading/`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from fractions import Fraction
from pathlib import Path

import pytest

from eval.exam import reading, reading_score
from eval.exam.reading import KeyRow

ROOT = Path(__file__).resolve().parents[2]


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


# ---- sampling --------------------------------------------------------------------------------


def _pack():
    dims = {
        "files": ["shop.pdf", "arch.pdf"],
        "pages": [
            {
                "file": "shop.pdf",
                "page": 1,
                "labels": [
                    {"text": '24"', "box": [10, 10, 30, 18], "side": "vendor", "route": "text"},
                    {"text": '6"', "box": [40, 10, 50, 18], "side": "vendor", "route": "drawn"},
                    {
                        "text": '3"',
                        "box": [60, 10, 70, 18],
                        "side": "vendor",
                        "route": "text",
                        "derived": True,
                    },
                ],
            },
            {
                "file": "arch.pdf",
                "page": 2,
                "labels": [
                    {
                        "text": "2'-0\"",
                        "box": [5, 5, 12, 40],
                        "side": "architect",
                        "route": "text",
                        "orientation": "v",
                    }
                ],
            },
        ],
    }
    coverage = {
        "pages": [
            {
                "file": "arch.pdf",
                "page": 2,
                "not_read": [
                    {
                        "kind": "strokes",
                        "side": "architect",
                        "reason": "stacked_fraction",
                        "also": ["sideways"],
                        "box": [100, 100, 110, 130],
                    },
                    {
                        "kind": "strokes",
                        "side": "architect",
                        "reason": "sideways",
                        "also": [],
                        "box": [200, 100, 200, 120],  # one straight stroke: no width
                    },
                    {
                        "kind": "strokes",
                        "side": "architect",
                        "reason": "too_small",
                        "box": [1, 1, 2, 2],
                    },
                    {
                        "kind": "strokes",
                        "side": "vendor",
                        "reason": "sideways",
                        "box": [3, 3, 4, 9],
                    },
                    {
                        "kind": "typed",
                        "side": "architect",
                        "reason": "in_note",
                        "box": [7, 7, 9, 9],
                    },
                ],
            }
        ]
    }
    return dims, coverage


def test_the_candidates_are_unread_architect_strokes_by_reason_and_three_control_groups():
    dims, coverage = _pack()
    found, boxes = reading.candidates_of_pack("setX", dims, coverage)
    kinds = sorted((c.group, c.reason) for c in found)
    assert kinds == [
        ("architect_strokes", "sideways"),
        ("architect_strokes", "stacked_fraction"),
        ("architect_typed", "architect_typed"),
        ("vendor_strokes", "vendor_strokes"),
        ("vendor_typed", "vendor_typed"),
    ]
    by_reason = {c.reason: c for c in found}
    # The reason given second still says the label is written sideways: both turns are cut.
    assert by_reason["stacked_fraction"].sideways and by_reason["stacked_fraction"].also == (
        "sideways",
    )
    assert by_reason["architect_typed"].sideways  # a typed label written along the height
    assert not by_reason["vendor_typed"].sideways
    assert (
        by_reason["vendor_typed"].code_text == '24"' and by_reason["vendor_typed"].file_index == 0
    )
    # Every label and spot on a page is a neighbour to stay clear of, the derived one too.
    assert len(boxes[(0, 1)]) == 3 and len(boxes[(1, 2)]) == 6


def test_allocation_is_proportional_with_a_minimum_per_reason():
    counts = reading.allocate({"a": 1000, "b": 500, "c": 60, "d": 10}, 240, 15)
    assert sum(counts.values()) == 240
    assert counts["d"] == 10  # all it has, fewer than the minimum
    assert counts["c"] == 15  # raised to the minimum
    assert counts["a"] > counts["b"] > counts["c"]
    assert abs(counts["a"] / counts["b"] - 2) < 0.05  # the rest stay in proportion
    assert reading.allocate({"a": 3, "b": 4}, 50, 15) == {"a": 3, "b": 4}


def test_the_sample_is_the_same_every_time_and_moves_with_the_seed():
    candidates = [
        reading.Candidate(
            "setX",
            1,
            "arch.pdf",
            page,
            "architect",
            "architect_strokes",
            reason,
            (float(n), 0.0, float(n) + 5, 8.0),
        )
        for page in (1, 2, 3)
        for n in range(40)
        for reason in ("sideways", "crowded")
    ]
    first = reading.sample(candidates, total=30, control_each=0, minimum=5)
    again = reading.sample(list(reversed(candidates)), total=30, control_each=0, minimum=5)
    assert [c.id for c in first] == [c.id for c in again]
    assert len(first) == 30
    other = reading.sample(candidates, total=30, control_each=0, minimum=5, seed="another")
    assert {c.id for c in other} != {c.id for c in first}
    assert reading.label_id("setX", 1, 2, (1, 2, 3, 4)) == reading.label_id(
        "setX", 1, 2, (1.0, 2.0, 3.0, 4.0)
    )


def test_the_code_crop_stops_short_of_its_neighbours_and_never_inside_the_label():
    box = (100.0, 100.0, 120.0, 108.0)
    left = (90.0, 101.0, 98.0, 107.0)  # 2 pt to the left, in the same row
    below = (100.0, 110.0, 120.0, 118.0)  # 2 pt below
    overlapping = (110.0, 104.0, 130.0, 106.0)  # crosses the label: never clipped against
    crop = reading.code_crop(box, [box, left, below, overlapping])
    assert crop[0] == pytest.approx(98.0 + reading._GAP_X_PT)
    assert crop[3] == pytest.approx(110.0 - reading._GAP_Y_PT)
    assert crop[1] == pytest.approx(100.0 - reading._PAD_PT)
    assert crop[2] == pytest.approx(120.0 + reading._PAD_PT)
    # A neighbour touching the label still leaves the whole label in the crop.
    tight = reading.code_crop(box, [(80.0, 100.0, 100.0, 108.0)])
    assert tight[0] <= box[0] and tight[2] >= box[2]


# ---- the key ----------------------------------------------------------------------------------


def _row(**more) -> KeyRow:
    base = {
        "id": "rd-1",
        "job": "setX",
        "file_index": 1,
        "page": 2,
        "side": "architect",
        "reason": "sideways",
        "box": (100.0, 100.0, 110.0, 130.0),
        "file": "arch.pdf",
    }
    base.update(more)
    return KeyRow(**base)


def test_the_key_round_trips_and_is_never_overwritten(tmp_path):
    path = tmp_path / "key.tsv"
    row = reading.with_proposal(
        _row(crops=("crops/a.png", "crops/a-t90.png"), also=("crowded",)), '34 1/2"', 90, "sure"
    )
    reading.write_key(path, [row])
    back = reading.read_key(path)[0]
    assert back.proposed_text == '34 1/2"' and back.proposed_turn == 90
    assert back.crops == ("crops/a.png", "crops/a-t90.png") and back.also == ("crowded",)
    assert back.box == row.box
    assert path.read_text().splitlines()[0].split("\t")[: len(reading.KEY_COLUMNS)] == list(
        reading.KEY_COLUMNS
    )
    with pytest.raises(FileExistsError):
        reading.write_key(path, [row])
    with pytest.raises(ValueError):
        reading.with_proposal(row, "1", 45, "sure")
    with pytest.raises(ValueError):
        reading.with_proposal(row, "1", 0, "maybe")


def test_the_confirmation_page_escapes_every_printed_value():
    row = reading.with_proposal(_row(crops=("crops/a.png",)), '<b>6"</b>', 0, "unsure")
    page = reading.index_html([row], "t")
    assert '<b>6"</b>' not in page and "&lt;b&gt;6&quot;&lt;/b&gt;" in page
    assert 'data-id="rd-1"' in page and "crops/a.png" in page


def test_finishing_joins_the_proposals_and_refuses_a_second_key(tmp_path):
    script = _script("build_reading_exam")
    reading.write_key(tmp_path / "candidates.tsv", [_row(), _row(id="rd-2")])
    proposals = tmp_path / "proposals.tsv"
    proposals.write_text(
        "id\tproposed_text\tproposed_turn\tconfidence\nrd-1\t68\t270\tsure\n", encoding="utf-8"
    )
    assert script.finish(tmp_path, proposals) == 0
    rows = {row.id: row for row in reading.read_key(tmp_path / "key.tsv")}
    assert rows["rd-1"].proposed_text == "68" and rows["rd-1"].proposed_turn == 270
    assert rows["rd-2"].proposed_text == "" and rows["rd-2"].confidence == ""
    assert (tmp_path / "index.html").is_file()
    with pytest.raises(SystemExit):
        script.finish(tmp_path, proposals)
    with pytest.raises(SystemExit):
        script._outside_repo(ROOT / "eval")


# ---- the scorer -------------------------------------------------------------------------------


def _read(
    text,
    inches,
    state="sealed_witnessed",
    box=(100.0, 100.0, 110.0, 130.0),
    turn=90,
    file="arch.pdf",
    page=2,
):
    return reading_score.Read("L1", file, page, box, state, text, inches, turn)


def test_an_upside_down_68_sealed_as_89_is_agreed_wrong():
    """The case the 180-degree guard exists for: "68" turned over reads "89" and both readers
    would agree. Sealed, it is the one failure the gate allows none of."""
    row = _row(proposed_text="68", proposed_turn=270, confidence="sure", confirmed="yes")
    per, overall, judged, _ = reading_score.score([row], [_read("89", None, turn=90)])
    assert judged[0].outcome == "agreed_wrong"
    assert overall.agreed_wrong == 1 and per["sideways"].agreed_wrong == 1
    # With inch marks it is judged in exact inches, and still wrong.
    marked = _row(proposed_text='68"', confidence="sure", confirmed="yes")
    assert reading_score.score([marked], [_read('89"', Fraction(89))])[1].agreed_wrong == 1
    # The same label read the right way up is right.
    assert reading_score.score([row], [_read("68", None, turn=270)])[1].right == 1


def test_readings_are_compared_in_exact_inches():
    row = _row(proposed_text="2'-10 1/2\"", confidence="sure", confirmed="yes")
    assert reading_score.truth_of(row).inches == Fraction(69, 2)
    right = reading_score.score([row], [_read('34 1/2"', Fraction(69, 2))])[1]
    assert right.right == 1 and right.sealed == 1 and right.witnessed == 1
    wrong = reading_score.score([row], [_read('34 3/4"', Fraction(139, 4))])[1]
    assert wrong.agreed_wrong == 1
    corrected = _row(proposed_text='34 3/4"', confidence="unsure", confirmed='34 1/2"')
    assert reading_score.score([corrected], [_read(None, Fraction(69, 2))])[1].right == 1


def test_held_unread_unmatched_unconfirmed_and_unverifiable_never_count_as_wrong():
    rows = [
        _row(id="a", proposed_text="5", confidence="sure", confirmed="yes"),
        _row(id="b", proposed_text="5", confidence="sure", confirmed="yes", page=3),
        _row(id="c", proposed_text="5", confidence="sure", confirmed="yes", page=4),
        _row(id="d", proposed_text="5", confidence="sure", confirmed=""),
        _row(id="e", proposed_text="", confidence="unreadable", confirmed="yes", page=5),
        _row(
            id="f", proposed_text=reading.NOT_A_NUMBER, confidence="sure", confirmed="yes", page=6
        ),
    ]
    reads = [
        _read(None, None, state="held"),
        _read(None, None, state="unread", page=3),
        _read("7", None, page=5),
        _read('6"', Fraction(6), page=6),
    ]
    _, overall, judged, outside = reading_score.score(rows, reads)
    outcomes = {item.row.id: item.outcome for item in judged}
    assert outcomes == {
        "a": "held",
        "b": "unread",
        "c": "unmatched",
        "d": "unconfirmed",
        "e": "unverifiable",
        "f": "agreed_wrong",  # a value sealed on a spot that prints no number
    }
    assert overall.held == overall.unread == overall.unmatched == overall.unverifiable == 1
    assert overall.unconfirmed == 1 and overall.agreed_wrong == 1 and outside == 0
    assert overall.read_rate == pytest.approx(2 / 4)  # sealed of the matched, confirmed


def test_a_key_row_matches_its_label_by_file_page_and_overlap():
    row = _row(file="Arch_drawings_only.pdf")
    near = _read("1", None, box=(101.0, 101.0, 111.0, 131.0), file="arch")
    far = _read("1", None, box=(105.0, 120.0, 115.0, 150.0), file="arch")
    assert reading_score.match([row], [far, near]) == {"rd-1": near}
    assert reading_score.match([row], [_read("1", None, page=9)]) == {}
    # A one-stroke box (no width) still overlaps its label.
    thin = _row(box=(50.0, 10.0, 50.0, 30.0))
    assert reading_score.match([thin], [_read("1", None, box=(49.6, 10.0, 50.4, 30.0))])
    # Without a file column, the pack's dims.json names the file.
    bare = _row(file="")
    assert reading_score.match([bare], [near], files={("setX", 1): "arch.pdf"}) == {"rd-1": near}


def test_a_reads_file_of_another_schema_or_state_is_refused(tmp_path):
    path = tmp_path / "reads.json"
    path.write_text(json.dumps({"schema": "something-else/1", "labels": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        reading_score.load_reads(path)
    path.write_text(
        json.dumps(
            {
                "schema": reading_score.SCHEMA,
                "labels": [{"id": "x", "file": "a", "page": 1, "box": [0, 0, 1, 1], "state": "ok"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        reading_score.load_reads(path)


def _reads_file(path: Path, text: str, inches: str | None) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": reading_score.SCHEMA,
                "pack_version": 27,
                "readers": [{"model": "m1", "maker": "a"}, {"model": "m2", "maker": "b"}],
                "labels": [
                    {
                        "id": "L1",
                        "file": "arch.pdf",
                        "page": 2,
                        "side": "architect",
                        "box": [100, 100, 110, 130],
                        "reasons": ["sideways"],
                        "turns_tried": [90, 270],
                        "answers": [{"model": "m1", "turn": 90, "text": text}],
                        "state": "sealed_unwitnessed",
                        "why": None,
                        "turn": 90,
                        "text": text,
                        "inches": inches,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_the_scoring_command_exits_non_zero_on_any_agreed_wrong(tmp_path, capsys):
    script = _script("score_reading_exam")
    key = tmp_path / "key.tsv"
    reading.write_key(key, [_row(proposed_text='68"', proposed_turn=270, confidence="sure")])
    answers = tmp_path / "answers.tsv"
    answers.write_text("id\tconfirmed\nrd-1\tyes\n", encoding="utf-8")
    reads = tmp_path / "reads.json"
    _reads_file(reads, '89"', "89")
    code = script.main(["--key", str(key), "--reads", str(reads), "--answers", str(answers)])
    assert code == 1
    assert "AGREED-WRONG rd-1" in capsys.readouterr().out
    _reads_file(reads, '68"', "68")
    out_json = tmp_path / "score.json"
    code = script.main(
        [
            "--key",
            str(key),
            "--reads",
            str(reads),
            "--answers",
            str(answers),
            "--json",
            str(out_json),
        ]
    )
    assert code == 0
    assert json.loads(out_json.read_text())["overall"]["right"] == 1
