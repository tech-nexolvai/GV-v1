"""Each kind of check makes reds only with a clean track record (`check_record`, 2026-10-24).

A Wrong code proved stays red only when its kind of check is clean (no false red) on at least the
minimum number of keyed cases; otherwise it is a Likely problem that says the check is still being
proven. Made-up values only; the record file in these tests is a temporary one."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import (
    architect_items,
    check_record,
    filler_range,
    missing_items,
    run_fit,
    same_item_pages,
    sink_offset,
    splash_views,
)
from app.review.assistant.proof import held_back, likely_words, proof_group, proof_of
from app.review.assistant.send_package import checks_of


def _proved(**more: Any) -> dict[str, Any]:
    """A size the vendor prints against the architect's, proved by code (made-up values)."""
    return {
        "id": "F1",
        "page": 3,
        "item": "Vanity: top width",
        "status": "different",
        "vendor": '50"',
        "architect": '48"',
        "source": "architect",
        "checks": {"printed_architect": "match", "printed_vendor": "match"},
        "trust_level": "confirmed",
        "witnesses": ["printed"],
        "paired_by": "code",
        "inches": {"architect": "48", "vendor": "50", "difference": "2"},
        **more,
    }


@pytest.fixture
def record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Write a temporary record and point the module at it; the gate on, the minimum 30."""
    monkeypatch.delenv(check_record.SWITCH_ENV, raising=False)
    monkeypatch.delenv(check_record.MIN_CASES_ENV, raising=False)
    path = tmp_path / "check_record.json"

    def write(**kinds: tuple[int, int]) -> Path:
        path.write_text(
            json.dumps(
                {"kinds": {k: {"keyed_cases": n, "false_reds": f} for k, (n, f) in kinds.items()}}
            )
        )
        monkeypatch.setattr(check_record, "RECORD_FILE", path)
        return path

    return write


def test_every_kind_is_named_by_the_check_that_makes_it() -> None:
    assert check_record.kind_of(_proved()) == "size_vs_architect"
    assert check_record.kind_of(_proved(source="vendor_arithmetic")) == "vendor_string"
    assert (
        check_record.kind_of(_proved(source="cross_view", reference_side="vendor")) == "vendor_span"
    )
    assert check_record.kind_of(_proved(same_item_pages={"pages": [3, 4]})) == "vendor_pages"
    made = {
        filler_range.CREATED_BY: "filler",
        sink_offset.CREATED_BY: "sink_offset",
        run_fit.CREATED_BY: "run_fit",
        splash_views.CREATED_BY: "splash",
        architect_items.CREATED_BY: "finish_code",
    }
    for created_by, kind in made.items():
        assert check_record.kind_of(_proved(created_by=created_by)) == kind
    assert check_record.kind_of(_proved(settled={"method": "gv_limits"})) == "filler"
    missing = _proved(text_proof={"kind": missing_items.KIND})
    assert check_record.kind_of(missing) == "missing_item"
    assert check_record.kind_of(_proved(tag_read={"outcome": "wrong"})) == "tag"
    assert check_record.kind_of(_proved(witnesses=["text"])) == "finish_code"
    assert check_record.kind_of(_proved(witnesses=["rule"])) == "gv_rule"
    assert same_item_pages.FIELD == "same_item_pages"
    assert set(made.values()) | {"vendor_string", "size_vs_architect"} <= set(check_record.KINDS)


def test_a_clean_record_on_enough_keyed_cases_keeps_the_red(record: Any) -> None:
    record(size_vs_architect=(30, 0))
    finding = _proved()
    assert proof_group(finding) == "clouded"
    assert held_back(finding) is None


def test_too_few_keyed_cases_holds_the_red_as_a_likely_problem(record: Any) -> None:
    record(size_vs_architect=(29, 0))
    finding = _proved()
    assert proof_group(finding) == "likely_problem"
    reason = held_back(finding)
    assert reason is not None and check_record.HELD_WORDS in reason and "29 of the 30" in reason
    proof = proof_of(finding)
    assert proof["group"] == "likely_problem" and proof["reason"] == reason
    assert proof["should_be"] is None  # never sent as an error
    assert likely_words(finding).endswith(f"({check_record.HELD_WORDS}).")


def test_one_false_red_holds_the_kind_whatever_its_cases(record: Any) -> None:
    record(size_vs_architect=(500, 1))
    finding = _proved()
    assert proof_group(finding) == "likely_problem"
    assert "1 false red" in (held_back(finding) or "")


def test_a_kind_the_record_does_not_list_has_no_record(record: Any) -> None:
    record(size_vs_architect=(30, 0))
    assert proof_group(_proved(created_by=sink_offset.CREATED_BY)) == "likely_problem"
    assert proof_group(_proved()) == "clouded"


def test_the_reviewer_agreeing_still_clouds_it(record: Any) -> None:
    record()
    agreed = _proved(decision={"decision": "agree"})
    assert proof_group(agreed) == "clouded"


def test_items_code_did_not_prove_are_unchanged(record: Any) -> None:
    record()
    unproved = _proved(trust_level="to_check", witnesses=[], paired_by=None)
    assert proof_group(unproved) == "not_compared"
    assert held_back(unproved) is None


def test_the_minimum_and_the_switch_are_settings(
    record: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    record(size_vs_architect=(12, 0))
    assert proof_group(_proved()) == "likely_problem"
    monkeypatch.setenv(check_record.MIN_CASES_ENV, "12")
    assert proof_group(_proved()) == "clouded"
    monkeypatch.setenv(check_record.MIN_CASES_ENV, "40")
    assert proof_group(_proved()) == "likely_problem"
    monkeypatch.setenv(check_record.SWITCH_ENV, "off")
    assert proof_group(_proved()) == "clouded"


def test_the_letter_asks_to_check_a_held_item_and_says_why(record: Any) -> None:
    record()
    lines = checks_of([_proved()])
    assert len(lines) == 1
    assert check_record.HELD_WORDS in lines[0].words


def test_the_committed_record_is_counts_for_known_kinds_only() -> None:
    data = json.loads(check_record.RECORD_FILE.read_text(encoding="utf-8"))
    kinds = data["kinds"]
    assert set(kinds) == set(check_record.KINDS)
    for row in kinds.values():
        assert set(row) <= {
            "keyed_cases",
            "false_reds",
            "right_reds",
            "kept_quiet",
            "unjudged_reds",
        }
        assert all(isinstance(v, int) and v >= 0 for v in row.values())
    assert set(data) <= {"about", "measured", "bank_run", "gate_run", "min_cases", "kinds"}


def _ai_rule_claim(**more: Any) -> dict[str, Any]:
    """A helper's GV-rule item (made-up values): it names a GV rule and its value, code read only
    the vendor's number (its millimetres and inches agree); GV's own rule check did not run."""
    base: dict[str, Any] = {
        "item": "Vanity: sink front offset",
        "vendor": '6 1/2" [165mm]',
        "architect": '4"',
        "source": "rules",
        "rule_check_id": "CT-EXAMPLE-RULE-001",
        "checks": {"rule": "not_run", "units": "agrees", "printed_vendor": "match"},
        "witnesses": ["units"],
        "paired_by": None,
        "inches": {"architect": "4", "vendor": "6.5", "difference": "2.5"},
        "why": 'The vendor prints 6 1/2" (the reference section prints 6 1/2" too); GV\'s rule is 4".',
    }
    return _proved(**{**base, **more})


def test_a_helpers_gv_rule_item_is_a_gv_rule_kind_never_a_size_vs_architect() -> None:
    assert check_record.kind_of(_ai_rule_claim()) == "gv_rule"
    assert check_record.kind_of(_proved(source="rules")) == "gv_rule"


def test_a_gv_rule_item_code_never_checked_is_never_red(record: Any) -> None:
    """The paid run of 2026-10-25: a helper clouded a sink's front offset against GV's 4" standard
    while saying the reference's section prints the vendor's 6 1/2". Code's own rule check never ran,
    so code proved nothing about the rule: Not compared, whatever the kinds' records say."""
    record(size_vs_architect=(30, 0), gv_rule=(30, 0))
    claim = _ai_rule_claim()
    assert proof_group(claim) == "not_compared"
    assert held_back(claim) is None
    # Code's own rule check agreeing is still the rule's proof; its kind's record then decides.
    checked = _ai_rule_claim(checks={"rule": "agrees"}, witnesses=["rule"])
    assert proof_group(checked) == "clouded"
    record(size_vs_architect=(30, 0))
    assert proof_group(checked) == "likely_problem"  # gv_rule has no record yet: held back
    assert proof_group(claim) == "not_compared"


