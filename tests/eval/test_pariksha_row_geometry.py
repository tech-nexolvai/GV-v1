"""Row keys by geometry, not by box number (#1321).

Verification for: `eval/pariksha/row_geometry.py`, the geometry parts of `eval/pariksha/keys.py`
and `score_rekha`'s geometry matching. Every page, picture, line and answer here is invented; no
model is called.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest

from eval.pariksha.keys import (
    KEY_FORMAT,
    AnswerKey,
    KeyFormatError,
    RowAt,
    RowTruth,
    dump_key,
    load_key,
)
from eval.pariksha.row_geometry import (
    ROW_BOXES_FORMAT,
    PictureBoxes,
    RowBoxes,
    RowBoxesError,
    dump_row_boxes,
    load_row_boxes,
    matching_boxes,
    prove_picture_boxes,
    to_geometry_key,
)
from eval.pariksha.score import AGREED_RIGHT, AGREED_WRONG, NOT_ASKED, SPLIT, score_rekha
from eval.pariksha.stored import QuestionIdentity, StoredAttempt, StoredQuestion, StoredRun

PAGE = "a" * 64
PICTURE = "3" * 64
RENUMBERED = "4" * 64
OPUS = "reader-one"
SONNET = "reader-two"

LINE = {"y": 412.5, "x0": 100, "x1": 400}
"""The invented countertop row: the line the key names."""
CABINETS = {"y": 430.25, "x0": 110, "x1": 390}
"""Another invented line on the same page, 17.75 pt lower."""
UPPERS = {"y": 120, "x0": 100, "x1": 400}


# --- helpers ---------------------------------------------------------------------------------


def _rows_key(*cases: dict[str, Any], tolerance: Any = None, name: str = "rows") -> AnswerKey:
    data: dict[str, Any] = {
        "format": KEY_FORMAT,
        "agent": "rekha",
        "name": name,
        "version": "1",
        "cases": list(cases),
    }
    if tolerance is not None:
        data["row_tolerance_pt"] = tolerance
    key = load_key(data)
    assert isinstance(key, AnswerKey)
    return key


def _case(question_id: str = "p1:row-choice", **fields: Any) -> dict[str, Any]:
    return {"family": "slot-row-choice", "page_sha256": PAGE, "question_id": question_id, **fields}


def _boxes(**pictures: list[dict[str, Any]]) -> RowBoxes:
    """`PICTURE=[...]`-style: each picture's numbered boxes, box 1 first."""
    named = {"PICTURE": PICTURE, "RENUMBERED": RENUMBERED}
    return load_row_boxes(
        {
            "format": ROW_BOXES_FORMAT,
            "pictures": [
                {
                    "picture_sha256": named[label],
                    "page_sha256": PAGE,
                    "boxes": boxes,
                    "how": "invented for a test",
                }
                for label, boxes in pictures.items()
            ],
        }
    )


def _row(row: int, kind: str = "row") -> str:
    return json.dumps({"row": row, "kind": kind, "also": [], "why": "invented"})


def _question(
    first: str, second: str, *, picture: str = PICTURE, boxes: int = 2, question_id: str = "p1"
) -> StoredQuestion:
    packet = {
        "question_id": f"{question_id}:row-choice",
        "page_index": 1,
        "prompt_id": "slot-row-choice-v5",
        "source_page_sha256": PAGE,
        "packet_sha256": "p" * 8,
        "candidate_count": boxes,
        "images": {"numbered_vendor_view": {"sha256": picture, "storage_key": None}},
    }
    return StoredQuestion(
        identity=QuestionIdentity(
            family="slot-row-choice", page_sha256=PAGE, question_id=f"{question_id}:row-choice"
        ),
        prompt_id="slot-row-choice-v5",
        packet=packet,
        slot_box=None,
        attempts=tuple(
            StoredAttempt(
                model_id=model, outcome="ok", raw_response=raw, cost_micros=0, reused=False
            )
            for model, raw in ((OPUS, first), (SONNET, second))
        ),
    )


def _run(*questions: StoredQuestion) -> StoredRun:
    return StoredRun(
        database="synthetic",
        extraction_run_id="run-1",
        extractor="extraction.form_reader",
        extractor_version="slot-reader-test",
        config_hash="test",
        questions=questions,
    )


