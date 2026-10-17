"""Your-decision items (#1545): decision help, printed against printed is Wrong, the extent check,
and the misread hint on a vendor string that does not add up. All $0: tiny PDFs, scripted replies.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant.chat_drawing import entry_lines
from app.review.assistant.chat_findings import (
    CHECK_REASONS,
    COVERAGE_CHECK_PREFIX,
    REPORT_FINDING,
    decision_help_lines,
    decision_help_note,
    helpless_decisions,
    printed_conflict,
)
from app.review.assistant.ledger import ReadingLedger, misread_values, string_misreads
from app.review.assistant.sandbox import SandboxRefused
from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text, tool
from tests.review.assistant.test_chat_engine import Harness
from tests.review.assistant.test_chat_engine_findings import _modules, _sent
from tests.review.assistant.test_chat_findings import book, finding_input, workspace_with_pdf

SKILL = Path(__file__).parents[3] / "app/review/assistant/skills/gv-review/SKILL.md"

HELP: dict[str, Any] = {
    "measures": "From the wall to the end of the 6\" blocking, not to the counter's end",
    "estimate": {
        "value_in": 185.5,
        "plus_minus_in": 1,
        "method": "outlet string added up, and scaled against two printed dims",
    },
    "likely": "probably fine",
    "confirm": "Confirm on site",
}


def _check(**changes: Any) -> dict[str, Any]:
    """A Your-decision finding: the architect prints no size, the vendor prints 191"."""
    return finding_input(
        status="check", architect="not dimensioned", vendor='191"', item="Run length", **changes
    )


# ---- decision help -------------------------------------------------------------------------------


def test_report_finding_takes_decision_help_and_the_lines_read_like_a_reviewer(
    tmp_path: Path,
) -> None:
    assert "decision_help" in REPORT_FINDING["input_schema"]["properties"]
    assert "decision_help" not in REPORT_FINDING["input_schema"]["required"]
    findings = book(tmp_path)
    finding, notes = findings.report(_check(decision_help=HELP), "lead")
    assert finding["status"] == "check" and not notes
    assert finding["decision_help"]["estimate"]["value_in"] == 185.5
    assert decision_help_lines(finding) == [
        "What it measures: From the wall to the end of the 6\" blocking, not to the counter's end",
        (
            'Our best estimate: about 185 1/2" (±1"): outlet string added up, and scaled against '
            "two printed dims"
        ),
        "Likely: probably fine",
        "What to confirm: Confirm on site",
    ]
    # The cloud drawing's notes pages print the same lines.
    assert entry_lines(finding)[-4:] == decision_help_lines(finding)
    no_estimate = findings.report(_check(decision_help={**HELP, "estimate": None}), "lead")[0]
    assert "Our best estimate: none" in decision_help_lines(no_estimate)


@pytest.mark.parametrize(
    ("bad", "why"),
    [
        ({**HELP, "likely": "maybe"}, "likely must be one of"),
        ({k: v for k, v in HELP.items() if k != "confirm"}, "needs 'confirm'"),
        ({**HELP, "estimate": {**HELP["estimate"], "value_in": "185"}}, "must be a number"),
        ({**HELP, "extra": 1}, "unknown field"),
        ({**HELP, "measures": ""}, "measures is empty"),
    ],
)
def test_bad_decision_help_is_refused(tmp_path: Path, bad: dict[str, Any], why: str) -> None:
    with pytest.raises(SandboxRefused, match=why):
        book(tmp_path).report(_check(decision_help=bad), "lead")


def test_your_decision_items_without_help_are_named_for_the_send_back(tmp_path: Path) -> None:
    findings = book(tmp_path)
    bare = findings.report(_check(), "helper:1")[0]
    findings.report(_check(decision_help=HELP), "lead")
    findings.report(finding_input(), "lead")  # a Wrong needs none
    missing = helpless_decisions(findings.live())
    assert missing == [{"id": bare["id"], "page": 2, "item": "Run length"}]
    note = decision_help_note(missing)
    assert note.startswith(COVERAGE_CHECK_PREFIX)
    assert f"{bare['id']} (page 2: Run length)" in note and "update_finding" in note
    assert not decision_help_note(missing, prefixed=False).startswith(COVERAGE_CHECK_PREFIX)
    findings.update({"finding_id": bare["id"], "decision_help": HELP})
    assert helpless_decisions(findings.live()) == []


