"""Code's maths on the AI's own numbers (flag mode, 2026-11-04): string sums, exact vendor-against-
architect inches and GV's rules, on the numbers the AI wrote down, never on the drawing.
Made-up values and sentences; no client data.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.review.assistant import ai_maths, likely_problems
from app.review.assistant.escalation import EscalationContext, escalate


def _finding(**changes: Any) -> dict[str, Any]:
    """A helper's item: the vendor's value against the architect's."""
    finding: dict[str, Any] = {
        "id": "F1",
        "page": 3,
        "item": "Desk: top length",
        "category": "countertop",
        "vendor": '61 1/2"',
        "architect": '61 5/8"',
        "status": "different",
        "source": "architect",
        "created_by": "helper:4",
    }
    finding.update(changes)
    return finding


def _please_check(wrong: str, kind: str = "size", **numbers: Any) -> dict[str, Any]:
    """A page reviewer's please-check: words only on its sides, its numbers on its mark."""
    mark: dict[str, Any] = {"by": "page_reviewer", "kind": kind, "why": wrong}
    if numbers:
        mark[ai_maths.NUMBERS] = numbers
    return _finding(
        item="Front view: width string",
        vendor="see the drawing",
        architect="n/a",
        status="check",
        created_by="page_reviewer",
        why=wrong,
        **{likely_problems.FIELD: mark},
    )


def _maths(finding: dict[str, Any], rules_md: str | None = None) -> dict[str, Any] | None:
    return ai_maths.check(finding, rules_md)


# ---- strings -------------------------------------------------------------------------------------


def test_a_string_the_ai_listed_is_added_by_code_and_an_eighth_off_differs() -> None:
    found = _maths(
        _finding(
            source="vendor_arithmetic",
            parts=['3"', '47 1/8" (EQ)', '3"'],
            printed_total='53"',
            vendor='53"',
            architect='53 1/8"',
        )
    )
    assert found is not None and found["result"] == "differs"
    assert found["kind"] == "string" and found["read_from"] == "fields"
    assert '3" + 47 1/8" + 3" = 53 1/8"' in found["words"] and '1/8" apart' in found["words"]


def test_millimetres_first_count_by_their_inch_figures_and_millimetres_alone_are_unreadable() -> (
    None
):
    inches = _maths(
        _finding(
            source="vendor_arithmetic",
            parts=["25 [1]", "450 [17 3/4]", "25 [1]"],
            printed_total="500 [19 1/2]",
        )
    )
    assert inches is not None and inches["result"] == "differs"  # 19 3/4" against 19 1/2"
    assert '= 19 3/4"' in inches["words"]
    metric = _maths(
        _finding(source="vendor_arithmetic", parts=["25", "1500", "25"], printed_total="1550")
    )
    assert metric is not None and metric["result"] == "unreadable"
    assert "millimetres only" in metric["words"]


def test_a_please_check_string_that_adds_up_by_the_ais_own_numbers_is_no_difference() -> None:
    found = _maths(
        _please_check(
            "The parts do not add up to the overall.",
            parts=['10"', '12 1/2"', '13 1/2"'],
            printed_total='36"',
        )
    )
    assert found is not None and found["result"] == "same"
    assert "the string adds up" in found["words"]


def test_a_part_code_cannot_read_leaves_the_string_unreadable() -> None:
    found = _maths(
        _finding(source="vendor_arithmetic", parts=['10"', "EQ", '10"'], printed_total='30"')
    )
    assert found is not None and found["result"] == "unreadable" and '"EQ"' in found["words"]


# ---- the sentence fallback -----------------------------------------------------------------------


def test_the_ais_sentence_is_read_when_it_gave_no_fields() -> None:
    found = _maths(
        _please_check(
            'The string 3" + 47 1/8" (EQ) + 3" adds up to 53 1/8", but the overall above it is '
            '53", and the plan also gives 53".'
        )
    )
    assert found is not None and found["result"] == "differs"
    assert found["read_from"] == "sentence"


def test_a_sentence_whose_own_sum_slipped_is_no_difference() -> None:
    found = _maths(_please_check('The parts 10" + 12" + 14" = 37", but the overall prints 36".'))
    assert found is not None and found["result"] == "same"
    assert "the AI's own sum, 37\", was wrong" in found["words"]