# ---- the accuracy line comes from the record (plan change #10) ------------------------------------


def test_the_bound_is_the_exact_rule_of_three_rounded_up() -> None:
    # 0 false reds in n keyed cases: at most 1 - 0.05^(1/n), in whole percent rounded up.
    assert check_record.at_most_percent(0) is None
    assert check_record.at_most_percent(30) == 10
    assert check_record.at_most_percent(51) == 6
    assert check_record.at_most_percent(59) == 5
    assert check_record.at_most_percent(299) == 1


def test_the_accuracy_line_says_what_the_record_says(record: Any) -> None:
    record(size_vs_architect=(51, 0), vendor_string=(31, 0), tag=(4, 0))
    line = check_record.record_line()
    assert line == (
        "Measured on GV's answer keys: the vendor's own dimension strings: 0 false reds in 31 "
        "keyed cases (at most about 10%); sizes against the architect's: 0 false reds in 51 keyed "
        "cases (at most about 6%). Every other kind of check has fewer than 30 clean keyed cases, "
        "so its reds wait as Likely problems."
    )


def test_the_accuracy_line_with_no_cleared_kind_or_a_false_red(record: Any) -> None:
    record(size_vs_architect=(80, 1))
    line = check_record.record_line()
    assert line.startswith(
        "Measured on GV's answer keys: no kind of check has 30 clean keyed cases"
    )
    assert "Held back after a false red: sizes against the architect's: 1 false red in 80" in line
    assert line.endswith(
        "Every other kind of check has fewer than 30 clean keyed cases, so its "
        "reds wait as Likely problems."
    )


