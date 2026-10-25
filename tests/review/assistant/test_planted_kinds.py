"""Planted cases per kind of check: made-up drawings with a planted mistake or a planted trap
(plan change #10). Made-up values only; no client file.

A kind of check earns its licence to make reds only from REAL keyed cases on GV's answer keys
(`check_record.json` `keyed_cases`, measured by the owner's local test bank). Planted cases never
count towards it. They show something else: that each check finds a mistake planted on a made-up
drawing (a **mistake**) and stays non-red on a planted look-alike that is not one (a **trap**). Each
entry below names a test that builds such a drawing, runs the real check on it and asserts the
outcome; CI runs every one of them. `check_record.json` states the counts (`planted_mistakes`,
`planted_traps`), and this module keeps the two the same.

A kind with no entry has no planted case yet (said so in the record as 0).
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping

import pytest

from app.review.assistant import check_record

_HERE = "tests.review.assistant."

#: kind -> {"mistake": [module:test, ...], "trap": [module:test, ...]}
PLANTED: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    "vendor_span": {
        "mistake": (
            "test_vendor_consistency:test_the_same_span_printed_twice_names_the_value_the_drawing_backs",
        ),
        "trap": (
            "test_vendor_consistency:test_a_part_detail_beside_an_elevation_is_never_the_same_span",
        ),
    },
    "vendor_pages": {
        "mistake": (
            "test_same_item_pages:test_code_reading_both_labels_makes_a_red_with_no_should_be_picked",
        ),
        "trap": (
            "test_same_item_pages:test_two_rounds_of_one_drawing_are_a_revision_not_a_mistake",
            "test_same_item_pages:test_the_same_page_a_tag_or_another_maker_is_left_as_it_is",
        ),
    },
    "filler": {
        # GV's 1"-2" range is a note, never red (DECIDED 2026-10-20): a planted 5" filler is noted.
        "mistake": (
            "test_filler_range:test_a_filler_wider_than_the_maximum_is_a_gv_note_never_red",
            "test_filler_range:test_a_filler_narrower_than_the_minimum_is_a_gv_note_never_red",
        ),
        "trap": (
            "test_filler_range:test_a_two_inch_filler_is_within_the_range_and_adds_nothing",
            "test_filler_range:test_a_four_inch_dimension_that_is_not_a_filler_is_ignored",
        ),
    },
    "sink_offset": {
        "mistake": (
            "test_sink_offset:test_architect_silent_and_vendor_three_inches_is_red_from_the_gv_standard",
        ),
        "trap": (
            "test_sink_offset:test_vendor_matching_the_standard_with_the_architect_silent_is_right",
            "test_sink_offset:test_architect_printing_another_value_is_never_red_the_reviewer_decides",
            "test_sink_offset:test_a_second_front_edge_makes_the_offset_unsure_never_red",
            "test_sink_offset:test_a_value_read_from_drawn_ink_is_never_red",
        ),
    },
    "sink_centre": {
        "mistake": ("test_dim_ends:test_a_sink_off_the_base_centre_is_a_difference",),
        "trap": (
            "test_dim_ends:test_a_sink_on_the_base_centre_is_right",
            "test_dim_ends:test_no_comparison_when_the_reference_does_not_draw_the_sink_centred",
        ),
    },
    "run_fit": {
        "mistake": (
            "test_run_fit:test_a_filler_changed_instead_of_the_doors_is_wrong",
            "test_run_fit:test_unequal_doors_and_a_changed_drawer_bank_are_wrong",
        ),
        "trap": (
            "test_run_fit:test_a_door_narrowed_by_the_site_is_right_not_red",
            "test_run_fit:test_two_architect_runs_that_fit_give_no_finding",
        ),
    },
    "splash": {
        "mistake": ("test_hard_uploads:test_splash_that_differs_is_a_likely_problem_never_red",),
        "trap": (
            "test_hard_uploads:test_splash_sized_on_the_same_sheets_section_is_compared",
            "test_hard_uploads:test_splash_never_compared_with_another_part_or_kind",
        ),
    },
    "finish_code": {
        "mistake": (
            "test_architect_items:test_a_finish_the_vendor_never_prints_is_a_missed_wrong",
            "test_architect_items:test_one_code_each_side_that_differs_is_a_changed_finish",
        ),
        "trap": (
            "test_architect_items:test_codes_are_one_code_however_they_are_spelled",
            "test_architect_items:test_a_material_named_in_words_is_not_compared",
        ),
    },
}


@pytest.mark.parametrize(
    "entry",
    [entry for roles in PLANTED.values() for entries in roles.values() for entry in entries],
)
def test_every_planted_case_names_a_test_that_exists(entry: str) -> None:
    module, name = entry.split(":")
    assert callable(getattr(importlib.import_module(_HERE + module), name, None)), entry


def test_planted_kinds_are_known_kinds_and_every_entry_is_listed_once() -> None:
    assert set(PLANTED) <= set(check_record.KINDS)
    entries = [e for roles in PLANTED.values() for es in roles.values() for e in es]
    assert len(entries) == len(set(entries))
    assert all(set(roles) <= {"mistake", "trap"} for roles in PLANTED.values())


def test_the_record_states_the_planted_counts_and_they_never_count_for_the_licence() -> None:
    data = json.loads(check_record.RECORD_FILE.read_text(encoding="utf-8"))
    for kind in check_record.KINDS:
        row = data["kinds"][kind]
        roles = PLANTED.get(kind, {})
        assert row.get("planted_mistakes", 0) == len(roles.get("mistake", ())), kind
        assert row.get("planted_traps", 0) == len(roles.get("trap", ())), kind
        # The licence reads keyed_cases and false_reds only: planted cases are never added in.
        record = check_record.records(check_record.RECORD_FILE)[kind]
        assert record.keyed_cases == row["keyed_cases"]