def test_the_finish_check_sends_the_lead_back_once_for_decision_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    provider = ScriptedProvider(
        lead=[
            reply(tool("r1", "report_finding", _check()), stop="tool_use"),
            reply(text("One item needs your decision.")),
            reply(
                tool("u1", "update_finding", {"finding_id": "F1", "decision_help": HELP}),
                stop="tool_use",
            ),
            reply(text("One item needs your decision: probably fine, confirm on site.")),
        ]
    )
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    note = _sent(provider.lead_calls[2])["messages"][-1]["content"][-1]["text"]
    assert note.startswith(COVERAGE_CHECK_PREFIX) and "decision_help" in note and "F1" in note
    assert len(provider.lead_calls) == 4
    shown = [b["finding"] for b in outcome.blocks if b["type"] == "finding"]
    assert shown[-1]["decision_help"]["likely"] == "probably fine"


# ---- printed against printed is Wrong ------------------------------------------------------------


def test_two_printed_values_that_differ_are_wrong_not_your_decision(tmp_path: Path) -> None:
    findings = book(tmp_path)
    finding, notes = findings.report(
        finding_input(status="check", architect="2'-9\"", vendor='34"', item="Counter height"),
        "lead",
    )
    assert finding["status"] == "different"
    assert any("status different (Wrong)" in note and "check_reason" in note for note in notes)
    # Asking for "check" again without a reason flips it back; with a reason it stays.
    again, _ = findings.update({"finding_id": finding["id"], "status": "check"})
    assert again["status"] == "different"
    kept, notes = findings.update(
        {"finding_id": finding["id"], "status": "check", "check_reason": "which_governs"}
    )
    assert kept["status"] == "check" and not notes


@pytest.mark.parametrize(
    "change",
    [
        {"architect": 'about 33" (scaled)'},  # scaled is never Wrong by itself
        {"architect": "not dimensioned"},
        {"architect": '34"'},  # the same value
        {"check_reason": "site_verify"},
        {"source": "brand_prototype"},  # prototype vs vendor: which governs
        {"vendor": '34" and 36"'},  # not one value
    ],
)
def test_printed_conflict_leaves_these_as_your_decision(change: dict[str, Any]) -> None:
    finding = {
        "status": "check",
        "source": "architect",
        "architect": "2'-9\"",
        "vendor": '34"',
        "created_by": "lead",
        **change,
    }
    assert printed_conflict(finding) is None


def test_printed_conflict_reads_both_sides_in_inches() -> None:
    finding = {"status": "check", "architect": "2'-9\" AFF", "vendor": '34"', "created_by": "lead"}
    conflict = printed_conflict(finding)
    assert conflict is not None and [str(v) for v in conflict] == ["34", "33"]
    assert printed_conflict({**finding, "created_by": "ledger"}) is None


# ---- the extent check ----------------------------------------------------------------------------

EXTENT = {
    "file": "Vendor set.pdf",
    "page": 1,
    "printed": "8'-11\"",
    "printed_in": 107.0,
    "covers_more": True,
    "unit_in": 89.0,
    "plus_minus_in": 0.75,
    "measures": "the printed 8'-11\" runs past the unit's own outline, about 18\" above the unit "
    "(a box, soffit, blocking or wall beyond it), not the unit alone",
    "words": "Extent: the printed 8'-11\" spans more than the unit",
}


