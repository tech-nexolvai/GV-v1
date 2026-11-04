"""Fixed questions become slots: the AI answers by naming numbered marks, code does the rest (Step 4).

**Why.** In the audits 75% of the real problems nobody raised were questions nobody asked, and an
open "what looks wrong?" answers differently each run. In flag mode a red could even rest on numbers
the AI typed. A slot fixes both: the question is fixed (it comes from `vocabulary/review_questions.yaml`
through `question_list`, never from a second list), and the AI's whole answer is *which numbered
marks* on the two pictures answer it. Code reads each mark's value from its own reading of the
drawing (`dims.json`), adds lists exactly, and does the maths (`slot_maths`).

**Which questions are slots.** A question decided by code whose answer is a size that can be pointed
at on one vendor view and its paired architect view. `_HOW` says, for each such question, how code
does its maths:

- `string`: the vendor's own parts against the vendor's own printed overall of the same line;
- `vendor_equal`: one vendor size against another vendor size of the same item (the splash against
  the top's run);
- `architect`: the vendor's size against the architect's size, exactly in inches;
- `rule`: GV's published rule (`rules/rulebook`) run by the isolated verdict engine (`slot_rules`);
- `job_value`: the vendor's size against a value the reviewer enters per job, exactly.

Every other code question is listed in `NOT_SLOTS` with the reason it is not a slot; a test checks
that the two together are exactly the code questions of the file.

**Off by default.** `GV_ASSISTANT_SLOTS` = `off` (the default) or `on`; off, nothing in the full check
changes (`slots_on`).

Pure: reads the question list, no I/O beyond it, no AI call.
Verification: tests/review/assistant/test_slots.py (made-up questions, no client data).
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from app.review.assistant import question_list
from app.review.assistant.question_list import Question

__all__ = [
    "MEASURES",
    "NOT_SLOTS",
    "SLOTS_ENV",
    "Maths",
    "Slot",
    "catalogue",
    "not_slots",
    "slot",
    "slots_on",
]

#: The switch (`Settings.assistant_slots`): `on` runs the slot calls; anything else (`off`, the
#: default) leaves the full check exactly as it is.
SLOTS_ENV: Final = "GV_ASSISTANT_SLOTS"

type Maths = Literal["string", "vendor_equal", "architect", "rule", "job_value"]

#: What a named dimension measures, from a fixed vocabulary, so code can check that both sides of
#: one answer measure the same kind of thing (a depth is never held against a width).
MEASURES: Final[tuple[str, ...]] = (
    "overall_length",
    "overall_depth",
    "overall_height",
    "thickness",
    "part_width",
    "part_height",
    "offset_from_front_edge",
    "cutout_width",
    "cutout_depth",
    "opening_width",
    "clear_width",
    "clear_height",
    "height_above_floor",
    "overhang",
    "position_along",
)


def slots_on() -> bool:
    """Whether the full check asks the slot questions (`GV_ASSISTANT_SLOTS` = `on`)."""
    return os.environ.get(SLOTS_ENV, "off").strip().lower() == "on"


@dataclass(frozen=True, slots=True)
class _How:
    """How code answers one question from named marks."""

    maths: Maths
    measures: tuple[str, ...]
    """The `what_it_measures` words an answer to this slot may give."""
    rule_ids: Mapping[str, str] | None = None
    """`rule`: the published rule for each `what_it_measures` (one id for every one: key `*`)."""
    setting: str | None = None
    """`job_value`: the reviewer's job setting (its `rules.md` key) the size is held against."""
    many: bool = False
    """Several answers on one item (one per part, per filler, per opening)."""
    against_words: str = ""
    """`string`, `vendor_equal`, `rule`: what the vendor's second group of marks is."""


#: How each slot question is answered. Keys are question keys of the file; nothing here is a
#: question of its own.
_HOW: Final[Mapping[str, _How]] = {
    "string_adds_up": _How(
        "string",
        ("overall_length", "overall_depth", "overall_height", "part_width", "part_height"),
        many=True,
        against_words="the printed overall of the same string",
    ),
    "run_overall_height": _How(
        "string", ("overall_height",), many=True, against_words="the printed overall height"
    ),
    "support_frame_or_legs": _How(
        "string",
        ("overall_length", "overall_height", "part_width", "part_height"),
        many=True,
        against_words="the printed overall of the support's string",
    ),
    "splash_length": _How(
        "vendor_equal",
        ("overall_length",),
        against_words="the top's run from wall to wall (parts allowed)",
    ),
    "ct_overall_length": _How(
        "rule",
        ("overall_length",),
        rule_ids={"*": "CT-WIDTH-001"},
        against_words="the run under the top: each piece, left to right",
    ),
    "sink_cutout_size": _How(
        "rule",
        ("cutout_width", "cutout_depth"),
        rule_ids={
            "cutout_width": "CT-SINK-CUTOUT-WIDTH-001",
            "cutout_depth": "CT-SINK-CUTOUT-DEPTH-001",
        },
    ),
    "sink_front_offset": _How(
        "rule", ("offset_from_front_edge",), rule_ids={"*": "CT-SINK-OFFSET-FRONT-001"}
    ),
    "ct_front_overhang": _How("job_value", ("overhang",), setting="countertop_overhang"),
    "ct_finished_depth": _How("architect", ("overall_depth",)),
    "ct_thickness": _How("architect", ("thickness",)),
    "ct_counter_height": _How("architect", ("height_above_floor", "overall_height")),
    "splash_height_and_thickness": _How("architect", ("overall_height", "thickness")),
    "run_wall_to_wall": _How("architect", ("overall_length",)),
    "cabinet_part_widths": _How("architect", ("part_width",), many=True),
    "cabinet_depth": _How("architect", ("overall_depth",)),
    "toe_kick": _How("architect", ("part_height", "height_above_floor")),
    "appliance_openings_sized": _How("architect", ("opening_width",), many=True),
    "filler_width": _How("architect", ("part_width",), many=True),
    "clear_width_at_an_accessible_item": _How(
        "architect", ("clear_width", "clear_height"), many=True
    ),
    "accessory_overall_size": _How(
        "architect", ("overall_length", "overall_depth", "thickness"), many=True
    ),
    "accessory_mounting_position": _How(
        "architect", ("height_above_floor", "position_along"), many=True
    ),
}