def test_a_sentence_with_no_sum_of_its_own_is_never_no_difference() -> None:
    found = _maths(
        _please_check('The run (10 + 12 + 14) is not the 36" overall GV needs with its field cut.')
    )
    assert found is not None and found["result"] == "unreadable"


@pytest.mark.parametrize(
    "wrong",
    [
        'The top should be 40" + 1" field cut at each wall end = 42", not 41".',
        'By GV\'s rule it should be 40" + 2 x 1" = 42", but it prints 41".',
    ],
)
def test_a_multiplier_is_never_read_as_a_part(wrong: str) -> None:
    found = _maths(_please_check(wrong))
    assert found is not None and found["result"] == "unreadable"
    assert "multiplier" in found["words"]


def test_a_chain_in_millimetres_is_left_out() -> None:
    found = _maths(
        _please_check(
            'In inches 1 1/8 + 19 3/4 + 1 1/8 = 22", but the overall prints 22 1/8". The '
            "millimetres (30 + 500 + 30 = 560) add up."
        )
    )
    assert found is not None and found["result"] == "differs"
    assert "560" not in found["words"]


def test_a_construction_claim_quoting_a_sum_states_no_maths() -> None:
    finding = _please_check(
        'The 98" top is two pieces, 49" + 49" = 98"; no support is drawn under the joint.',
        kind="construction",
    )
    assert _maths(finding) is None


def test_a_sum_the_ai_says_adds_up_is_no_claim() -> None:
    finding = _please_check(
        'The elevation has no overall height; its parts 20" + 30" = 50" match the title block\'s '
        'overall of 50".'
    )
    assert _maths(finding) is None


# ---- two sizes -----------------------------------------------------------------------------------


def test_two_sizes_are_compared_exactly_in_inches() -> None:
    differs = _maths(_finding())
    assert differs is not None and differs["result"] == "differs" and differs["kind"] == "sizes"
    assert '1/8" apart' in differs["words"]
    same = _maths(_finding(vendor="1562 [61 1/2]", architect="5'-1 1/2\""))
    assert same is not None and same["result"] == "same"


def test_the_page_reviewers_two_values_are_compared() -> None:
    found = _maths(_please_check("Top differs from p7.", vendor_value='24"', other_value='25"'))
    assert found is not None and found["result"] == "differs"
    assert "the size it should match" in found["words"]


@pytest.mark.parametrize(
    ("vendor", "architect"), [('36" MIN', '36"'), ('36" x 24"', '36"'), ("940mm", '37"')]
)
def test_a_value_that_is_not_one_exact_size_is_unreadable(vendor: str, architect: str) -> None:
    found = _maths(_finding(vendor=vendor, architect=architect))
    assert found is not None and found["result"] == "unreadable"


def test_a_missing_side_is_a_missing_item_not_maths() -> None:
    assert _maths(_finding(vendor="not shown on the vendor")) is None
    assert _maths(_finding(architect="n/a")) is None


@pytest.mark.parametrize(
    ("item", "vendor", "architect"),
    [
        ("Key plan: revision number", "6", "6"),
        ("Title block: sheet number", "2.3", "2.1"),
        ("Base cabinet tag", "7", "8"),
    ],
)
def test_an_identifier_is_never_inches(item: str, vendor: str, architect: str) -> None:
    found = _maths(_finding(item=item, vendor=vendor, architect=architect))
    assert found is not None and found["result"] == "unreadable" and found["kind"] == "identifier"


def test_tags_in_words_state_no_maths() -> None:
    assert _maths(_finding(item="Vanity: top tag", vendor="ST-01", architect="ST-02")) is None


def test_a_gv_rule_item_is_left_to_its_own_rule() -> None:
    """A rule item's two sides are code's reading of the rule (from the top's edge, say), not two
    sizes that must match."""
    assert _maths(_finding(source="rules", vendor="102 [4]", architect='4"')) is None


# ---- GV's rules ----------------------------------------------------------------------------------

_RULES_MD = """\
| Setting | Value | Where it comes from |
|---|---|---|
| Field cut per wall end (`field_cut`) | 1 1/2" | This project's value |
| Sink front offset (`front_offset_required`) | 3" | Typed for this drawing set |
| Sink cut-out clearance (`sink_cutout_clearance`) | 1/8" | This project's value |
"""