def test_a_dimension_that_spans_more_than_the_unit_is_your_decision_not_wrong(
    tmp_path: Path,
) -> None:
    findings = book(tmp_path)
    findings.extents = [EXTENT]
    finding, notes = findings.report(
        finding_input(
            item="Market unit height", architect="8'-11\"", vendor='89 1/2"', arch_box=None
        ),
        "lead",
    )
    assert finding["status"] == "check" and finding["check_reason"] == "different_extent"
    assert any("not Wrong" in note for note in notes)
    help_ = finding["decision_help"]
    assert "above the unit" in help_["measures"]
    assert help_["estimate"]["value_in"] == 89.0 and help_["likely"] == "probably fine"
    assert "by others" in help_["confirm"]
    # Printed against printed defers to the extent check: it stays Your decision.
    assert printed_conflict(finding) is None
    again, _ = findings.update({"finding_id": finding["id"], "status": "different"})
    assert again["status"] == "check"


def test_the_extent_check_only_touches_the_same_printed_value_on_its_page(
    tmp_path: Path,
) -> None:
    findings = book(tmp_path)
    findings.extents = [EXTENT]
    other_value = findings.report(finding_input(architect='37"', vendor="3'-10\""), "lead")[0]
    assert other_value["status"] == "different" and other_value.get("check_reason") is None
    other_page = findings.report(
        finding_input(page=3, vs_page=3, architect="8'-11\"", vendor='89 1/2"', arch_box=None),
        "lead",
    )[0]
    assert other_page["status"] == "different"
    assert "different_extent" in CHECK_REASONS


# ---- the misread hint on a vendor string ---------------------------------------------------------


def test_misread_values_try_the_common_slips() -> None:
    values = misread_values(Decimal("13.125"))
    assert values[Decimal(13)] == "its fraction dropped"
    assert Decimal("31.125") in values  # two digits swapped
    assert Decimal("13.25") in values  # off by 1/8


def test_the_one_change_that_makes_a_string_add_up_is_named_and_seen_in_the_other_round() -> None:
    ledger = ReadingLedger(page_count=60)
    first = ledger.record_string(
        {
            "page": 3,
            "side": "vendor",
            "parts": ['10"', '13 1/8"', '16 1/2"'],
            "printed_total": '39 1/2"',
        }
    )
    assert 'the part 13 1/8" (its fraction dropped); as 13" the string adds up exactly' in first
    ledger.record_string(
        {"page": 39, "side": "vendor", "parts": ['13 1/8"', '32 1/4"'], "printed_total": '45 1/4"'}
    )
    whys = [f["why"] for f in ledger.ledger_findings() if f["source"] == "vendor_arithmetic"]
    assert len(whys) == 2
    assert 'The same 13 1/8" is off the same way on p.39 (to 45 1/4")' in whys[0]
    assert 'The same 13 1/8" is off the same way on p.3 (to 39 1/2")' in whys[1]
    assert all("A hint only" in why for why in whys)
    # Never corrected: the recorded parts are as printed.
    assert ledger.strings[0].parts == ('10"', '13 1/8"', '16 1/2"')


def test_a_swapped_digit_and_an_ambiguous_string() -> None:
    swapped = string_misreads(['31"', '10"'], '23"')
    assert [(m.printed, str(m.likely)) for m in swapped] == [('31"', "13")]
    ledger = ReadingLedger(page_count=9)
    ambiguous = ledger.record_string(
        {"page": 1, "side": "vendor", "parts": ['10"', '20"'], "printed_total": '30 1/4"'}
    )
    # A plain 1/4" fits either part (or the overall's dropped fraction): code names one only when
    # it can tell; here the overall's dropped fraction is the one telling slip.
    assert 'the overall 30 1/4" (its fraction dropped)' in ambiguous
    assert string_misreads(['10"', '20"'], '30"') == []


# ---- the skill -----------------------------------------------------------------------------------


def test_skill_md_requires_decision_help_and_the_two_status_rules() -> None:
    text_ = SKILL.read_text(encoding="utf-8")
    assert len(text_.splitlines()) <= 260
    for words in (
        "**Every `check` (Your decision) has `decision_help`**",
        "**Extent first**",
        "`measure_on_drawing` with `dimension`",
        "**Printed vs printed is Wrong**",
        "a brand prototype string that sums to the vendor's value",
    ):
        assert words in text_, words
