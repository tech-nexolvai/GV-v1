"""The agreement gate replayed against a person's answers, and the guards measured round it (#851).

Verification for: `eval/experiments/gate_replay.py`.

The one that matters most is `test_an_agreed_wrong_pair_is_counted`: two readers from different
vendors agreeing on `3 3/4"` where the person read `3/4"` is the failure the gate exists to prevent,
and the bake-off's pair score never counted it.

No model is called and no client drawing is read: every reading here is made up.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

import evidence.corroborate
from eval.experiments import gate_replay
from eval.experiments.agent_scorecard import KeyCrop, Kind, load_key
from eval.experiments.gate_replay import (
    Facts,
    Guard,
    Outcome,
    Reading,
    Region,
    Replay,
    ReplayError,
    StoredRow,
    frame_of,
    gate,
    group_rows,
    held_back,
    join,
    judge,
    outcome,
    regions_from_scorecard,
    render_markdown,
    rule_of_three,
    stacked_catch,
    tally,
)
from eval.experiments.model_bakeoff import KeyFrame
from evidence.candidate import ObservationCandidate
from evidence.canonical import CorroborationLane, EvidenceStatus
from evidence.corroborate import CorroborationResult
from tests.eval.test_agent_scorecard import _row, _write_key
from units.dual import DualDimension
from units.measurement import Measurement, Unit

NOVA = ("bedrock-nova-2-lite", "amazon.nova-2-lite-v1:0")
MINI = ("bedrock-ministral-3-3b", "mistral.ministral-3-3b-instruct")
LARGE = ("bedrock-mistral-large-3", "mistral.mistral-large-3-675b-instruct")
STAMP = ("extraction.stamp_text", "extraction.stamp_text/1")
OCR = ("rapidocr", "extraction.ocr.rapidocr/2")

MODEL_IDS = {NOVA[0]: NOVA[1], MINI[0]: MINI[1], LARGE[0]: LARGE[1]}

FRAME = KeyFrame(polygon_dpi=600, margin_pt=Decimal(9))
"""A key cut as production cuts a crop: 600 dpi, 9 pt round each region."""

PLAIN = Facts(cut_at_edge=False, sideways=False, stacked=False, gv_mark=False)


def _inches(value: int | Fraction) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, f'{value}"')


def _read(who: tuple[str, str], text: str, value: int | Fraction | None) -> Reading:
    return Reading(
        extractor=who[0],
        extractor_version=who[1],
        raw_text=text,
        value=None if value is None else _inches(value),
    )


def _crop(
    crop_id: str,
    expected: int | Fraction | None,
    *,
    kind: Kind = Kind.SCORED,
    stacked: bool = False,
    page_index: int = 0,
    crop_px: tuple[int, int, int, int] = (1000, 1000, 1200, 1100),
) -> KeyCrop:
    return KeyCrop(
        crop_id=crop_id,
        page_index=page_index,
        crop_px=crop_px,
        kind=kind,
        expected=None if expected is None else _inches(expected),
        stratum="-",
        rotated=False,
        cut_off=False,
        stacked=stacked,
    )


def _region(handle: str, *readings: Reading, page_index: int = 0) -> Region:
    return Region(
        handle=handle,
        page_index=page_index,
        box_px=(0, 0, 10, 10),
        dpi=300,
        readings=tuple(readings),
    )


def _replay(rows: Sequence[tuple[KeyCrop, Region, Facts]], *, source: str = "synthetic") -> Replay:
    return Replay(
        source=source,
        crops=tuple(crop for crop, _, _ in rows),
        regions={crop.crop_id: (region,) for crop, region, _ in rows},
        facts={region.handle: facts for _, region, facts in rows},
    )


def _agreeing(handle: str, text: str, value: int | Fraction) -> Region:
    """Two readers from different vendors agreeing on one value."""
    return _region(handle, _read(NOVA, text, value), _read(MINI, text, value))


# ---------------------------------------------------------------------------
# Agreed and wrong, agreed and right, agreed on what nobody could read
# ---------------------------------------------------------------------------


def test_an_agreed_wrong_pair_is_counted() -> None:
    """**The #726 case.** A stacked `3/4"` read as `3 3/4"` by two vendors is agreed and wrong."""
    replay = _replay(
        [(_crop("c1", Fraction(3, 4)), _agreeing("r1", '3 3/4"', Fraction(15, 4)), PLAIN)]
    )

    counts = tally(replay, Guard.NONE)

    assert (counts.right, counts.wrong, counts.unvouched) == (0, 1, 0)
    assert counts.bound is None, "a wrong agreement was seen: the count is the finding"