def _scored(card: Any) -> list[tuple[str, tuple[str, ...]]]:
    """Each case's pair outcome and each reader's verdict: what must not change on conversion."""
    return [(case.outcome, tuple(r.verdict for r in case.readers)) for case in card.cases]


# --- the key format --------------------------------------------------------------------------


def test_a_geometry_row_case_names_its_line_and_the_key_states_its_tolerance() -> None:
    key = _rows_key(_case(answer={"row_at": LINE}), tolerance=1)

    (case,) = key.cases
    assert key.row_tolerance_pt == Decimal(1)
    assert case.truth == RowTruth(
        box=None, kind="row", at=RowAt(Decimal("412.50"), Decimal("100.00"), Decimal("400.00"))
    )
    assert case.picture_sha256 is None
    assert "412.5" in str(case.truth)
    # Written back in the same shape, so it reads back to the same key (and the same hash).
    assert load_key(dump_key(key)) == key


def test_a_geometry_case_needs_a_tolerance_the_key_states_and_a_sane_one() -> None:
    with pytest.raises(KeyFormatError, match="row_tolerance_pt"):
        _rows_key(_case(answer={"row_at": LINE}))
    for wrong in (0, -1, 6, "wide", True):
        with pytest.raises(KeyFormatError, match="row_tolerance_pt"):
            _rows_key(_case(answer={"row_at": LINE}), tolerance=wrong)
    # A tolerance on a key that matches nothing by geometry is a typo, not a setting.
    with pytest.raises(KeyFormatError, match="row_tolerance_pt"):
        _rows_key(_case(picture_sha256=PICTURE, answer={"box": 1}), tolerance=1)


@pytest.mark.parametrize(
    "line",
    [
        {"y": 1, "x0": 5},
        {"y": 1, "x0": 5, "x1": 9, "z": 0},
        {"y": "a lot", "x0": 5, "x1": 9},
        {"y": 1, "x0": 9, "x1": 5},
        {"y": float("nan"), "x0": 5, "x1": 9},
    ],
)
def test_a_malformed_line_is_refused(line: dict[str, Any]) -> None:
    with pytest.raises(KeyFormatError, match="row_at"):
        _rows_key(_case(answer={"row_at": line}), tolerance=1)


def test_a_box_case_still_needs_its_picture_but_a_geometry_or_no_countertop_case_does_not() -> None:
    with pytest.raises(KeyFormatError, match="picture_sha256"):
        _rows_key(_case(answer={"box": 1}))
    with pytest.raises(KeyFormatError, match="picture_sha256"):
        _rows_key(_case(answer={"kind": "not_among_boxes"}))
    key = _rows_key(
        _case(answer={"row_at": LINE}),
        _case("p2:row-choice", answer={"kind": "no_countertop"}),
        tolerance=1,
    )
    assert [case.picture_sha256 for case in key.cases] == [None, None]


def test_an_existing_box_number_key_reads_and_hashes_exactly_as_before() -> None:
    """Registered keys are checked by hash: the new fields must not move an old key's hash."""
    data = {
        "format": KEY_FORMAT,
        "agent": "rekha",
        "name": "rows",
        "version": "1",
        "cases": [_case(picture_sha256=PICTURE, answer={"box": 2}, source="by eye")],
    }
    key = _rows_key(*data["cases"])  # type: ignore[arg-type]

    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert key.sha256 == hashlib.sha256(canonical.encode()).hexdigest()
    assert dump_key(key) == data
    assert key.row_tolerance_pt is None


def test_who_confirmed_a_case_is_kept_and_changes_the_hash() -> None:
    plain = _rows_key(_case(answer={"row_at": LINE}), tolerance=1)
    confirmed = _rows_key(_case(answer={"row_at": LINE}, confirmed_by="a person"), tolerance=1)

    assert plain.cases[0].confirmed_by == ""
    assert confirmed.cases[0].confirmed_by == "a person"
    assert confirmed.sha256 != plain.sha256
    with pytest.raises(KeyFormatError, match="confirmed_by"):
        _rows_key(_case(answer={"row_at": LINE}, confirmed_by=" "), tolerance=1)
    # Only a person confirms: never an AI, an agent or code, however it is spelled.
    for machine in ("Claude Opus", "codex-agent", "C1aude", "code:converter"):
        with pytest.raises(KeyFormatError, match="only a person confirms"):
            _rows_key(_case(answer={"row_at": LINE}, confirmed_by=machine), tolerance=1)