@pytest.mark.parametrize(
    ("printed", "rules_md", "result"),
    [('50"', None, "same"), ('52"', None, "differs"), ('51"', _RULES_MD, "same")],
)
def test_the_field_cut_rule_is_applied_by_code(
    printed: str, rules_md: str | None, result: str
) -> None:
    rule = {"rule": "field_cut", "printed": printed, "run": '48"', "wall_ends": 2}
    found = _maths(_please_check("Field cut.", gv_rule=rule), rules_md)
    assert found is not None and found["result"] == result and found["kind"] == "rule"


def test_the_field_cut_rule_needs_the_run_and_the_wall_ends() -> None:
    found = _maths(_please_check("Field cut.", gv_rule={"rule": "field_cut", "printed": '50"'}))
    assert found is not None and found["result"] == "unreadable"


@pytest.mark.parametrize(
    ("rule", "rules_md", "result"),
    [
        ({"printed": '3 1/2"'}, None, "differs"),
        ({"printed": '4"'}, None, "same"),
        ({"printed": '4"', "overhang": '1/2"'}, None, "differs"),
        ({"printed": '4 1/2"', "overhang": '1/2"'}, None, "same"),
        ({"printed": '3"'}, _RULES_MD, "same"),
    ],
)
def test_the_sink_front_offset_is_gvs_value_from_the_cabinets_front(
    rule: dict[str, Any], rules_md: str | None, result: str
) -> None:
    found = _maths(
        _please_check("Front offset.", gv_rule={"rule": "sink_front_offset", **rule}), rules_md
    )
    assert found is not None and found["result"] == result


@pytest.mark.parametrize(
    ("printed", "rules_md", "result"),
    [('16"', None, "same"), ('16 1/4"', None, "differs"), ('16 1/4"', _RULES_MD, "same")],
)
def test_the_sink_cut_out_is_the_inside_size_less_the_clearance_each_side(
    printed: str, rules_md: str | None, result: str
) -> None:
    rule = {"rule": "sink_cutout", "printed": printed, "sink_inside": '16 1/2"'}
    found = _maths(_please_check("Cut-out.", gv_rule=rule), rules_md)
    assert found is not None and found["result"] == result


# ---- the plain fields and the escalation ------------------------------------------------------------


def test_only_plain_number_fields_are_kept() -> None:
    given = {
        "kind": "A",
        "parts": ['10"', 12, True],
        "printed_total": '22"',
        "vendor_value": "",
        "gv_rule": {"rule": "field_cut", "printed": '50"', "wall_ends": 3, "extra": "x"},
    }
    kept = ai_maths.numbers_of(given)
    assert kept == {
        "parts": ['10"', "12"],
        "printed_total": '22"',
        "gv_rule": {"rule": "field_cut", "printed": '50"'},
    }
    assert ai_maths.numbers_of({"kind": "A", "gv_rule": {"rule": "made_up"}}) is None


def test_mark_answer_writes_each_changed_record_once_and_leaves_folded_items() -> None:
    live = [_finding(), _finding(id="F2", vendor='61 5/8"'), _finding(id="F3")]
    result = SimpleNamespace(changed=[], absorbed={"F3": "folded"})
    counts = ai_maths.mark_answer(live, result)
    assert counts["differs"] == 1 and counts["same"] == 1 and counts["no_difference"] == 1
    assert [f["id"] for f in result.changed] == ["F1", "F2"]
    assert ai_maths.FIELD not in live[2]
    again = SimpleNamespace(changed=[], absorbed={})
    ai_maths.mark_answer(live[:2], again)
    assert again.changed == []  # nothing new
    live[0]["vendor"] = "not shown on the vendor"
    ai_maths.mark_answer(live[:1], again)
    assert ai_maths.FIELD not in live[0] and again.changed == [live[0]]


def test_the_escalation_marks_the_maths_on_every_item() -> None:
    result = escalate([_finding(vs_page=3)], EscalationContext(main_file="set.pdf"))
    (found,) = [f for f in result.changed if f["id"] == "F1"]
    assert found[ai_maths.FIELD]["result"] == "differs"
