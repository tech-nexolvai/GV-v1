"""Reading a stored answer as two facts, and what the screen then shows (`verdict_adapter`).

Every answer here is made up. The shapes come from the hand-audited runs of 2026-11-04, but the
sizes, tags and wording are invented and no client name, job name or drawing number appears.

Each case is one of the patterns the audits found misfiled, plus the guards that must not move.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from app.review.assistant import classification, dim_ends, verdict_adapter
from app.review.assistant.verdict_facts import Missing, Present


@pytest.fixture(autouse=True)
def _flag_mode(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Flag mode, which is how the table runs (Anant, 2026-11-04)."""
    monkeypatch.setenv(classification.FLAG_MODE_ENV, "flag")
    yield


def answer(**fields: Any) -> dict[str, Any]:
    """One stored answer, with the fields every reading carries."""
    found: dict[str, Any] = {
        "id": "F1",
        "item": "worktop: depth",
        "page": 7,
        "vs_page": 20,
        "category": "countertop",
        "created_by": "helper:1",
        "source": "architect",
        "status": "different",
        "round_status": "single_round",
        "checks": {"printed_vendor": "match", "printed_architect": "match"},
        "witnesses": ["printed"],
    }
    found.update(fields)
    return found


def sizes(
    vendor: str, reference: str, *, vendor_inches: str, reference_inches: str
) -> dict[str, Any]:
    """The two sides as the reading records them: the text, the inches and a spot for each."""
    gap = str(float(vendor_inches) - float(reference_inches))
    return {
        "vendor": vendor,
        "architect": reference,
        "inches": {"vendor": vendor_inches, "architect": reference_inches, "difference": gap},
        "vendor_box": {"page": 7, "x0": 10.0, "y0": 10.0, "x1": 30.0, "y1": 20.0},
        "arch_box": {"page": 20, "x0": 50.0, "y0": 50.0, "x1": 70.0, "y1": 60.0},
        "dims": {
            "vendor": {
                "id": "D1",
                "page": 7,
                "text": vendor,
                "inches": vendor_inches,
                "route": "text",
                "side": "vendor",
            },
            "architect": {
                "id": "D2",
                "page": 20,
                "text": reference,
                "inches": reference_inches,
                "route": "text",
                "side": "architect",
            },
        },
    }


# ---- the leak: a difference filed as Not compared ------------------------------------------------


def test_a_missing_job_setting_no_longer_hides_a_printed_difference() -> None:
    """The audits' biggest class: the architect prints the size, a GV job value is not entered,
    and the answer was parked as "a job value or GV setting is missing" instead of flagged."""
    found = answer(
        **sizes('22"', "1'-10 1/2\"", vendor_inches="22", reference_inches="22.5"),
        card={"question": {"key": "missing_setting:cabinet depth|front overhang"}},
    )
    pair = verdict_adapter.facts_of(found)
    assert isinstance(pair.vendor, Present) and isinstance(pair.reference, Present)
    assert pair.comparable.ok
    verdict = verdict_adapter.verdict_of(found)
    assert verdict.differs and verdict.row == "V6"
    assert verdict_adapter.group_of(found) == "likely_problem"


def test_an_answer_not_looked_at_on_both_drawings_still_agrees() -> None:
    """Both values are there and equal; "not looked at on both drawings" is a note about the
    process, not a reason either side has no value."""
    found = answer(
        status="same",
        compared=False,
        **sizes("762 [30]", '30"', vendor_inches="30", reference_inches="30"),
    )
    verdict = verdict_adapter.verdict_of(found)
    assert verdict.same and verdict.row == "V5"
    assert verdict_adapter.group_of(found) == "right"


def test_a_tag_with_the_maker_s_suffix_agrees() -> None:
    found = answer(
        status="same",
        category="cabinet",
        item="base run: dishwasher tag",
        vendor="AP-001-XY",
        architect="AP-001",
        arch_box={"page": 20, "x0": 1.0, "y0": 1.0, "x1": 9.0, "y1": 9.0},
        vendor_box={"page": 7, "x0": 1.0, "y0": 1.0, "x1": 9.0, "y1": 9.0},
    )
    assert verdict_adapter.verdict_of(found).same
    assert verdict_adapter.group_of(found) == "right"


# ---- what must still be Not compared --------------------------------------------------------------