# --- the box map -----------------------------------------------------------------------------


def test_the_box_map_reads_strictly_and_writes_back_the_same() -> None:
    boxes = _boxes(PICTURE=[CABINETS, LINE])

    picture = boxes.get(PICTURE, PAGE)
    assert isinstance(picture, PictureBoxes)
    assert picture.boxes[1] == RowAt.of(LINE["y"], LINE["x0"], LINE["x1"])
    assert load_row_boxes(dump_row_boxes(boxes)) == boxes
    assert boxes.get(RENUMBERED, PAGE) is None
    assert boxes.get(PICTURE, "b" * 64) is None

    good = dump_row_boxes(boxes)
    for broken in (
        {**good, "extra": 1},
        {**good, "format": "pariksha-row-boxes/0"},
        {**good, "pictures": [{**good["pictures"][0], "picture_sha256": "short"}]},
        {**good, "pictures": [{**good["pictures"][0], "boxes": []}]},
        {**good, "pictures": [{**good["pictures"][0], "how": ""}]},
        {**good, "pictures": [{**good["pictures"][0], "colour": "green"}]},
    ):
        with pytest.raises(RowBoxesError):
            load_row_boxes(broken)


def test_one_picture_named_twice_with_other_boxes_is_refused_also_when_merging() -> None:
    first = _boxes(PICTURE=[CABINETS, LINE])
    other = _boxes(PICTURE=[LINE, CABINETS])

    assert first.merged(_boxes(PICTURE=[CABINETS, LINE])) == first
    with pytest.raises(RowBoxesError, match="other boxes"):
        first.merged(other)
    doubled = dump_row_boxes(first)
    doubled["pictures"] = doubled["pictures"] + dump_row_boxes(other)["pictures"]
    with pytest.raises(RowBoxesError, match="other boxes"):
        load_row_boxes(doubled)


def test_matching_finds_every_box_within_the_tolerance() -> None:
    picture = _boxes(PICTURE=[CABINETS, LINE, {"y": 413, "x0": 100.5, "x1": 399.5}]).get(
        PICTURE, PAGE
    )
    assert picture is not None
    line = RowAt.of(LINE["y"], LINE["x0"], LINE["x1"])

    assert matching_boxes(line, picture, Decimal("0.25")) == (2,)
    assert matching_boxes(line, picture, Decimal("0.5")) == (2, 3)
    # The x-range counts too: a line as high as the key's but shorter is another line.
    short = RowAt.of(LINE["y"], 150, 300)
    assert matching_boxes(short, picture, Decimal(1)) == ()


# --- scoring ---------------------------------------------------------------------------------


