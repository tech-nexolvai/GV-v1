"""The reading ledger (chat): string arithmetic, same item in every view, typical units,
completeness both ways and the recorded-results guard, all by code. $0: no model, no PDF."""

from __future__ import annotations

import threading
from decimal import Decimal
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from app.review.assistant.ledger import (
    LEDGER_TOOL_NAMES,
    LEDGER_TOOLS,
    RECORD_READING,
    RECORD_STRING,
    ReadingLedger,
    format_inches,
    governing_view,
    item_key,
    recorded_results_check,
)

BOX = {"x0": 10, "y0": 20, "x1": 60, "y1": 32}


def string(ledger: ReadingLedger, **fields: Any) -> tuple[str, bool]:
    raw = {"page": 3, "side": "vendor", "view": "elevation", **fields}
    return ledger.run_tool("record_string", raw)


def reading(ledger: ReadingLedger, **fields: Any) -> tuple[str, bool]:
    raw = {"side": "vendor", "kind": "width", **fields}
    return ledger.run_tool("record_reading", raw)


def by_source(findings: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    return [finding for finding in findings if finding["source"] == source]


# ---- vendor arithmetic ---------------------------------------------------------------------------


def test_fraction_string_off_by_an_eighth_is_a_vendor_arithmetic_finding() -> None:
    ledger = ReadingLedger(page_count=60)
    text, error = string(
        ledger, parts=["10", '14 1/4"', '10 1/8"'], printed_total='34 1/4"', box=BOX
    )

    assert not error
    assert '10 + 14 1/4" + 10 1/8" = 34 3/8" ≠ 34 1/4" printed (off 1/8")' in text
    assert 'replaces "S1"' in text
    entry = ledger.strings[0]
    assert entry.result == "does_not_add_up"
    assert entry.parts_sum == Decimal("34.375")
    assert entry.off_by == Decimal("-0.125")

    (finding,) = ledger.ledger_findings()
    assert finding["source"] == "vendor_arithmetic"
    assert finding["status"] == "different"
    assert finding["parts"] == ["10", '14 1/4"', '10 1/8"']
    assert finding["printed_total"] == '34 1/4"'
    assert finding["architect"] == '34 3/8"' and finding["vendor"] == '34 1/4"'
    assert finding["vendor_box"] == {"page": 3, "x0": 10.0, "y0": 20.0, "x1": 60.0, "y1": 32.0}
    assert finding["verified"] is False


def test_feet_and_inches_string_adds_up() -> None:
    ledger = ReadingLedger()
    text, _ = string(ledger, parts=["2'-6\"", "3'-0 1/2\"", "1'"], printed_total="6'-6 1/2\"")

    assert ledger.strings[0].result == "adds_up"
    assert ledger.strings[0].off_by == 0
    assert "printed" in text and "≠" not in text
    assert ledger.ledger_findings() == []


def test_a_string_code_cannot_read_is_not_a_number_and_never_a_finding() -> None:
    ledger = ReadingLedger()
    string(ledger, parts=["940 mm", "610 mm"], printed_total="1550 mm")

    assert ledger.strings[0].result == "not_a_number"
    assert ledger.ledger_findings() == []
    assert ledger.ledger_report()["strings"]["not_a_number"] == 1


def test_a_re_read_replaces_the_entry_and_counts_as_checked() -> None:
    ledger = ReadingLedger()
    string(ledger, parts=["12", "12"], printed_total="24 1/8")
    text, error = string(ledger, parts=["12", "12"], printed_total="24 1/8", replaces="S1")

    assert not error
    assert "Still off after your re-read" in text
    assert [entry.id for entry in ledger.strings] == ["S1"]
    assert ledger.ledger_findings()[0]["verified"] is True

    string(ledger, parts=["12", "12"], printed_total="24", replaces="S1")
    assert ledger.ledger_findings() == []
    assert ledger.ledger_report()["strings"] == {
        **ledger.ledger_report()["strings"],
        "total": 1,
        "add_up": 1,
        "off": 0,
    }


def test_an_architect_string_that_is_off_is_counted_but_never_a_vendor_finding() -> None:
    ledger = ReadingLedger()
    string(ledger, side="architect", parts=["10", "10"], printed_total="21")

    assert ledger.ledger_findings() == []
    report = ledger.ledger_report()["strings"]
    assert (report["off"], report["architect_off"], report["vendor_off"]) == (1, 1, 0)
    text, _ = string(
        ledger, side="architect", parts=["10", "10"], printed_total="21", replaces="S1"
    )
    assert "never a vendor error" in text


def test_format_inches() -> None:
    assert format_inches(Decimal("34.375")) == '34 3/8"'
    assert format_inches(Decimal(36)) == '36"'
    assert format_inches(Decimal("-0.125")) == '-1/8"'


# ---- same item, different views ------------------------------------------------------------------


def test_item_names_are_normalised() -> None:
    assert item_key("Base B-36")[0] == item_key("b36 base cabinet")[0] == item_key("B 36 Base")[0]
    assert item_key("Vanity top depth")[0] == item_key("vanity top")[0]
    assert item_key("Vanity V1 #2") == ("v1 vanity", 2)
    assert item_key("Vanity V1 (3)") == ("v1 vanity", 3)
    assert item_key("Base B-36")[0] != item_key("Base B-30")[0]


def test_settled_view_roles_decide_which_view_governs() -> None:
    assert governing_view("B36 base", "width") == "elevation"
    assert governing_view("Left filler", "other") == "elevation"
    assert governing_view("Vanity top overhang", "depth") == "section"
    assert governing_view("Sink cut-out", "cl") == "plan"
    assert governing_view("Wall to wall", "other") == "plan"
    assert governing_view("B36 base", "height") is None


def test_the_elevation_governs_a_width_even_when_the_plans_outnumber_it() -> None:
    ledger = ReadingLedger()
    reading(ledger, page=2, view="plan", item="Base B-36", value="35 1/2", box=BOX)
    reading(ledger, page=4, view="plan", item="b36 base cabinet", value='35 1/2"')
    text, _ = reading(ledger, page=3, view="elevation", item="B 36 Base", value="3'-0\"", box=BOX)

    assert "Differs from" in text
    (finding,) = by_source(ledger.ledger_findings(), "cross_view")
    assert finding["status"] == "check"
    assert finding["architect"] == "3'-0\"" and finding["vendor"] == "35 1/2"
    assert finding["reference_side"] == "vendor"
    assert finding["page"] == 2 and finding["vs_page"] == 3
    assert "the elevation governs widths" in finding["why"]
    assert "same item, different views" in finding["item"]
    assert finding["category"] == "cabinet"
    assert finding["arch_box"]["page"] == 3 and finding["vendor_box"]["page"] == 2
    views = ledger.ledger_report()["views"]
    assert (views["groups"], views["consistent"], views["inconsistent"]) == (1, 0, 1)


def test_the_section_governs_the_overhang_and_the_plan_a_centre_line() -> None:
    ledger = ReadingLedger()
    reading(ledger, page=5, view="plan", item="Vanity top overhang", kind="depth", value="1 1/2")
    reading(ledger, page=6, view="detail", item="Vanity top overhang", kind="depth", value="1 1/2")
    reading(ledger, page=7, view="section", item="Vanity top overhang", kind="depth", value="1")
    reading(ledger, page=5, view="plan", item="Sink", kind="cl", value='18"')
    reading(ledger, page=8, view="elevation", item="Sink", kind="cl", value='18 1/2"')

    findings = by_source(ledger.ledger_findings(), "cross_view")
    overhang = next(f for f in findings if "overhang" in f["item"])
    sink = next(f for f in findings if f["item"].startswith("Sink"))
    assert overhang["architect"] == "1" and "the section governs the overhang" in overhang["why"]
    assert overhang["category"] == "countertop"
    assert sink["architect"] == '18"' and sink["vendor"] == '18 1/2"'
    assert "the plan governs" in sink["why"]


def test_agreeing_views_and_the_two_sides_are_never_a_cross_view_finding() -> None:
    ledger = ReadingLedger()
    reading(ledger, page=2, view="plan", item="B36", value='36"')
    reading(ledger, page=3, view="elevation", item="B36", value="3'-0\"")
    # The architect's value differs from the vendor's: that is the lead's finding, not a view check.
    reading(ledger, page=9, view="elevation", item="B36", value='33"', side="architect")

    assert by_source(ledger.ledger_findings(), "cross_view") == []
    views = ledger.ledger_report()["views"]
    assert (views["groups"], views["consistent"], views["inconsistent"]) == (1, 1, 0)


def test_tags_are_compared_as_normalised_words() -> None:
    ledger = ReadingLedger()
    reading(ledger, page=2, view="plan", item="Vanity top", kind="tag", value="ST-01")
    reading(ledger, page=3, view="elevation", item="Vanity top", kind="tag", value="st 01")
    assert by_source(ledger.ledger_findings(), "cross_view") == []

    reading(ledger, page=4, view="section", item="Vanity top", kind="tag", value="ST-02")
    (finding,) = by_source(ledger.ledger_findings(), "cross_view")
    assert finding["architect"] == "ST-01" and finding["vendor"] == "ST-02"
    assert "the elevation governs widths, fillers and tags" in finding["why"]


def test_a_size_code_cannot_read_is_left_out_of_the_compare() -> None:
    ledger = ReadingLedger()
    text, _ = reading(ledger, page=2, view="plan", item="B36", value="see detail")
    reading(ledger, page=3, view="elevation", item="B36", value='36"')

    assert "cannot read it as a number" in text
    assert ledger.ledger_findings() == []
    assert ledger.ledger_report()["views"]["not_compared"] == 1


# ---- typical units -------------------------------------------------------------------------------


def test_typical_units_that_disagree_are_flagged_against_the_shared_value() -> None:
    ledger = ReadingLedger()
    for page, value in ((10, '30"'), (11, '30"'), (12, '30 1/2"')):
        reading(ledger, page=page, view="elevation", item=f"Vanity V1 #{page - 9}", value=value)

    (finding,) = by_source(ledger.ledger_findings(), "cross_view")
    assert "typical units" in finding["item"]
    assert finding["architect"] == '30"' and finding["vendor"] == '30 1/2"'
    assert finding["page"] == 12
    assert "elevation p.12 #3" in finding["why"]
    assert ledger.ledger_report()["views"]["typical_groups"] == 1


def test_a_split_with_no_governing_view_still_names_every_value() -> None:
    ledger = ReadingLedger()
    reading(ledger, page=2, view="elevation", item="Tall T24", kind="height", value='84"')
    reading(ledger, page=3, view="section", item="Tall T24", kind="height", value='90"')

    (finding,) = by_source(ledger.ledger_findings(), "cross_view")
    assert "no view governs" in finding["why"]
    assert '84"' in finding["why"] and '90"' in finding["why"]


# ---- completeness both ways ----------------------------------------------------------------------


def test_completeness_both_ways_from_readings_findings_and_the_sheet_index() -> None:
    ledger = ReadingLedger()
    reading(ledger, page=20, view="elevation", item="B36 base", value='36"', side="architect")
    reading(ledger, page=20, view="elevation", item="Pantry T24", value='24"', side="architect")
    reading(ledger, page=3, view="elevation", item="Base B-36", value='36"')
    reading(ledger, page=4, view="elevation", item="Filler F3", value='3"', box=BOX)
    findings = [
        {
            "page": 5,
            "vs_page": 21,
            "item": "Vanity top depth",
            "architect": '22"',
            "vendor": '22 1/2"',
            "status": "different",
            "source": "architect",
        },
        {
            "page": 6,
            "vs_page": 22,
            "item": "Wall W30",
            "architect": '30"',
            "vendor": "not shown",
            "status": "different",
            "source": "architect",
        },
        {
            "page": 7,
            "item": "Dimension string",
            "architect": '12"',
            "vendor": '12 1/8"',
            "status": "different",
            "source": "vendor_arithmetic",
        },
    ]
    architect_items = [{"item": "Vanity top", "page": 21}, {"item": "Linen L18", "page": 30}]
    coverage = {"pages": [{"page": 30, "status": "not_opened"}, {"page": 3, "status": "same"}]}

    report = ledger.ledger_report(findings, coverage, architect_items)["completeness"]
    assert report["compared"] is True
    assert report["missing_on_vendor"] == ["Pantry T24", "Wall W30"]
    assert report["extra_on_vendor"] == ["Filler F3"]
    assert report["not_looked_at"] == ["Linen L18"]
    assert (report["architect_items"], report["vendor_items"]) == (5, 3)

    made = by_source(ledger.ledger_findings(findings, coverage, architect_items), "completeness")
    assert {(f["item"], f["architect"], f["vendor"]) for f in made} == {
        ("Pantry T24", "shown", "not found"),
        ("Wall W30", "shown", "not found"),
        ("Filler F3", "not found", "shown"),
    }
    filler = next(f for f in made if f["item"] == "Filler F3")
    assert filler["status"] == "check" and filler["vendor_box"]["page"] == 4


def test_completeness_needs_both_sides() -> None:
    ledger = ReadingLedger()
    reading(ledger, page=3, view="elevation", item="B36", value='36"')

    report = ledger.ledger_report()["completeness"]
    assert report["compared"] is False and report["extra_on_vendor"] == []
    assert by_source(ledger.ledger_findings(), "completeness") == []


# ---- never drop a recorded result ----------------------------------------------------------------

PAYLOAD: dict[str, Any] = {
    "ai_page_review": [
        {
            "page": 34,
            "status": "done",
            "different": [
                {
                    "key": "air:1",
                    "measures": "vanity width",
                    "architect_text": "3'-1\"",
                    "vendor_text": '46"',
                    "result": "different",
                    "verified_by": "evaluator",
                },
                {
                    "key": "air:2",
                    "measures": "top depth",
                    "architect_text": '22"',
                    "vendor_text": '23"',
                    "result": "different",
                    "verified_by": None,
                },
            ],
        },
        {
            "page": 35,
            "status": "done",
            "rows": [
                {
                    "key": "air:3",
                    "measures": "filler",
                    "architect_text": '2"',
                    "vendor_text": '3"',
                    "result": "different",
                    "verified_by": "evaluator",
                },
                {
                    "key": "air:4",
                    "measures": "height",
                    "architect_text": '34"',
                    "vendor_text": '34"',
                    "result": "same",
                    "verified_by": "evaluator",
                },
            ],
        },
    ],
    "records": {
        "countertops": [
            {
                "id": "c1",
                "page": 12,
                "label": "Kitchen top",
                "outcome": "Needs correction",
                "printed_overall": '96 1/8"',
                "needed_overall": '96"',
            },
            {"id": "c2", "page": 13, "label": "Island", "outcome": "Looks right"},
        ],
        "other_checks": [
            {"id": "k1", "check": "Sink clearance", "outcome": "Needs correction", "pages": [40]},
        ],
        "review_list": {"failures": [{"id": "c1", "page": 12, "what": "Kitchen top"}]},
    },
}


def test_recorded_results_check_lists_what_the_answer_left_out() -> None:
    findings = [
        {"page": 34, "item": "Vanity", "architect": "37", "vendor": "46 in", "source": "architect"},
        {
            "page": 99,
            "item": "Top",
            "architect": "96",
            "vendor": "96 1/8",
            "source": "rules",
            "rule_check_id": "c1",
        },
    ]
    result = recorded_results_check(PAYLOAD, findings)

    assert (result["recorded"], result["included"]) == (4, 2)
    assert [item["key"] for item in result["missing"]] == ["k1", "air:3"]
    assert result["missing"][1] == {
        "key": "air:3",
        "kind": "ai_review_difference",
        "pages": [35],
        "what": "filler",
        "architect": '2"',
        "vendor": '3"',
    }
    assert result["send_back"] is not None
    assert "k1 (p.40: Sink clearance)" in result["send_back"]


def test_recorded_results_check_accepts_a_rules_finding_on_the_page_and_dismissals() -> None:
    findings = [
        {
            "page": 40,
            "item": "Sink clearance",
            "architect": '3"',
            "vendor": '2"',
            "source": "rules",
        },
        {"page": 34, "item": "Vanity", "architect": "37", "vendor": "46", "source": "architect"},
        {"page": 12, "item": "Top", "architect": "96", "vendor": "96 1/8", "source": "architect"},
    ]
    result = recorded_results_check(PAYLOAD, findings, dismissed={"air:3": "the filler moved"})

    assert result["missing"] == [] and result["send_back"] is None
    assert (result["included"], result["dismissed"]) == (3, 1)


def test_a_finding_that_contradicts_the_recorded_architect_value_does_not_include_it() -> None:
    findings = [
        {"page": 34, "item": "Vanity", "architect": "40", "vendor": "46", "source": "architect"}
    ]
    missing = recorded_results_check(PAYLOAD, findings)["missing"]
    assert "air:1" in [item["key"] for item in missing]


def test_recorded_results_check_without_a_payload() -> None:
    assert recorded_results_check(None, [])["recorded"] == 0
    assert (
        recorded_results_check({"ai_review_and_job_values": "could not be read"}, [])["send_back"]
        is None
    )


# ---- tool schemas and input checks ---------------------------------------------------------------


@pytest.mark.parametrize("tool", LEDGER_TOOLS, ids=lambda tool: tool["name"])
def test_tool_schemas_are_valid_json_schema(tool: dict[str, Any]) -> None:
    assert set(tool) == {"name", "description", "input_schema"}
    Draft202012Validator.check_schema(tool["input_schema"])
    assert LEDGER_TOOL_NAMES == {"record_string", "record_reading"}


def test_good_inputs_pass_the_schema_and_bad_ones_fail_both_schema_and_code() -> None:
    strings = Draft202012Validator(RECORD_STRING["input_schema"])
    readings = Draft202012Validator(RECORD_READING["input_schema"])
    good_string = {
        "page": 3,
        "view": "plan",
        "side": "vendor",
        "parts": ["12", "12"],
        "printed_total": "24",
        "box": {**BOX, "file": "vendor.pdf"},
    }
    good_reading = {
        "page": 3,
        "view": "section",
        "side": "architect",
        "item": "B36",
        "kind": "depth",
        "value": '24"',
        "box": {**BOX, "page": 4},
    }
    assert not list(strings.iter_errors(good_string))
    assert not list(readings.iter_errors(good_reading))
    ledger = ReadingLedger(page_count=10)
    assert ledger.run_tool("record_string", good_string)[1] is False
    assert ledger.run_tool("record_reading", good_reading)[1] is False
    assert ledger.readings[0].box is not None and ledger.readings[0].box.page == 4

    bad_strings = [
        {**good_string, "side": "gv"},
        {**good_string, "parts": ["12"]},
        {**good_string, "page": 0},
        {**good_string, "extra": 1},
    ]
    # The schema cannot say "parts or part_dims"; code refuses a string with neither.
    for raw in (
        {key: value for key, value in good_string.items() if key != "printed_total"},
        {key: value for key, value in good_string.items() if key != "parts"},
        {**good_string, "part_dims": ["D1", "D2"]},
    ):
        text, error = ledger.run_tool("record_string", raw)
        assert error and text.startswith("Not recorded:"), text
    bad_readings = [
        {**good_reading, "kind": "length"},
        {**good_reading, "view": "isometric"},
        {**good_reading, "box": {"x0": 1, "y0": 1}},
    ]
    for raw in bad_strings:
        assert list(strings.iter_errors(raw))
        text, error = ledger.run_tool("record_string", raw)
        assert error and text.startswith("Not recorded:")
    for raw in bad_readings:
        assert list(readings.iter_errors(raw))
        assert ledger.run_tool("record_reading", raw)[1] is True


@pytest.mark.parametrize(
    ("name", "raw", "says"),
    [
        (
            "record_string",
            {"page": 11, "side": "vendor", "parts": ["1", "1"], "printed_total": "2"},
            "not in this set",
        ),
        (
            "record_string",
            {
                "page": 1,
                "side": "vendor",
                "parts": ["1", "1"],
                "printed_total": "2",
                "replaces": "S9",
            },
            "not in this answer",
        ),
        (
            "record_reading",
            {
                "page": 1,
                "view": "plan",
                "side": "vendor",
                "item": "the",
                "kind": "width",
                "value": "1",
            },
            "must name the item",
        ),
        (
            "record_reading",
            {
                "page": 1,
                "view": "plan",
                "side": "vendor",
                "item": "B36",
                "kind": "width",
                "value": "1",
                "box": {"x0": 5, "y0": 5, "x1": 1, "y1": 9},
            },
            "right of",
        ),
        ("view", {}, "not a ledger tool"),
    ],
)
def test_code_refuses_what_it_cannot_record(name: str, raw: dict[str, Any], says: str) -> None:
    text, error = ReadingLedger(page_count=10).run_tool(name, raw)
    assert error and says in text


def test_helpers_record_from_their_own_threads() -> None:
    ledger = ReadingLedger()

    def record(n: int) -> None:
        for i in range(20):
            ledger.run_tool(
                "record_reading",
                {
                    "page": 1 + i,
                    "view": "plan",
                    "side": "vendor",
                    "item": f"B{n}",
                    "kind": "width",
                    "value": f"{n}",
                },
                by=f"helper:{n}",
            )

    threads = [threading.Thread(target=record, args=(n,)) for n in range(1, 6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    ids = [entry.id for entry in ledger.readings]
    assert len(ids) == len(set(ids)) == 100
    assert ledger.ledger_findings() == []


def test_summary_and_storage() -> None:
    ledger = ReadingLedger()
    string(ledger, parts=["12", "12"], printed_total="24")
    reading(ledger, page=2, view="plan", item="B36", value='36"')
    assert ledger.ledger_summary() == (
        "Ledger: 1 string (1 add up, 0 off) · 1 reading, 0 items seen more than once (0 disagree)."
    )
    stored = ledger.as_dict()
    assert stored["strings"][0]["sum_text"] == '12 + 12 = 24" = 24" printed'
    assert stored["readings"][0]["item_key"] == "b36" and stored["readings"][0]["inches"] == "36"


# ---- strings by label (prompts design W1: label ids as the output) -------------------------------

_LABELS = {
    (7, "D12"): {
        "id": "D12",
        "page": 7,
        "text": '10 1/4"',
        "inches": "10.25",
        "box": [10, 50, 30, 58],
    },
    (7, "D13"): {
        "id": "D13",
        "page": 7,
        "text": "610 [24]",
        "inches": "24",
        "box": [40, 50, 70, 58],
    },
    (7, "D14"): {"id": "D14", "page": 7, "text": '34"', "inches": "34", "box": [10, 70, 80, 78]},
    (8, "D2"): {"id": "D2", "page": 8, "text": "TYP", "inches": "x", "box": [1, 1, 2, 2]},
}


def _lookup(ref: object, page: int, side: str) -> dict[str, Any]:
    assert side == "vendor"
    named = str(ref)
    if named.startswith("p8."):
        page, named = 8, named[3:]
    label = _LABELS.get((page, named))
    if label is None:
        raise LookupError(f"page {page} has no label {named}")
    return {**label, "file": "set.pdf"}


def test_a_string_recorded_by_labels_is_read_and_added_by_code() -> None:
    ledger = ReadingLedger(page_count=10, labels=_lookup)
    text, error = ledger.run_tool(
        "record_string",
        {"page": 7, "side": "vendor", "part_dims": ["D12", "D13"], "total_dim": "D14"},
    )
    assert error is False, text
    entry = ledger.strings[0]
    # Each label's text as printed (code reads "610 [24]" as its 24 inches).
    assert entry.parts == ('10 1/4"', "610 [24]") and entry.printed_total == '34"'
    assert entry.result == "does_not_add_up" and entry.off_by == Decimal("-0.25")
    assert entry.dims == ("p7.D12", "p7.D13", "p7.D14")
    assert entry.box is not None and (entry.box.x0, entry.box.y1) == (10, 78)


def test_a_string_partly_typed_is_not_a_label_string_and_bad_labels_are_refused() -> None:
    ledger = ReadingLedger(page_count=10, labels=_lookup)
    text, error = ledger.run_tool(
        "record_string",
        {"page": 7, "side": "vendor", "part_dims": ["D12", "D13"], "printed_total": '34 1/4"'},
    )
    assert error is False and ledger.strings[0].dims == ()
    assert ledger.strings[0].result == "adds_up"
    for raw, says in (
        ({"page": 7, "side": "vendor", "part_dims": ["D12", "D99"], "total_dim": "D14"}, "D99"),
        ({"page": 7, "side": "vendor", "part_dims": ["D12", "p8.D2"], "total_dim": "D14"}, "D2"),
        (
            {"page": 7, "side": "vendor", "parts": ["1", "2"], "part_dims": ["D12", "D13"]},
            "not both",
        ),
    ):
        text, error = ledger.run_tool("record_string", raw)
        assert error and says in text, text
    no_lookup = ReadingLedger(page_count=10)
    text, error = no_lookup.run_tool(
        "record_string",
        {"page": 7, "side": "vendor", "part_dims": ["D12", "D13"], "total_dim": "D14"},
    )
    assert error and "as printed" in text