def test_a_reference_that_prints_no_value_is_missing() -> None:
    """The architect's drawing is described but the value is not on it ("no model printed"): the
    reference has nothing, so there is nothing to compare and no please-check to raise."""
    found = answer(
        status="check",
        item="vanity: sink tag",
        vendor="UC-20-2",
        architect="undermount sink drawn, no model printed",
        vendor_box={"page": 7, "x0": 1.0, "y0": 1.0, "x1": 9.0, "y1": 9.0},
    )
    pair = verdict_adapter.facts_of(found)
    assert isinstance(pair.reference, Missing)
    verdict = verdict_adapter.verdict_of(found)
    assert not verdict.compared and verdict.row == "V3"
    assert verdict_adapter.group_of(found) != "right"


def test_a_reading_that_points_at_the_sheet_records_no_value() -> None:
    found = answer(status="check", vendor="see the drawing", architect="n/a")
    pair = verdict_adapter.facts_of(found)
    assert isinstance(pair.vendor, Missing) and isinstance(pair.reference, Missing)
    assert verdict_adapter.verdict_of(found).row == "V1"


def test_gv_s_own_rule_that_did_not_run_gives_no_reference_value() -> None:
    """The vendor's 4" is from the stone's edge and GV's standard 4" is from the cabinet front:
    the quoted standard is not a value, so this is never a green tick, and because its reason is
    not one that says nothing was compared it is still raised as a please-check."""
    found = answer(
        status="check",
        source="rules",
        created_by="sink_offset",
        item="worktop: sink cut-out, front offset",
        checks={"printed_vendor": "match", "printed_architect": "no_box", "rule": "not_run"},
        **sizes("102 [4]", '4"', vendor_inches="4", reference_inches="4"),
    )
    pair = verdict_adapter.facts_of(found)
    assert isinstance(pair.reference, Missing)
    assert not verdict_adapter.verdict_of(found).compared
    assert verdict_adapter.group_of(found) == "likely_problem"


# ---- what code proved -----------------------------------------------------------------------------


def test_a_millimetre_label_is_never_read_as_inches() -> None:
    """A label's own number on a vendor's sheet is often the millimetres. Only the comparison's
    own inch figures are the maths, or "[3344]" against 34" becomes a difference of 3310\"."""
    found = answer(
        status="same",
        vendor="[3344]",
        architect='34"',
        inches={"vendor": None, "architect": "34", "difference": None},
        vendor_box={"page": 7, "x0": 1.0, "y0": 1.0, "x1": 9.0, "y1": 9.0},
        arch_box={"page": 20, "x0": 1.0, "y0": 1.0, "x1": 9.0, "y1": 9.0},
        dims={
            "vendor": {
                "id": "D1",
                "page": 7,
                "text": "[3344]",
                "inches": "3344",
                "route": "text",
                "side": "vendor",
            },
            "architect": {
                "id": "D2",
                "page": 20,
                "text": '34"',
                "inches": "34",
                "route": "text",
                "side": "architect",
            },
        },
    )
    pair = verdict_adapter.facts_of(found)
    assert isinstance(pair.vendor, Present) and pair.vendor.inches is None
    assert verdict_adapter.verdict_of(found).same


def test_two_dimensions_code_proved_unlike_are_never_a_problem() -> None:
    """`dim_ends` proved the two run between different points: the pair carries the reason, and a
    pair code proved unlike is kept off the problems list however the numbers read."""
    found = answer(
        **sizes('34"', "2'-10\"", vendor_inches="34", reference_inches="34"),
        **{
            dim_ends.FIELD: {
                "outcome": "unlike",
                "vendor": "|7|D1",
                "architect": "|20|D2",
                "why": "the vendor's runs to the underside, the reference's to the top",
            }
        },
    )
    pair = verdict_adapter.facts_of(found)
    assert not pair.comparable.ok and pair.comparable.proved
    verdict = verdict_adapter.verdict_of(found)
    assert verdict.row == "V4"
    assert verdict.absent is not None and verdict.absent.side == "pair"
    assert verdict_adapter.group_of(found) == "not_compared"


def test_an_item_for_gv_s_reviewer_only_stays_off_every_list() -> None:
    found = answer(salvaged=True, **sizes('22"', '24"', vendor_inches="22", reference_inches="24"))
    assert verdict_adapter.group_of(found) == "not_compared"


def test_the_reviewer_s_own_decision_still_decides() -> None:
    found = answer(
        **sizes('22"', '24"', vendor_inches="22", reference_inches="24"),
        decision={"decision": "not_a_problem"},
    )
    assert verdict_adapter.group_of(found) == "dismissed"


def test_off_flag_mode_the_proof_rules_decide(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(classification.FLAG_MODE_ENV, "proof")
    found = answer(status="same", **sizes('30"', '30"', vendor_inches="30", reference_inches="30"))
    assert verdict_adapter.group_of(found) == "right"