def test_the_keyed_line_is_followed_to_a_renumbered_picture() -> None:
    """Box 2 on one picture and box 1 on another are the same line: both readers are right."""
    key = _rows_key(_case(answer={"row_at": LINE}), tolerance=1)
    boxes = _boxes(PICTURE=[CABINETS, LINE], RENUMBERED=[LINE, CABINETS])

    on_first = score_rekha(
        key, _run(_question(_row(2), _row(2))), code_version="abc1234", row_boxes=boxes
    )
    on_renumbered = score_rekha(
        key,
        _run(_question(_row(1), _row(1), picture=RENUMBERED)),
        code_version="abc1234",
        row_boxes=boxes,
    )
    wrong_line = score_rekha(
        key,
        _run(_question(_row(2), _row(2), picture=RENUMBERED)),
        code_version="abc1234",
        row_boxes=boxes,
    )

    assert on_first.cases[0].outcome == AGREED_RIGHT
    assert on_renumbered.cases[0].outcome == AGREED_RIGHT
    assert "box 1" in on_renumbered.cases[0].expected
    assert wrong_line.cases[0].outcome == AGREED_WRONG


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (_row(2), _row(2)),
        (_row(1), _row(1)),
        (_row(2), _row(1)),
        (_row(0, "not_among_boxes"), _row(0, "not_among_boxes")),
        (_row(0, "no_countertop"), _row(0, "no_countertop")),
        (_row(0, "unsure"), _row(2)),
    ],
)
def test_a_converted_key_scores_exactly_as_its_box_number_key_on_the_same_picture(
    first: str, second: str
) -> None:
    by_box = _rows_key(
        _case(picture_sha256=PICTURE, answer={"box": 2}),
        _case("p2:row-choice", picture_sha256=PICTURE, answer={"kind": "no_countertop"}),
    )
    boxes = _boxes(PICTURE=[CABINETS, LINE])
    by_line = to_geometry_key(
        by_box, boxes, tolerance=Decimal(1), name="rows-geometry", version="2"
    )
    run = _run(
        _question(first, second),
        _question(first, second, question_id="p2"),
    )

    before = score_rekha(by_box, run, code_version="abc1234")
    after = score_rekha(by_line, run, code_version="abc1234", row_boxes=boxes)

    assert _scored(after) == _scored(before)
    assert by_line.cases[0].truth.at == RowAt.of(LINE["y"], LINE["x0"], LINE["x1"])  # type: ignore[union-attr]
    assert by_line.cases[0].picture_sha256 is None
    assert "box 2" in by_line.cases[0].source
    assert by_line.cases[1].truth == RowTruth(box=None, kind="no_countertop")
    assert by_line.cases[1].picture_sha256 is None
    assert (by_line.name, by_line.version, by_line.row_tolerance_pt) == (
        "rows-geometry",
        "2",
        Decimal(1),
    )


def test_a_keyed_line_that_is_not_among_the_boxes_expects_not_among_the_boxes() -> None:
    key = _rows_key(_case(answer={"row_at": LINE}), tolerance=1)
    boxes = _boxes(PICTURE=[CABINETS, UPPERS])

    agreed_box = score_rekha(
        key, _run(_question(_row(1), _row(1))), code_version="abc1234", row_boxes=boxes
    )
    both_not_among = score_rekha(
        key,
        _run(_question(_row(0, "not_among_boxes"), _row(0, "not_among_boxes"))),
        code_version="abc1234",
        row_boxes=boxes,
    )

    assert agreed_box.cases[0].outcome == AGREED_WRONG
    assert "not_among_boxes" in agreed_box.cases[0].expected
    # Two "not among the boxes" still go to a person, so the pair is split; each reader is right.
    assert both_not_among.cases[0].outcome == SPLIT
    assert [r.verdict for r in both_not_among.cases[0].readers] == ["right", "right"]


def test_a_geometry_case_is_never_guessed_without_the_pictures_boxes() -> None:
    key = _rows_key(_case(answer={"row_at": LINE}), tolerance=1)
    run = _run(_question(_row(2), _row(2)))

    no_map = score_rekha(key, run, code_version="abc1234")
    other_picture = score_rekha(
        key, run, code_version="abc1234", row_boxes=_boxes(RENUMBERED=[CABINETS, LINE])
    )
    other_count = score_rekha(
        key, run, code_version="abc1234", row_boxes=_boxes(PICTURE=[CABINETS, LINE, UPPERS])
    )

    for card, words in (
        (no_map, "no box geometry"),
        (other_picture, "no box geometry"),
        (other_count, "3 boxes"),
    ):
        (case,) = card.cases
        assert case.outcome == NOT_ASKED
        assert words in case.note
    assert no_map.metrics["asked"].numerator == 0


def test_two_boxes_within_the_tolerance_are_never_chosen_between() -> None:
    key = _rows_key(_case(answer={"row_at": LINE}), tolerance=1)
    close = {"y": 412.9, "x0": 100, "x1": 400}
    run = _run(_question(_row(1), _row(1)))

    card = score_rekha(key, run, code_version="abc1234", row_boxes=_boxes(PICTURE=[LINE, close]))

    (case,) = card.cases
    assert case.outcome == NOT_ASKED
    assert "boxes 1 and 2" in case.note