def test_an_agreed_right_pair_is_counted_right_with_its_coverage() -> None:
    replay = _replay(
        [
            (_crop("c1", 12), _agreeing("r1", '12"', 12), PLAIN),
            (_crop("c2", 4), _region("r2", _read(NOVA, '4"', 4), _read(MINI, '5"', 5)), PLAIN),
            (_crop("c3", 2), _region("r3", _read(NOVA, '2"', 2)), PLAIN),
        ]
    )

    counts = tally(replay, Guard.NONE)

    assert (counts.right, counts.wrong, counts.unvouched) == (1, 0, 0)
    assert counts.coverage == Fraction(1, 3)
    assert counts.bound == Fraction(1), "3/1 is capped: one agreement bounds nothing below 100%"


def test_an_agreement_on_a_crop_nobody_could_read_is_counted_apart() -> None:
    replay = _replay(
        [
            (_crop("c1", None, kind=Kind.UNREADABLE), _agreeing("r1", '18"', 18), PLAIN),
            (_crop("c2", None, kind=Kind.COMPOUND), _agreeing("r2", '57"', 57), PLAIN),
        ]
    )

    counts = tally(replay, Guard.NONE)

    assert (counts.right, counts.wrong, counts.unvouched) == (0, 0, 2)
    assert counts.coverage is None and counts.bound is None


def test_one_wrong_agreement_among_a_crops_regions_makes_the_crop_wrong() -> None:
    crop = _crop("c1", 12)
    replay = Replay(
        source="synthetic",
        crops=(crop,),
        regions={"c1": (_agreeing("r1", '12"', 12), _agreeing("r2", '92"', 92))},
        facts={"r1": PLAIN, "r2": PLAIN},
    )

    assert outcome(crop, replay.agreements(crop, Guard.NONE)) is Outcome.WRONG


# ---------------------------------------------------------------------------
# The gate, called as it is
# ---------------------------------------------------------------------------


def test_the_gate_is_the_one_in_evidence() -> None:
    assert vars(gate_replay)["corroborate"] is evidence.corroborate.corroborate


