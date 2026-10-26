"""Drafting notes next to a reference size (final plan #9): made-up labels and phrases, no client
data.

A made-up reference page prints a 24" size (label D1, its box at x 100-120, y 50-60). Each test
writes a note beside it, or elsewhere on the sheet.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

import pytest

from app.review.assistant import dim_ends, drafting_notes, proof
from app.review.assistant.chat_trust import judged
from app.review.assistant.escalation import EscalationContext, escalate

VENDOR = "vendor.pdf"
REFERENCE = "reference.pdf"
BOX = [100.0, 50.0, 120.0, 60.0]


def _label(text: str = '24"', flags: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "id": "D1",
        "text": text,
        "inches": "24",
        "box": list(BOX),
        "side": "architect",
        "orientation": "h",
        "route": "text",
        "flags": list(flags),
    }


def _phrase(text: str, box: list[float]) -> dict[str, Any]:
    return {"text": text, "box": box}


# ---- reading the note --------------------------------------------------------------------------


def test_the_label_reader_s_own_flags_are_read() -> None:
    assert drafting_notes.read_note(_label(flags=("vif",)), []) == ["field_verify"]
    assert drafting_notes.read_note(_label(flags=("approx",)), []) == ["approx"]


def test_a_note_in_the_label_s_own_text_is_read() -> None:
    assert drafting_notes.read_note(_label('24" (V.I.F)'), []) == ["field_verify"]
    assert drafting_notes.read_note(_label('HOLD 24"'), []) == ["hold"]


def test_a_note_alone_beside_the_label_on_its_line_is_read() -> None:
    beside = _phrase("HOLD", [123.0, 50.0, 140.0, 60.0])
    assert drafting_notes.read_note(_label(), [beside]) == ["hold"]
    under = _phrase("(V.I.F.)", [100.0, 62.0, 120.0, 70.0])
    assert drafting_notes.read_note(_label(), [under]) == ["field_verify"]


def test_the_size_s_own_phrase_with_its_note_is_read() -> None:
    own = _phrase('24" (610 mm) +/- OVERALL', [100.0, 50.0, 190.0, 60.0])
    assert drafting_notes.read_note(_label(), [own]) == ["approx"]


def test_a_general_note_is_never_one_size_s_note() -> None:
    general = _phrase("ALL SIZES VIF BEFORE FABRICATION BY OTHERS", [123.0, 50.0, 300.0, 60.0])
    door = _phrase("DOOR HOLD OPEN MAGNET", [123.0, 50.0, 260.0, 60.0])
    sentence = _phrase('24" HOLD THE TOP BACK FROM THE WINDOW SILL', [100.0, 50.0, 300.0, 60.0])
    assert drafting_notes.read_note(_label(), [general, door, sentence]) == []


def test_a_note_far_from_the_label_is_never_read() -> None:
    far = _phrase("HOLD", [300.0, 200.0, 320.0, 210.0])
    assert drafting_notes.read_note(_label(), [far]) == []


# ---- what a note does ----------------------------------------------------------------------------


def _finding(
    vendor: str = "25", architect: str = "24", status: str = "different"
) -> dict[str, Any]:
    return {
        "id": "F1",
        "page": 1,
        "vs_page": 1,
        "item": "Vanity: top width",
        "vendor": f'{vendor}"',
        "architect": f'{architect}"',
        "status": status,
        "source": "architect",
        "category": "countertop",
        "created_by": "helper:1",
        "paired_by": "code",
        "inches": {"vendor": vendor, "architect": architect},
        "checks": {
            "printed_vendor": "match",
            "printed_architect": "match",
            "ledger": "agrees",
        },
        "dims": {
            "vendor": {"id": "D1", "page": 1, "file": VENDOR, "inches": vendor},
            "architect": {"id": "D1", "page": 1, "file": REFERENCE, "inches": architect},
        },
    }


class _Page:
    def __init__(self, low: set[str], high: set[str]) -> None:
        self._ends = dim_ends.Ends(
            dim_ends.Span("D1", "h", 100.0, 0.0, 96.0, Fraction(24)),
            dim_ends.End(0.0, frozenset(low)),
            dim_ends.End(96.0, frozenset(high)),
        )

    def ends(self, _label_id: object) -> dim_ends.Ends:
        return self._ends


def _escalated(
    finding: dict[str, Any],
    label: dict[str, Any],
    phrases: list[dict[str, Any]] | None = None,
    ends: tuple[set[str], set[str]] | None = None,
) -> dict[str, Any]:
    page = _Page(*ends) if ends is not None else None
    ctx = EscalationContext(
        main_file=VENDOR,
        labels={(REFERENCE, 1): [label]},
        phrases={(REFERENCE, 1): phrases or []},
        pages=(lambda file, _p: page if file == REFERENCE else None) if page else None,
    )
    result = escalate([finding], ctx)
    changed = {str(f["id"]): f for f in result.changed}
    return judged(changed.get("F1", finding))


def test_a_difference_against_a_vif_size_is_a_field_verify_note_never_red() -> None:
    found = _escalated(_finding(), _label(flags=("vif",)))
    assert found[drafting_notes.FIELD]["kinds"] == ["field_verify"]
    assert proof.proof_group(found) == "not_compared"
    assert not proof.code_proved(found)
    classified = proof.classified(found)
    assert classified.reason == "field_verify"
    assert str(classified.why).startswith('field verify: the reference marks its 24" V.I.F.')


def test_a_plus_or_minus_size_is_softened_too() -> None:
    found = _escalated(_finding(), _label(flags=("approx",)))
    assert proof.proof_group(found) == "not_compared"
    assert proof.classified(found).reason == "field_verify"


def test_half_an_inch_to_a_wall_s_face_is_the_drafting_convention() -> None:
    found = _escalated(_finding("24.375"), _label(), ends=({"wall"}, {"cabinet_face"}))
    assert found[drafting_notes.FIELD]["kinds"] == ["convention"]
    assert proof.proof_group(found) == "not_compared"
    why = str(proof.classified(found).why)
    assert why.startswith("convention difference, field verify") and 'by 3/8"' in why


def test_more_than_half_an_inch_or_no_wall_is_no_convention() -> None:
    over = _escalated(_finding("24.75"), _label(), ends=({"wall"}, {"cabinet_face"}))
    assert drafting_notes.FIELD not in over
    no_wall = _escalated(_finding("24.25"), _label(), ends=({"end_panel"}, {"cabinet_face"}))
    assert drafting_notes.FIELD not in no_wall


def test_a_reference_size_finer_than_half_an_inch_is_no_rounding() -> None:
    label = dict(_label('24 3/16"'), inches="24.1875")
    found = _escalated(
        _finding("24.5", architect="24.1875"), label, ends=({"wall"}, {"cabinet_face"})
    )
    assert drafting_notes.FIELD not in found


def test_a_hold_size_is_never_softened_and_says_so() -> None:
    beside = _phrase("HOLD", [123.0, 50.0, 140.0, 60.0])
    found = _escalated(
        _finding("24.25"), _label(), phrases=[beside], ends=({"wall"}, {"cabinet_face"})
    )
    assert found[drafting_notes.FIELD]["kinds"] == ["hold"]
    assert drafting_notes.softened(found) is None and drafting_notes.hold(found)
    likely = dict(found, whole_look={"proof": "likely_only", "words": "Please check: the top."})
    assert "marks this size HOLD" in proof.likely_words(likely)


def test_a_right_keeps_its_outcome() -> None:
    found = _escalated(_finding("24", status="same"), _label(flags=("vif",)))
    assert proof.proof_group(found) == "right"


def test_a_note_read_for_another_label_is_stale() -> None:
    found = _escalated(_finding(), _label(flags=("vif",)))
    moved = dict(found)
    moved["dims"] = {**found["dims"], "architect": {"id": "D9", "page": 1, "file": REFERENCE}}
    assert drafting_notes.softened(moved) is None


@pytest.mark.usefixtures("read_twice")
def test_without_a_note_the_same_difference_is_code_proved() -> None:
    # The softening is what keeps it from red: the same item with no note is code-proved.
    found = _escalated(_finding(), _label())
    assert drafting_notes.FIELD not in found
    assert proof.code_proved(found)