def test_one_picture_on_two_pages_is_two_entries_and_only_this_pages_counts() -> None:
    """Two drawing sets holding the same drawing give byte-identical pictures on two pages."""
    key = _rows_key(_case(answer={"row_at": LINE}), tolerance=1)
    data = dump_row_boxes(_boxes(PICTURE=[CABINETS, LINE]))
    other_page = {**data["pictures"][0], "page_sha256": "b" * 64, "boxes": [LINE, CABINETS]}
    only_other = load_row_boxes({**data, "pictures": [other_page]})
    both = load_row_boxes({**data, "pictures": [*data["pictures"], other_page]})
    run = _run(_question(_row(2), _row(2)))

    assert len(both.pictures) == 2
    assert score_rekha(key, run, code_version="a1b2c3d", row_boxes=both).cases[0].outcome == (
        AGREED_RIGHT
    )
    (case,) = score_rekha(key, run, code_version="a1b2c3d", row_boxes=only_other).cases
    assert case.outcome == NOT_ASKED
    assert "no box geometry" in case.note


# --- converting a box-number key -------------------------------------------------------------


def test_conversion_refuses_what_it_cannot_prove() -> None:
    by_box = _rows_key(_case(picture_sha256=PICTURE, answer={"box": 2}))

    with pytest.raises(RowBoxesError, match="no box geometry"):
        to_geometry_key(
            by_box, _boxes(RENUMBERED=[LINE]), tolerance=Decimal(1), name="g", version="2"
        )
    with pytest.raises(RowBoxesError, match="box 2"):
        to_geometry_key(by_box, _boxes(PICTURE=[LINE]), tolerance=Decimal(1), name="g", version="2")
    # The keyed box must be the only one within the tolerance on its own picture.
    close = {"y": 412.9, "x0": 100, "x1": 400}
    with pytest.raises(RowBoxesError, match="tolerance"):
        to_geometry_key(
            by_box, _boxes(PICTURE=[close, LINE]), tolerance=Decimal(1), name="g", version="2"
        )


def test_conversion_keeps_a_not_among_the_boxes_case_on_its_own_picture() -> None:
    """Whether a line is among the boxes depends on the picture: that case stays tied to it."""
    by_box = _rows_key(_case(picture_sha256=PICTURE, answer={"kind": "not_among_boxes"}))

    converted = to_geometry_key(
        by_box, _boxes(PICTURE=[LINE]), tolerance=Decimal(1), name="g", version="2"
    )

    assert converted.cases[0].picture_sha256 == PICTURE
    assert converted.row_tolerance_pt is None  # no geometry case, so no tolerance to state


# --- the box map is the product's own picture code, proven byte for byte ---------------------


def test_the_boxes_are_proven_by_drawing_the_stored_picture_again_byte_for_byte() -> None:
    from tests.workflow.test_slot_reader import named_sheet, slot_page
    from workflow.slot_reader import _numbered_rows_png

    page = slot_page(named_sheet())
    png, candidates = _numbered_rows_png(page)
    stored = hashlib.sha256(png).hexdigest()

    proven = prove_picture_boxes(page, picture_sha256=stored, page_sha256=PAGE)

    assert isinstance(proven, PictureBoxes)
    assert proven.boxes == tuple(RowAt.of(c.y, c.x0, c.x1) for c in candidates)
    assert "byte for byte" in proven.how
    refused = prove_picture_boxes(page, picture_sha256="0" * 64, page_sha256=PAGE)
    assert isinstance(refused, str) and "not drawn again" in refused


def test_a_picture_drawn_in_an_earlier_palette_is_proven_with_that_palette() -> None:
    from eval.pariksha.row_geometry import EARLIER_PALETTES
    from tests.workflow.test_slot_reader import named_sheet, slot_page
    from workflow import slot_reader

    page = slot_page(named_sheet())
    (name, palette), *_ = EARLIER_PALETTES.items()
    with mock.patch.object(slot_reader, "_ROW_COLOURS", palette):
        png, _ = slot_reader._numbered_rows_png(page)

    proven = prove_picture_boxes(
        page, picture_sha256=hashlib.sha256(png).hexdigest(), page_sha256=PAGE
    )

    assert isinstance(proven, PictureBoxes)
    assert name in proven.how
