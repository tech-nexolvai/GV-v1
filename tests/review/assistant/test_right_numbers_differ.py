"""A Right needs its two numbers to be the same (2026-10-27).

A by-eye check of a real upload's green ticks found two Rights whose numbers differ: the AI paired
a top depth with a cabinet width (and a tap-hole offset with a cabinet width) and called
them the same, and code never checked that a "same" item's two numbers agree. Such an item is now
Not compared, with the two values in its reason. The Rights whose raw numbers differ for a reason
code proved (GV's formulas, a field cut code allowed, a millimetre-only reference) stay Right, and
tags and words are untouched. Made-up values only.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from app.review.assistant import classification, proof
from app.review.assistant.chat_trust import judged
from app.review.assistant.escalation import EscalationContext, escalate
from app.review.assistant.field_cut import PageFieldCuts


def _same(vendor: str, architect: str, v_in: str | None, a_in: str | None, **extra: Any) -> dict:
    finding: dict[str, Any] = {
        "id": "F1",
        "item": "Vanity A: top depth",
        "status": "same",
        "source": "architect",
        "vendor": vendor,
        "architect": architect,
        "compared": True,
        "checks": {"units": "agrees", "printed_vendor": "match", "printed_architect": "match"},
        "witnesses": ["printed", "units"],
        "trust_level": "likely",
        "inches": {"vendor": v_in, "architect": a_in, "difference": None},
    }
    finding.update(extra)
    return finding


def test_a_same_item_whose_numbers_differ_is_not_compared() -> None:
    finding = _same('23"', "279 [11]", "23", "11")
    found = proof.classified(finding)
    assert found.outcome == "not_compared" and found.reason == "not_proved"
    assert found.why == 'the two values differ (23" vs 11"); not the same thing'
    assert proof.proof_group(finding) == "not_compared"
    fraction = _same('3 1/4" [83mm]', "432 [17]", "3.25", "17")
    assert proof.classified(fraction).why == (
        'the two values differ (3 1/4" vs 17"); not the same thing'
    )
    assert proof.proof_group(fraction) == "not_compared"


def test_equal_numbers_stay_right() -> None:
    for v_in, a_in in (("33", "33"), ("41.99", "42"), ("52.5", "52.5")):
        finding = _same('33"', "838 [33]", v_in, a_in)
        assert classification.differing_values(finding) is None
        assert proof.proof_group(finding) == "right"


def test_a_millimetre_only_reference_is_the_same_size() -> None:
    finding = _same('33"', "838", "33", "838")
    assert classification.differing_values(finding) is None
    assert proof.proof_group(finding) == "right"


def test_tags_and_words_are_untouched() -> None:
    finding = _same("SK-0001", "SK-0001", None, None, item="Vanity A: sink model")
    assert classification.differing_values(finding) is None
    assert proof.proof_group(finding) == "right"


def test_a_formula_right_compares_the_formula_value() -> None:
    """GV's chain formula: the reference prints parts (2" + 31"), the vendor the 33" top."""
    formula = {"id": "chain", "kind": "formula_chain", "outcome": "same", "expected": '33"'}
    chain = _same('33"', '2" + 31" = 33"', "33", "31", formula=formula)
    assert classification.differing_values(chain) is None
    assert proof.proof_group(chain) == "right"
    # A formula record without a number (the splash's wall-to-wall run) is code's comparison.
    splash = _same('62"', '61"', "62", "61", formula={"id": "splash", "outcome": "same"})
    assert classification.differing_values(splash) is None
    # A formula's own value that differs from the vendor's is never a Right.
    off = _same('33"', '31"', "33", "31", formula={**formula, "expected": '31"'})
    assert classification.differing_values(off) == ('33"', '31"')


def test_a_field_cut_code_allowed_is_taken_off_the_vendor_value() -> None:
    cut = {"outcome": "allowed", "per_end": '1"', "field_cuts": 1}
    finding = _same('43"', "1067 [42]", "43", "42", field_cut=cut)
    assert classification.differing_values(finding) is None
    assert proof.proof_group(finding) == "right"
    # Without code's field cut, the AI's own "43 = 42 + 1" is not a Right.
    assert proof.proof_group(_same('43"', "1067 [42]", "43", "42")) == "not_compared"
    more = _same('44"', "1067 [42]", "44", "42", field_cut=cut)
    assert classification.differing_values(more) == ('43"', '42"')


def test_gv_limits_and_the_vendors_own_sum_are_untouched() -> None:
    limits = _same('3 3/8"', '2 3/4"', "3.375", "2.75", settled={"method": "gv_limits"})
    assert classification.differing_values(limits) is None
    arithmetic = _same('33"', '31"', "33", "31", source="vendor_arithmetic")
    assert classification.differing_values(arithmetic) is None


def test_a_sixteenth_is_the_same_size() -> None:
    assert classification.differing_values(_same("", "", "33.0625", "33")) is None
    assert classification.differing_values(_same("", "", "33.125", "33")) == ('33 1/8"', '33"')


def _called_same_with_a_field_cut() -> dict:
    """The AI's own "43 = 42 + 1" on a top whose vendor overall includes the field cut."""
    vendor = {"id": "D3", "page": 4, "file": "vendor.pdf", "inches": "43", "text": '43"'}
    architect = {
        "id": "D9",
        "page": 2,
        "file": "reference.pdf",
        "inches": "42",
        "text": "1067 [42]",
    }
    return _same(
        '43"',
        "1067 [42]",
        "43",
        "42",
        id="F1",
        dims={"vendor": vendor, "architect": architect},
    )


def _escalated(finding: dict, overalls: dict[str, int]) -> dict:
    ctx = EscalationContext(
        main_file="vendor.pdf",
        field_cuts=lambda _file, _page: PageFieldCuts(Fraction(1), frozenset(), overalls),
    )
    result = escalate([finding], ctx)
    return judged({str(f["id"]): f for f in result.changed}.get("F1", finding))


def test_the_escalation_vets_a_field_cut_on_an_item_called_the_same() -> None:
    """Code's field-cut rule (settled with GV) proves the AI's arithmetic: Right with code's
    record. With no field cut on the vendor's overall it stays Not compared."""
    found = _escalated(_called_same_with_a_field_cut(), {"D3": 1})
    assert found["field_cut"]["outcome"] == "allowed"
    assert proof.proof_group(found) == "right"
    kept = _escalated(_called_same_with_a_field_cut(), {"D7": 1})
    assert "field_cut" not in kept
    assert proof.proof_group(kept) == "not_compared"
