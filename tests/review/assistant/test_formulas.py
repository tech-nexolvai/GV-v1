"""GV's settled formulas as code checks (final plan #9): made-up drawings and values, no client data.

A made-up vanity: the reference (the cabinet maker's drawing) prints its cabinet run, 46" from the
wall to a finished end panel. GV's top (the vendor's drawing) prints its length from GV's 1" field
cut at the wall to the top's edge past the cabinets at the open end.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import pytest

from app.review.assistant import check_record, dim_ends, formulas, job_value_rules, proof
from app.review.assistant.chat_trust import judged
from app.review.assistant.escalation import EscalationContext, escalate

VENDOR = "vendor.pdf"
REFERENCE = "reference.pdf"


def _ends(
    value: float, low: set[str], high: set[str], *, key: str = "D1", axis: str = "h"
) -> dim_ends.Ends:
    span = dim_ends.Span(key, axis, 100.0, 0.0, value * 4, Fraction(value))
    return dim_ends.Ends(
        span, dim_ends.End(span.a, frozenset(low)), dim_ends.End(span.b, frozenset(high))
    )


# ---- the length formula ------------------------------------------------------------------------


def test_a_field_cut_at_the_wall_and_both_at_the_wall_add_one_field_cut() -> None:
    vendor = _ends(47, {"field_cut_end"}, {"wall"})
    ref = _ends(46, {"wall", "cabinet_face"}, {"wall", "end_panel"})
    found = formulas.top_length(vendor, ref, field_cut=Fraction(1))
    assert found is not None and found.outcome == "same" and found.expected == 47
    assert '1" field cut at the wall end' in found.words


def test_a_difference_is_the_formula_s() -> None:
    vendor = _ends(48, {"field_cut_end"}, {"wall"})
    ref = _ends(46, {"wall", "cabinet_face"}, {"wall", "end_panel"})
    found = formulas.top_length(vendor, ref, field_cut=Fraction(1))
    assert found is not None and found.outcome == "different" and found.expected == 47


def test_a_finished_end_waits_for_the_end_overhang() -> None:
    vendor = _ends(48, {"field_cut_end"}, {"overhang_end", "top_edge"})
    ref = _ends(46, {"wall", "cabinet_face"}, {"end_panel"})
    found = formulas.top_length(vendor, ref, field_cut=Fraction(1))
    assert found is not None and found.outcome == "missing"
    assert found.missing == ("end overhang over a finished end",)
    entered = formulas.top_length(vendor, ref, field_cut=Fraction(1), end_overhang=Fraction(1))
    assert entered is not None and entered.outcome == "same" and entered.expected == 48


def test_an_end_code_cannot_name_says_nothing() -> None:
    vendor = _ends(46.75, set(), {"top_outline"})
    ref = _ends(46, {"end_panel"}, {"outline"})
    assert formulas.top_length(vendor, ref, field_cut=Fraction(1)) is None


def test_a_top_s_outline_is_never_taken_for_a_free_end() -> None:
    vendor = _ends(47, {"field_cut_end"}, {"top_outline"})
    ref = _ends(46, {"wall", "cabinet_face"}, {"end_panel"})
    assert formulas.top_length(vendor, ref, field_cut=Fraction(1)) is None


def test_an_end_that_meets_no_formula_case_says_nothing() -> None:
    # The far end: a wall on the vendor's top against a cabinet's side with no wall on the
    # reference: code cannot place it.
    found = formulas.top_length(
        _ends(47, {"field_cut_end"}, {"wall"}),
        _ends(46, {"wall", "cabinet_face"}, {"cabinet_face"}),
        field_cut=Fraction(1),
    )
    assert found is None


def test_two_ways_round_that_disagree_say_nothing() -> None:
    # Read one way round: a field cut and the end overhang; the other way: both at the wall.
    vendor = _ends(47, {"field_cut_end", "wall"}, {"overhang_end", "wall"})
    ref = _ends(46, {"wall", "end_panel"}, {"wall", "cabinet_face"})
    assert formulas.top_length(vendor, ref, field_cut=Fraction(1)) is None


def test_a_gap_too_big_for_a_field_cut_is_no_formula() -> None:
    vendor = _ends(52, {"field_cut_end"}, {"wall"})
    ref = _ends(46, {"wall", "cabinet_face"}, {"wall", "end_panel"})
    assert formulas.top_length(vendor, ref, field_cut=Fraction(6)) is None


# ---- the job settings ----------------------------------------------------------------------------

RULES_MD = """
| Setting | In force | Where it comes from | Used by |
|---|---|---|---|
| Cabinet depth (`cabinet_depth`) | not set | missing | CT-DEPTH-001 |
| Countertop overhang (`countertop_overhang`) | not set | missing | CT-DEPTH-001 |
| Field cut per wall end (`field_cut`) | 1" | the rulebook's default | CT-WIDTH-001 |
| Filler width, maximum (`filler_max`) | 2" | the rulebook's default | CAB-FILLER-001 |
| Filler width, minimum (`filler_min`) | 1" | the rulebook's default | CAB-FILLER-001 |
"""


def test_settled_defaults_are_used_and_the_rest_stay_not_entered() -> None:
    state = {row["setting"]: row for row in formulas.settings_state(RULES_MD)}
    assert state["field cut per wall end"]["entered"] is True
    assert state["field cut per wall end"]["values"] == {"field_cut": '1"'}
    assert state["filler width range"]["entered"] is True
    for name in (
        "front overhang",
        "cabinet depth (carcass + door)",
        "end overhang over a finished end",
        "sink inside size (or the sink model's cut sheet)",
    ):
        assert state[name]["entered"] is False, name
        assert state[name]["default"] is None, name
    assert formulas.setting(RULES_MD, "countertop_overhang") is None
    assert formulas.setting(RULES_MD, formulas.END_OVERHANG) is None


# ---- each formula is its own kind of check ---------------------------------------------------------


def test_each_formula_has_its_own_kind_with_no_keyed_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(check_record.SWITCH_ENV, raising=False)
    for formula in formulas.FORMULAS:
        assert formula.kind in check_record.KINDS, formula.kind
        assert formula.kind in check_record.SHORT_NAMES, formula.kind
        if formula.kind != "sink_centre":
            assert check_record.record_of(formula.kind).keyed_cases == 0
            assert not check_record.cleared(formula.kind)


def test_kind_of_a_formula_item() -> None:
    found = {"formula": formulas.record(formulas.LENGTH, "different", "x")}
    assert check_record.kind_of(found) == "formula_length"
    depth = {"created_by": "sink_offset", "gv_standard": {"rule": "CT-DEPTH-001"}}
    assert check_record.kind_of(depth) == "formula_depth"
    offset = {"created_by": "sink_offset", "gv_standard": {"rule": "CT-SINK-OFFSET-FRONT-001"}}
    assert check_record.kind_of(offset) == "sink_offset"


# ---- in the escalation (the like-for-like step) ----------------------------------------------------


@dataclass
class _Check:
    agrees: bool = True


class _Fit:
    def __init__(self, agrees: bool = True) -> None:
        self._agrees = agrees

    def check(self, _label_id: object) -> _Check:
        return _Check(self._agrees)

    def disagrees(self, _label_id: object) -> _Check | None:
        return None if self._agrees else _Check(False)


def _page(file: str, ends: dim_ends.Ends, *, agrees: bool = True) -> dim_ends.Page:
    page = dim_ends.Page(file, 1, spans={ends.span.id: ends.span}, fit=_Fit(agrees))
    page.kinds[(ends.span.id, "a")] = set(ends.low.kinds)
    page.kinds[(ends.span.id, "b")] = set(ends.high.kinds)
    return page


def _finding(vendor: float, architect: float, status: str = "different") -> dict[str, Any]:
    return {
        "id": "F1",
        "page": 1,
        "vs_page": 1,
        "item": "Vanity: top length",
        "vendor": f'{vendor:g}"',
        "architect": f'{architect:g}"',
        "status": status,
        "source": "architect",
        "category": "countertop",
        "created_by": "helper:1",
        "inches": {"vendor": f"{vendor:g}", "architect": f"{architect:g}"},
        "checks": {"printed_vendor": "match", "printed_architect": "match"},
        "dims": {
            "vendor": {"id": "D1", "page": 1, "file": VENDOR, "inches": f"{vendor:g}"},
            "architect": {"id": "D1", "page": 1, "file": REFERENCE, "inches": f"{architect:g}"},
        },
    }


def _escalated(
    finding: dict[str, Any],
    vendor: dim_ends.Ends,
    ref: dim_ends.Ends,
    *,
    agrees: bool = True,
    rules_md: str | None = RULES_MD,
) -> dict[str, Any]:
    pages = {VENDOR: _page(VENDOR, vendor, agrees=agrees), REFERENCE: _page(REFERENCE, ref)}
    ctx = EscalationContext(
        main_file=VENDOR, pages=lambda file, _page_no: pages.get(file), rules_md=rules_md
    )
    result = escalate([finding], ctx)
    changed = {str(f["id"]): f for f in result.changed}
    return judged(changed.get("F1", finding))


def test_the_same_by_the_formula_is_right_with_the_formula_shown() -> None:
    vendor = _ends(47, {"field_cut_end"}, {"wall"})
    ref = _ends(46, {"wall", "cabinet_face"}, {"wall", "end_panel"})
    found = _escalated(_finding(47, 46), vendor, ref)
    assert proof.proof_group(found) == "right"
    assert found["formula"]["kind"] == "formula_length" and found["formula"]["outcome"] == "same"
    assert found["settled"]["how"].startswith("GV's formula (countertop length = ")


def test_a_difference_by_the_formula_is_a_likely_problem_never_red() -> None:
    vendor = _ends(48, {"field_cut_end"}, {"wall"})
    ref = _ends(46, {"wall", "cabinet_face"}, {"wall", "end_panel"})
    found = _escalated(_finding(48, 46), vendor, ref)
    assert proof.proof_group(found) == "likely_problem"
    assert check_record.kind_of(found) == "formula_length"
    words = proof.likely_words(found)
    assert words.startswith("Please check and confirm: by GV's formula") and "never red" in words
    # Even a run whose checks all passed (an old saved answer) is never red by a formula.
    passed = dict(_finding(48, 46), critic={"model": "devil's advocate", "result": "passed"})
    assert proof.proof_group(_escalated(passed, vendor, ref)) == "likely_problem"


def test_a_missing_job_value_is_not_compared_saying_which() -> None:
    vendor = _ends(48, {"field_cut_end"}, {"overhang_end", "top_edge"})
    ref = _ends(46, {"wall", "cabinet_face"}, {"end_panel"})
    found = _escalated(_finding(48, 46), vendor, ref)
    assert proof.proof_group(found) == "not_compared"
    classified = proof.classified(found)
    assert classified.reason == "missing_setting"
    assert "end overhang over a finished end" in str(classified.why)
    assert "not entered yet" in str(classified.why)
    assert found[job_value_rules.FIELD]["missing"] == ["end overhang over a finished end"]


def test_a_value_not_drawn_to_its_length_never_feeds_the_formula() -> None:
    vendor = _ends(48, {"field_cut_end"}, {"wall"})
    ref = _ends(46, {"wall", "cabinet_face"}, {"wall", "end_panel"})
    found = _escalated(_finding(48, 46), vendor, ref, agrees=False)
    assert "formula" not in found


def test_two_alike_dimensions_are_not_the_formula_s() -> None:
    vendor = _ends(46, {"wall"}, {"wall"})
    ref = _ends(46, {"wall"}, {"wall"})
    found = _escalated(_finding(46, 46, status="same"), vendor, ref)
    assert "formula" not in found