def test_the_gate_is_asked_about_every_reading_of_a_region_in_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Asked as the stage asks it.** One call with every reading. The OCR row with no value
    abstains (#924), as it does in the stage, so the pair's agreement on `4"` is the region's."""
    calls: list[tuple[tuple[str, str | None], ...]] = []
    real = evidence.corroborate.corroborate

    def spy(
        candidates: Sequence[ObservationCandidate], *, dual_dimension: DualDimension | None = None
    ) -> CorroborationResult:
        calls.append(
            tuple(
                (
                    candidate.extractor,
                    None if candidate.parsed_value is None else str(candidate.parsed_value.exact),
                )
                for candidate in candidates
            )
        )
        return real(candidates, dual_dimension=dual_dimension)

    monkeypatch.setattr(gate_replay, "corroborate", spy)
    readings = (_read(OCR, "4", None), _read(MINI, '4"', 4), _read(NOVA, '4"', 4))

    assert gate(readings) == gate_replay.Agreement(
        _inches(4), CorroborationLane.SECOND_READER, '4"'
    ), "the text of a reading that agreed, never the blank one's"
    assert calls == [((OCR[0], None), (MINI[0], "4"), (NOVA[0], "4"))]


def test_two_vendors_agreeing_on_both_halves_of_a_models_dual_label_agree_it() -> None:
    """**#924, as the gate decides it.** The pair's `914 [36]`, beside the forced-tool reader's `914`
    with no value: agreed on the inches. The same inches with other millimetres: nothing."""
    pair = (
        Reading(NOVA[0], NOVA[1], "914 [36]", _inches(36)),
        Reading(MINI[0], MINI[1], "914 [36]", _inches(36)),
    )
    blank = _read(LARGE, "914", None)

    assert gate((blank, *pair)) == gate_replay.Agreement(
        _inches(36), CorroborationLane.SECOND_READER, "914 [36]"
    )
    assert gate((pair[0], Reading(MINI[0], MINI[1], "920 [36]", _inches(36)))) is None


def test_the_replay_follows_the_gate_and_never_overrules_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def conflicting(
        candidates: Sequence[ObservationCandidate], *, dual_dimension: DualDimension | None = None
    ) -> CorroborationResult:
        del dual_dimension
        ids = tuple(candidate.candidate_id for candidate in candidates)
        return CorroborationResult(
            EvidenceStatus.CONFLICTING, ids, ids, CorroborationLane.SECOND_READER
        )

    monkeypatch.setattr(gate_replay, "corroborate", conflicting)
    replay = _replay([(_crop("c1", 12), _agreeing("r1", '12"', 12), PLAIN)])

    assert tally(replay, Guard.NONE).right == 0


def test_two_readers_from_one_vendor_agree_nothing() -> None:
    """The gate's own rule (#775), which the replay inherits rather than restates."""
    assert gate((_read(MINI, "2'", 24), _read(LARGE, "2'", 24))) is None
    assert gate((_read(NOVA, "2'", 24), _read(MINI, "2'", 24))) == gate_replay.Agreement(
        _inches(24), CorroborationLane.SECOND_READER, "2'"
    )


def test_a_text_routes_own_dual_label_is_judged_alone_as_production_judges_it() -> None:
    """`app.evidence.record` gives a text route's `mm [in]` row its own lane, and the stage leaves
    it out of the region's group: the two models can still agree beside it."""
    stamp = Reading(STAMP[0], STAMP[1], "381 [15]", _inches(15))

    assert gate((stamp, _read(NOVA, '15"', 15), _read(MINI, '15"', 15))) == gate_replay.Agreement(
        _inches(15), CorroborationLane.SECOND_READER, '15"'
    )


def test_a_text_routes_dual_label_whose_halves_disagree_leaves_nothing_agreed() -> None:
    stamp = Reading(STAMP[0], STAMP[1], "999 [15]", _inches(15))

    assert gate((stamp, _read(NOVA, '15"', 15), _read(MINI, '15"', 15))) is None


def test_a_models_dual_reading_is_never_judged_by_its_own_millimetres() -> None:
    reading = Reading(NOVA[0], NOVA[1], "381 [15]", _inches(15))

    assert judge(_region("r1", reading), PLAIN, Guard.MM_ON_FILE_TEXT) is None


# ---------------------------------------------------------------------------
# The candidate guards, each measured
# ---------------------------------------------------------------------------


def test_the_whole_number_and_fraction_guard_is_the_production_gates_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**One test, not two copies of it (#924).** The replay asks `workflow.stages`'s own
    `mixed_fraction_refusal` about what the gate agreed, handed the agreed value and the text of a
    reading that agreed: whatever it says is what the replay's guard does."""
    from workflow import stages

    assert vars(gate_replay)["mixed_fraction_refusal"] is stages.mixed_fraction_refusal
    region = _agreeing("r1", '12"', 12)
    asked: list[tuple[Fraction, str]] = []

    def refuses(value: object, text: str) -> str:
        asked.append((value.exact, text))  # type: ignore[attr-defined]
        return "refused"

    monkeypatch.setattr(gate_replay, "mixed_fraction_refusal", refuses)

    assert judge(region, PLAIN, Guard.MIXED_FRACTION) is None
    assert judge(region, PLAIN, Guard.GATE) is None
    assert judge(region, PLAIN, Guard.GATE_919) is not None, "the gate before #924 never asks it"
    assert asked == [(Fraction(12), '12"'), (Fraction(12), '12"')]


def test_the_guard_holds_back_a_whole_number_and_a_fraction_and_lets_a_dual_label_through() -> None:
    """`3 3/4"` agreed is held back; `597 [23 1/2]` agreed on both halves is not."""
    mixed = _agreeing("r1", '3 3/4"', Fraction(15, 4))
    dual = _region(
        "r2",
        Reading(NOVA[0], NOVA[1], "597 [23 1/2]", _inches(Fraction(47, 2))),
        Reading(MINI[0], MINI[1], "597 [23 1/2]", _inches(Fraction(47, 2))),
    )

    assert judge(mixed, PLAIN, Guard.NONE) is not None
    assert judge(mixed, PLAIN, Guard.MIXED_FRACTION) is None
    assert judge(dual, PLAIN, Guard.MIXED_FRACTION) is not None


def test_the_sideways_guard_needs_a_third_reader_with_a_value() -> None:
    sideways = Facts(cut_at_edge=False, sideways=True, stacked=False, gv_mark=False)
    two = _agreeing("r1", '26 3/4"', Fraction(107, 4))
    three = _region("r2", *two.readings, _read(LARGE, '26 3/4"', Fraction(107, 4)))
    blank = _region("r3", *two.readings, _read(LARGE, "26", None))

    assert held_back(two, sideways, Guard.SIDEWAYS)
    assert not held_back(three, sideways, Guard.SIDEWAYS)
    assert held_back(blank, sideways, Guard.SIDEWAYS), "a reader with no value is no third reader"
    assert not held_back(two, PLAIN, Guard.SIDEWAYS)
    assert judge(three, sideways, Guard.SIDEWAYS) is not None


def test_mm_confirms_inches_only_on_the_files_own_text() -> None:
    stamp = _region("r1", Reading(STAMP[0], STAMP[1], "381 [15]", _inches(15)))
    ocr = _region("r2", Reading(OCR[0], OCR[1], "381 [15]", _inches(15)))

    assert judge(stamp, PLAIN, Guard.NONE) is None, "the sealing gate takes no mm lane today"
    agreed = judge(stamp, PLAIN, Guard.MM_ON_FILE_TEXT)
    assert agreed is not None and agreed.lane is CorroborationLane.DUAL_UNIT
    assert (agreed.value.exact, agreed.value.unit) == (
        Fraction(15),
        Unit.INCH,
    ), "the inches, never the mm"
    assert judge(ocr, PLAIN, Guard.MM_ON_FILE_TEXT) is None, "OCR is a reading of pixels"


#: One crop per guard, each a right or wrong agreement only that guard changes.
CUT = Facts(cut_at_edge=True, sideways=False, stacked=False, gv_mark=False)
MARKED = Facts(cut_at_edge=False, sideways=False, stacked=False, gv_mark=True)
SIDEWAYS = Facts(cut_at_edge=False, sideways=True, stacked=False, gv_mark=False)
GUARDED = [
    (_crop("cut", 92), _agreeing("r-cut", '92"', 92), CUT),  # right, cut off
    (_crop("gv", 34), _agreeing("r-gv", '56"', 56), MARKED),  # GV's value read as the vendor's
    (_crop("stack", Fraction(3, 4)), _agreeing("r-stack", '3 3/4"', Fraction(15, 4)), PLAIN),
    (_crop("side", 4), _agreeing("r-side", '4"', 4), SIDEWAYS),  # right, two readers only
    (
        _crop("mm", 15),
        _region("r-mm", Reading(STAMP[0], STAMP[1], "381 [15]", _inches(15))),
        PLAIN,
    ),
    (_crop("plain", 12), _agreeing("r-plain", '12"', 12), PLAIN),
]


@pytest.mark.parametrize(
    ("guard", "right", "wrong"),
    [
        (Guard.NONE, 3, 2),
        (Guard.CUT, 2, 2),
        (Guard.GV_MARK, 3, 1),
        (Guard.GATE_919, 2, 1),
        (Guard.MIXED_FRACTION, 3, 1),
        (Guard.GATE, 2, 0),
        (Guard.SIDEWAYS, 2, 2),
        (Guard.MM_ON_FILE_TEXT, 4, 2),
    ],
)
def test_each_guard_is_measured_against_the_gate_as_it_is(
    guard: Guard, right: int, wrong: int
) -> None:
    counts = tally(_replay(GUARDED), guard)

    assert (counts.right, counts.wrong) == (right, wrong)
    assert counts.scored == len(GUARDED)


# ---------------------------------------------------------------------------
# The rule of three
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("agreed", "wrong", "bound"),
    [
        (9, 0, Fraction(1, 3)),
        (300, 0, Fraction(1, 100)),
        (6, 0, Fraction(1, 2)),
        (3, 0, Fraction(1)),
        (1, 0, Fraction(1)),
        (0, 0, None),
        (5, 1, None),
    ],
)
def test_the_rule_of_three_bound(agreed: int, wrong: int, bound: Fraction | None) -> None:
    assert rule_of_three(agreed, wrong) == bound


@pytest.mark.parametrize(("agreed", "wrong"), [(-1, 0), (1, 2), (1, -1)])
def test_the_rule_of_three_refuses_counts_that_are_not_counts(agreed: int, wrong: int) -> None:
    with pytest.raises(ValueError):
        rule_of_three(agreed, wrong)


def test_the_report_never_calls_zero_wrong_safe() -> None:
    report = render_markdown(
        header="## Gate replay",
        crops=tuple(crop for crop, _, _ in GUARDED),
        stacked=stacked_catch([], {}),
        replays=[_replay([GUARDED[-1]])],
    )

    assert "safe" not in report.lower()
    assert "0 wrong is not 0 risk" in report
    assert "below 100.0% (0 wrong in 1)" in report


def test_the_report_states_a_wrong_agreement_unbounded() -> None:
    replay = _replay(
        [(_crop("c1", Fraction(3, 4)), _agreeing("r1", '3 3/4"', Fraction(15, 4)), PLAIN)]
    )
    report = render_markdown(
        header="## Gate replay", crops=replay.crops, stacked=stacked_catch([], {}), replays=[replay]
    )

    assert (
        f"| {Guard.NONE.value} | 0 | **1** | 0 | 1/1 (100.0%) | not bounded: 1 of 1 agreed wrong |"
        in report
    )


# ---------------------------------------------------------------------------
# Where a crop is, and what is joined to it
# ---------------------------------------------------------------------------

#: 1000–1300 × 1000–1200 px at 600 dpi is 120–156 × 120–144 pt. Less a 9 pt margin, its region is
#: 129–147 × 129–135 pt; with production's 9 pt round that, the crop the person read is the polygon.
WIDE = (1000, 1000, 1300, 1200)


def _at(handle: str, centre_px: tuple[int, int], *, page_index: int = 0) -> Region:
    """A 20 px region at 300 dpi round `centre_px`: 0.24 pt to the pixel."""
    x, y = centre_px
    return Region(handle, page_index, (x - 10, y - 10, x + 10, y + 10), 300, ())


def test_a_region_joins_a_crop_where_its_centre_lies_in_the_crop_the_person_read() -> None:
    crop = _crop("c1", 12, crop_px=WIDE)
    inside = _at("inside", (575, 550))  # centre (138, 132) pt
    edge = _at("edge", (650, 550))  # centre (156, 132) pt: on the crop's right edge
    outside = _at("outside", (670, 550))  # centre (160.8, 132) pt
    other_page = _at("other", (575, 550), page_index=1)

    joined = join(
        [crop], [inside, edge, outside, other_page], frame=FRAME, view_margin_pt=Decimal(9)
    )

    assert [region.handle for region in joined["c1"]] == ["inside", "edge"]


def test_the_keys_own_margin_decides_where_its_region_is() -> None:
    """A key cut round the region itself (the pilot's 0 pt) shows the person 9 pt more page than a
    key cut as production cuts (9 pt): the same polygon is a different crop."""
    crop = _crop("c1", 12, crop_px=WIDE)
    left_of_it = _at("left", (475, 542))  # centre (114, 130.08) pt
    tight = KeyFrame(polygon_dpi=600, margin_pt=Decimal(0))

    assert join([crop], [left_of_it], frame=tight, view_margin_pt=Decimal(9))["c1"] == (left_of_it,)
    assert join([crop], [left_of_it], frame=FRAME, view_margin_pt=Decimal(9))["c1"] == ()


def test_stored_rows_are_grouped_by_page_and_polygon_as_the_stage_groups_them() -> None:
    square = ((10, 10), (20, 10), (20, 20), (10, 20))
    other = ((30, 10), (40, 10), (40, 20), (30, 20))
    rows = [
        StoredRow(0, square, 300, _read(NOVA, '4"', 4)),
        StoredRow(0, square, 300, _read(MINI, '4"', 4)),
        StoredRow(0, other, 300, _read(OCR, "4", None)),
        StoredRow(1, square, 300, _read(NOVA, '5"', 5)),
    ]

    regions = group_rows(rows)

    assert [(region.page_index, len(region.readings)) for region in regions] == [
        (0, 2),
        (0, 1),
        (1, 1),
    ]
    assert regions[0].box_px == (10, 10, 20, 20)
    assert len({region.handle for region in regions}) == 3


def test_one_polygon_recorded_at_two_resolutions_is_refused() -> None:
    square = ((10, 10), (20, 10), (20, 20), (10, 20))

    with pytest.raises(ReplayError, match="different resolutions"):
        group_rows(
            [
                StoredRow(0, square, 300, _read(NOVA, '4"', 4)),
                StoredRow(0, square, 600, _read(MINI, '4"', 4)),
            ]
        )


def test_a_scorecards_pair_is_read_and_parsed_by_the_stages_rule() -> None:
    crops = [_crop("k1", 2, crop_px=WIDE), _crop("k2", 4, crop_px=WIDE)]
    rows = [
        {"crop": "k1", "pair": [[NOVA[0], '2"', None], [MINI[0], '2-1/2"', None]]},
        {"crop": "k2", "pair": [[NOVA[0], None, "refused"], [MINI[0], '4"', None]]},
    ]

    regions = regions_from_scorecard(rows, crops, frame=FRAME, stage_dpi=300, model_ids=MODEL_IDS)

    first = regions["k1"][0]
    assert [
        None if reading.value is None else (reading.value.exact, reading.value.unit)
        for reading in first.readings
    ] == [(2, Unit.INCH), (Fraction(5, 2), Unit.INCH)], 'a hyphenated `2-1/2"` is read as 2 1/2'
    assert [reading.extractor_version for reading in first.readings] == [NOVA[1], MINI[1]]
    assert first.box_px == (537, 537, 612, 562), "the key's region, at the stage's 300 dpi"
    assert [reading.extractor for reading in regions["k2"][0].readings] == [MINI[0]]


def test_a_scorecard_from_another_key_or_an_unknown_reader_is_refused() -> None:
    crops = [_crop("k1", 2, crop_px=WIDE)]

    with pytest.raises(ReplayError, match="not made on this key"):
        regions_from_scorecard(
            [{"crop": "k9", "pair": []}], crops, frame=FRAME, stage_dpi=300, model_ids=MODEL_IDS
        )
    with pytest.raises(ReplayError, match="no model id"):
        regions_from_scorecard(
            [{"crop": "k1", "pair": [["bedrock-new", '2"', None]]}],
            crops,
            frame=FRAME,
            stage_dpi=300,
            model_ids=MODEL_IDS,
        )


def _key_dir(tmp_path: Path, frame: dict[str, object] | None) -> Path:
    metadata: dict[str, object] = {"tags": {}}
    if frame is not None:
        metadata["frame"] = frame
    (tmp_path / "model_bakeoff_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    return tmp_path


def test_a_key_that_records_no_frame_needs_its_dpi_and_its_margin_named(tmp_path: Path) -> None:
    key = _key_dir(tmp_path, None)

    with pytest.raises(ReplayError, match="does not record the pixel frame"):
        frame_of(key, polygon_dpi=None, margin_pt=Decimal(0))
    with pytest.raises(ReplayError, match="how much page"):
        frame_of(key, polygon_dpi=300, margin_pt=None)
    assert frame_of(key, polygon_dpi=300, margin_pt=Decimal(0)) == KeyFrame(
        polygon_dpi=300, margin_pt=Decimal(0)
    )


def test_a_key_that_records_its_frame_refuses_another(tmp_path: Path) -> None:
    key = _key_dir(tmp_path, {"polygon_dpi": 600, "margin_pt": "9"})

    assert frame_of(key, polygon_dpi=None, margin_pt=None) == FRAME
    with pytest.raises(ReplayError, match="records a 9 pt margin"):
        frame_of(key, polygon_dpi=None, margin_pt=Decimal(0))
    with pytest.raises(ReplayError, match="records its polygons at 600 dpi"):
        frame_of(key, polygon_dpi=300, margin_pt=None)


def test_the_stacked_finder_is_scored_against_the_keys_marks() -> None:
    crops = [
        _crop("a", 1, stacked=True),
        _crop("b", 1, stacked=True),
        _crop("c", 1),
        _crop("d", 1),
    ]

    caught = stacked_catch(crops, {"a": True, "b": False, "c": True, "d": False})

    assert (caught.marked, caught.caught, caught.flagged_unmarked) == (2, 1, 1)


def test_a_replay_with_a_crop_left_unjoined_or_a_region_with_no_geometry_is_refused() -> None:
    crop = _crop("c1", 12)
    region = _agreeing("r1", '12"', 12)

    with pytest.raises(ReplayError, match="no regions were joined"):
        Replay(source="s", crops=(crop,), regions={}, facts={})
    with pytest.raises(ReplayError, match="no geometry"):
        Replay(source="s", crops=(crop,), regions={"c1": (region,)}, facts={})


def test_the_key_marks_a_crop_stacked_by_its_tick_or_its_group(tmp_path: Path) -> None:
    folder = _write_key(
        tmp_path / "key",
        [
            _row("k0", value='3/4"', exact="3/4", stratum="stacked_fraction"),
            _row("k1", value='12"', exact="12"),
            _row("k2", value='1/4"', exact="1/4"),
        ],
    )
    with (folder / "crops.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows[2]["stacked"] = "x"
    with (folder / "crops.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*rows[0].keys(), "stacked"])
        writer.writeheader()
        writer.writerows(rows)

    assert [crop.stacked for crop in load_key(folder)] == [True, False, True]
