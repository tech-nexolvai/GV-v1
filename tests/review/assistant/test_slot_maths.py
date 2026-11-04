"""Code fills a slot answer and decides it: exact sums, contiguity, decision B, GV's published rule.

Mirrors `app/review/assistant/slot_maths.py` and `slot_rules.py`, and checks how `verdict_adapter`
groups the findings a slot answer makes. Made-up labels, values and settings only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.review.assistant import proof, slot_maths, slots, verdict_adapter
from app.review.assistant.slot_answers import Answer, MarkRef
from app.review.assistant.verdict_facts import Missing, Present

WHEN = datetime(2026, 1, 2, tzinfo=UTC)
V = "vendor.pdf"
A = "architect.pdf"


def label(
    lid: str,
    text: str,
    inches: str | None,
    *,
    start: float = 0.0,
    end: float = 100.0,
    across: float = 50.0,
    orientation: str = "h",
    route: str = "text",
    **extra: Any,
) -> dict[str, Any]:
    if orientation == "h":
        line = [[start, across], [end, across]]
    else:
        line = [[across, start], [across, end]]
    return {
        "id": lid,
        "text": text,
        "inches": inches,
        "box": [start, across - 8, start + 20, across - 2],
        "orientation": orientation,
        "route": route,
        "dim_line": line,
        **extra,
    }


def ref(mark: str, found: Mapping[str, Any], side: str = "vendor") -> MarkRef:
    return MarkRef(mark, side, V if side == "vendor" else A, 2 if side == "vendor" else 5, str(found["id"]), found)  # type: ignore[arg-type]


def slot(key: str) -> slots.Slot:
    found = slots.slot(key)
    assert found is not None
    return found


def answer(
    measures: str,
    shop: Sequence[MarkRef],
    arch: Sequence[MarkRef] = (),
    against: Sequence[MarkRef] = (),
    ends: Mapping[str, str] | None = None,
) -> Answer:
    return Answer(measures, tuple(shop), tuple(against), tuple(arch), dict(ends or {}))


def decide(key: str, given: Answer, rules_md: str | None = None) -> slot_maths.SlotOutcome:
    return slot_maths.decide(slot(key), given, rules_md=rules_md, when=WHEN)


# ---- the architect, exactly ------------------------------------------------------------------------


def test_architect_compare_is_exact() -> None:
    v = ref("M3", label("D1", '1 1/4"', "1.25"))
    same = decide(
        "ct_thickness",
        answer("thickness", [v], [ref("M7", label("D9", '1 1/4"', "1.25"), "architect")]),
    )
    assert same.verdict.same and same.verdict.row == "V5" and not same.red_barred
    off = decide(
        "ct_thickness",
        answer("thickness", [v], [ref("M7", label("D9", '1 3/16"', "1.1875"), "architect")]),
    )
    assert off.differs and off.verdict.row == "V6"
    assert off.maths is not None and "1/16" in off.maths["words"]


def test_a_list_is_summed_exactly_and_proved_contiguous() -> None:
    parts = [
        ref("M1", label("D1", '30"', "30", start=0, end=200)),
        ref("M2", label("D2", '1 1/2"', "1.5", start=200, end=210)),
        ref("M3", label("D3", '24 1/16"', "24.0625", start=210.5, end=370)),
    ]
    arch = ref("M9", label("D4", '55 9/16"', "55.5625"), "architect")
    found = decide("run_wall_to_wall", answer("overall_length", parts, [arch]))
    assert found.verdict.same
    assert isinstance(found.vendor, Present) and found.vendor.inches == Decimal("55.5625")
    assert not found.red_barred


def test_a_list_not_proved_contiguous_is_never_red(monkeypatch: pytest.MonkeyPatch) -> None:
    parts = [
        ref("M1", label("D1", '30"', "30", start=0, end=200)),
        ref("M2", label("D2", '24"', "24", start=230, end=390)),
    ]
    arch = ref("M9", label("D4", '55"', "55"), "architect")
    found = decide("run_wall_to_wall", answer("overall_length", parts, [arch]))
    assert found.differs and slot_maths.NOT_CONTIGUOUS in found.red_barred
    monkeypatch.setenv("GV_ASSISTANT_FLAG_MODE", "flag")
    assert verdict_adapter.group_of(_finding(found)) == "likely_problem"


def test_unread_label_is_never_red(monkeypatch: pytest.MonkeyPatch) -> None:
    drawn = ref("M1", label("D1", "36", "36", route="drawn", scale_ok=None))
    arch = ref("M9", label("D4", '35"', "35"), "architect")
    found = decide("ct_counter_height", answer("height_above_floor", [drawn], [arch]))
    assert found.differs and slot_maths.NOT_CODE_READ in found.red_barred
    monkeypatch.setenv("GV_ASSISTANT_FLAG_MODE", "flag")
    finding = _finding(found)
    assert proof.slot_barred(finding)
    assert verdict_adapter.group_of(finding) == "likely_problem"


@pytest.mark.parametrize(
    ("extra", "why"),
    [
        ({"origin": "picture"}, "pasted picture"),
        ({"derived": {"from": "D7"}}, "equal part"),
        ({"units": "disagrees", "text": '36" [800]'}, "disagree"),
    ],
)
def test_unqualified_labels(extra: dict[str, Any], why: str) -> None:
    found = slot_maths.unqualified({**label("D1", '36"', "36"), **extra})
    assert found is not None and why in found


def test_qualified_labels() -> None:
    assert slot_maths.unqualified(label("D1", '36"', "36")) is None
    assert slot_maths.unqualified(label("D1", "914 [36]", "36", units="agrees")) is None
    assert slot_maths.unqualified(label("D1", "36", "36", route="drawn", scale_ok=True)) is None
    assert "bare number" in str(slot_maths.unqualified(label("D1", "914", "914")))


def test_a_width_is_never_held_against_a_height() -> None:
    v = ref("M1", label("D1", '24"', "24", orientation="h"))
    a = ref("M2", label("D2", '25"', "25", orientation="v"), "architect")
    found = decide("cabinet_depth", answer("overall_depth", [v], [a]))
    assert found.verdict.row == "V4" and found.comparable.proved


def test_no_architect_mark_is_not_compared() -> None:
    found = decide("ct_thickness", answer("thickness", [ref("M1", label("D1", '1"', "1"))]))
    assert found.verdict.row == "V3" and isinstance(found.reference, Missing)


# ---- the vendor's own string -----------------------------------------------------------------------


def _string(overall_end: float) -> slot_maths.SlotOutcome:
    parts = [
        ref("M1", label("D1", '20"', "20", start=0, end=100)),
        ref("M2", label("D2", '10 1/2"', "10.5", start=100, end=150)),
    ]
    overall = ref("M3", label("D3", '30 1/2"', "30.5", start=0, end=overall_end, across=80))
    return decide("string_adds_up", answer("overall_length", parts, against=[overall]))


def test_a_string_that_adds_up() -> None:
    found = _string(150)
    assert found.verdict.same and not found.red_barred
    assert found.maths is not None and found.maths["kind"] == "string"


def test_an_overall_elsewhere_bars_the_red() -> None:
    found = _string(180)
    assert slot_maths.OVERALL_ELSEWHERE in found.red_barred


# ---- GV's rule, by the isolated verdict engine ---------------------------------------------------


def _top(overall: str, inches: str, ends: Mapping[str, str]) -> slot_maths.SlotOutcome:
    top = ref("M1", label("D1", overall, inches, start=0, end=310, across=20))
    pieces = [
        ref("M2", label("D2", '30"', "30", start=5, end=155)),
        ref("M3", label("D3", '30"', "30", start=155, end=305)),
    ]
    return decide("ct_overall_length", answer("overall_length", [top], against=pieces, ends=ends))


def test_the_countertop_rule_runs_in_the_engine() -> None:
    walls = {"left": "wall", "right": "wall"}
    same = _top('62"', "62", walls)
    assert same.verdict.same and same.rule is not None and same.rule["id"] == "CT-WIDTH-001"
    assert same.rule["outcome"] == "PASS"
    off = _top('61"', "61", walls)
    assert off.differs and off.rule is not None and off.rule["outcome"] == "FAIL"
    one = _top('61"', "61", {"left": "wall", "right": "free"})
    assert one.verdict.same


def test_an_end_not_named_leaves_the_rule_undecided() -> None:
    found = _top('62"', "62", {"left": "not_shown", "right": "wall"})
    assert found.verdict.row == "V4" and not found.comparable.proved


def test_a_project_field_cut_replaces_the_standard() -> None:
    rules_md = "| Field cut per wall end (`field_cut`) | 1/2\" | this project's value |"
    top = ref("M1", label("D1", '61"', "61"))
    pieces = [
        ref("M2", label("D2", '30"', "30", start=0, end=150)),
        ref("M3", label("D3", '30"', "30", start=150, end=300)),
    ]
    found = slot_maths.decide(
        slot("ct_overall_length"),
        answer("overall_length", [top], against=pieces, ends={"left": "wall", "right": "wall"}),
        rules_md=rules_md,
        when=WHEN,
    )
    assert found.verdict.same


def test_sink_rules_in_the_engine() -> None:
    offset = decide(
        "sink_front_offset", answer("offset_from_front_edge", [ref("M1", label("D1", '4"', "4"))])
    )
    assert offset.verdict.same and offset.rule is not None
    assert offset.rule["id"] == "CT-SINK-OFFSET-FRONT-001"
    wrong = decide(
        "sink_front_offset",
        answer("offset_from_front_edge", [ref("M1", label("D1", '4 1/2"', "4.5"))]),
    )
    assert wrong.differs
    cut = answer("cutout_width", [ref("M1", label("D1", '17 1/2"', "17.5"))])
    no_sink = decide("sink_cutout_size", cut)
    assert isinstance(no_sink.reference, Missing) and no_sink.reference.reason == "missing_setting"
    rules_md = '| Sink inside width (`sink_interior_width`) | 18" | typed for this drawing set |'
    assert slot_maths.decide(
        slot("sink_cutout_size"), cut, rules_md=rules_md, when=WHEN
    ).verdict.same


def test_an_unqualified_value_never_enters_a_rule() -> None:
    drawn = ref("M1", label("D1", "4", "4", route="drawn"))
    found = decide("sink_front_offset", answer("offset_from_front_edge", [drawn]))
    assert found.rule is not None and found.rule["outcome"] == "REVIEW_REQUIRED"
    assert not found.verdict.compared


def test_a_job_value_that_is_not_set_is_missing() -> None:
    given = answer("overhang", [ref("M1", label("D1", '1"', "1"))])
    found = decide("ct_front_overhang", given)
    assert isinstance(found.reference, Missing) and found.reference.reason == "missing_setting"
    rules_md = "| Front overhang (`countertop_overhang`) | 1 1/4\" | this project's value |"
    set_one = slot_maths.decide(slot("ct_front_overhang"), given, rules_md=rules_md, when=WHEN)
    assert set_one.differs


# ---- the finding and its group ------------------------------------------------------------------


def _finding(outcome: slot_maths.SlotOutcome) -> dict[str, Any]:
    raw, extra = slot_maths.finding_fields(outcome)
    filled = {k: v for k, v in raw.items() if k not in ("vendor_dim", "architect_dim")}
    vendor = outcome.vendor.value if isinstance(outcome.vendor, Present) else "not read"
    reference = outcome.reference.value if isinstance(outcome.reference, Present) else ""
    filled.setdefault("vendor", vendor)
    filled.setdefault("architect", reference)
    return {"id": "F1", **filled, **extra}


def test_the_stored_record_gives_the_same_facts() -> None:
    v = ref("M3", label("D1", '1 1/4"', "1.25"))
    found = decide(
        "ct_thickness", answer("thickness", [v], [ref("M7", label("D9", '1"', "1"), "architect")])
    )
    pair = verdict_adapter.facts_of(_finding(found))
    assert pair.vendor == found.vendor and pair.reference == found.reference
    assert verdict_adapter.verdict_of(_finding(found)).row == "V6"


def test_groups_in_flag_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GV_ASSISTANT_FLAG_MODE", "flag")
    v = ref("M3", label("D1", '1 1/4"', "1.25"))
    differs = decide(
        "ct_thickness", answer("thickness", [v], [ref("M7", label("D9", '1"', "1"), "architect")])
    )
    assert verdict_adapter.group_of(_finding(differs)) == "clouded"
    same = decide(
        "ct_thickness",
        answer("thickness", [v], [ref("M7", label("D9", '1 1/4"', "1.25"), "architect")]),
    )
    assert verdict_adapter.group_of(_finding(same)) == "right"
    rule = _top('61"', "61", {"left": "wall", "right": "wall"})
    assert (
        verdict_adapter.group_of(_finding(rule)) == "likely_problem"
    )  # GV's rule: never red alone
    unlike = decide(
        "cabinet_depth",
        answer(
            "overall_depth",
            [ref("M1", label("D1", '24"', "24"))],
            [ref("M2", label("D2", '25"', "25", orientation="v"), "architect")],
        ),
    )
    assert verdict_adapter.group_of(_finding(unlike)) == "not_compared"
    missing = decide("ct_front_overhang", answer("overhang", [ref("M1", label("D1", '1"', "1"))]))
    assert verdict_adapter.group_of(_finding(missing)) == "not_compared"


def test_a_finding_without_a_slot_record_is_untouched() -> None:
    plain = {
        "id": "F1",
        "vendor": '36"',
        "architect": '35"',
        "status": "different",
        "source": "architect",
    }
    assert not proof.slot_barred(plain)
    assert verdict_adapter._slot_pair(plain) is None