def test_the_accuracy_line_follows_the_minimum_and_the_switch(
    record: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    record(size_vs_architect=(20, 0))
    assert "no kind of check has 30" in check_record.record_line()
    monkeypatch.setenv(check_record.MIN_CASES_ENV, "20")
    assert "sizes against the architect's: 0 false reds in 20 keyed cases (at most about 14%)" in (
        check_record.record_line()
    )
    monkeypatch.setenv(check_record.SWITCH_ENV, "off")
    assert "switched off" in check_record.record_line()


def test_every_kind_has_a_short_name() -> None:
    assert set(check_record.SHORT_NAMES) == set(check_record.KINDS)


def test_the_answers_accuracy_says_the_records_line(record: Any) -> None:
    from app.review.assistant.chat_trust import accuracy

    record(size_vs_architect=(51, 0))
    assert accuracy([], [], None)["measured"] == check_record.record_line()
    assert "51 keyed cases" in accuracy([], [], None)["measured"]


def test_the_committed_record_gives_a_line_without_a_hand_edited_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(check_record.SWITCH_ENV, raising=False)
    monkeypatch.delenv(check_record.MIN_CASES_ENV, raising=False)
    line = check_record.record_line(check_record.RECORD_FILE)
    assert line.startswith("Measured on GV's answer keys: ")
    assert "53 of 55" not in line