#: The code questions that are not slots, and why (in plain words).
NOT_SLOTS: Final[Mapping[str, str]] = {
    "inch_labels_match_the_millimetres": (
        "it checks a millimetre value against its inch figure, and millimetres never decide (Q12)"
    ),
    "parts_have_an_inch_figure": (
        "it asks whether a label prints an inch figure at all, a property code reads from each "
        "label itself; there is no size for the AI to point at"
    ),
    "same_value_across_sheets": (
        "it needs the item's other sheets, beyond one vendor view and its paired architect view"
    ),
    "mirror_twin_sizes": "it needs the other hand's sheets, beyond one vendor view",
    "item_number_matches_architect_tag": "its answer is a tag, not a size",
    "quantity_and_room_list_agree": "its answer is a count, not a size",
    "ct_end_overhang": (
        "the end overhang is the stone's end against the run's end, rarely printed as one "
        "dimension, and no job value has a GV default"
    ),
    "sink_back_offset": (
        "the published rule derives the back offset from the top's depth, the job's overhang and "
        "splash thickness; a printed back offset has no rule path, and ai_maths has none either"
    ),
    "sink_position_along_the_top": (
        "its first comparison is the stone sheet against the cabinet plan (two vendor sheets), "
        "measured from the same end"
    ),
    "sink_note_size_matches_the_sink": "its vendor value is a note's words, not a numbered mark",
    "faucet_and_accessory_holes": (
        "it compares hole sizes and positions across the item's other sheets and the spec sheet"
    ),
    "run_site_fit_share": (
        "it needs each part's kind (door cabinet, drawer bank, filler, opening) on both runs"
    ),
    "room_type_tags": "its answer is a tag, not a size",
    "detail_callout_ids": "its answer is a callout id, not a size",
    "item_counts_per_hand": "its answer is a count, not a size",
    "spec_sheet_values_used": "the spec sheet is another page, beyond one vendor view",
}


@dataclass(frozen=True, slots=True)
class Slot:
    """One fixed question asked as a slot."""

    key: str
    asks: str
    item_kinds: tuple[str, ...]
    compares: str
    """`vendor_only`, `gv_rule` or `architect`, as the question list says."""
    maths: Maths
    measures: tuple[str, ...]
    rule: str
    problem_when: str
    not_a_problem_when: tuple[str, ...]
    missing_fact_when: str
    rule_ids: Mapping[str, str]
    setting: str | None
    many: bool
    against_words: str

    @property
    def needs_architect(self) -> bool:
        """The answer names architect marks (an architect compare)."""
        return self.maths == "architect"

    @property
    def needs_against(self) -> bool:
        """The answer names a second group of vendor marks (`shop_against_mark_ids`)."""
        return self.maths in ("string", "vendor_equal") or (
            self.maths == "rule" and bool(self.against_words)
        )

    def rule_id(self, measures: str) -> str | None:
        """The published rule for an answer measuring `measures`, else `None`."""
        return self.rule_ids.get(measures) or self.rule_ids.get("*")

    def applies_to(self, kind: str | None) -> bool:
        return "*" in self.item_kinds or kind is None or kind in self.item_kinds


def _slot(question: Question, how: _How) -> Slot:
    return Slot(
        key=question.key,
        asks=question.asks,
        item_kinds=question.item_kinds,
        compares=question.compares,
        maths=how.maths,
        measures=how.measures,
        rule=question.rule,
        problem_when=question.problem_when,
        not_a_problem_when=question.not_a_problem_when,
        missing_fact_when=question.missing_fact_when,
        rule_ids=dict(how.rule_ids or {}),
        setting=how.setting,
        many=how.many,
        against_words=how.against_words,
    )


def catalogue(questions: Sequence[Question] | None = None) -> tuple[Slot, ...]:
    """The slots, in the question list's order: every code-decided question `_HOW` answers.

    A key in `_HOW` that the list does not hold, or holds as an AI question, raises
    `QuestionsError`: the slots never drift from the list."""
    asked = tuple(questions) if questions is not None else question_list.questions()
    by_key = {one.key: one for one in asked}
    for key in _HOW:
        found = by_key.get(key)
        if found is None:
            raise question_list.QuestionsError(f"slot {key!r} is not in the question list")
        if not found.can_go_red:
            raise question_list.QuestionsError(f"slot {key!r} is not a question code decides")
    return tuple(_slot(one, _HOW[one.key]) for one in asked if one.key in _HOW)


def slot(key: str) -> Slot | None:
    """One slot by its question key, or `None`."""
    return next((one for one in catalogue() if one.key == key), None)


def not_slots(questions: Sequence[Question] | None = None) -> dict[str, str]:
    """Every code-decided question that is not a slot, with the reason (`NOT_SLOTS`); a code
    question with no reason written gets "no reason written", which the tests refuse."""
    asked = tuple(questions) if questions is not None else question_list.questions()
    return {
        one.key: NOT_SLOTS.get(one.key, "no reason written")
        for one in asked
        if one.can_go_red and one.key not in _HOW
    }
