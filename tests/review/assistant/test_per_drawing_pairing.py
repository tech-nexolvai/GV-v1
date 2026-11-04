"""Pairing per drawing, not per sheet (rebuild plan Step 3, 2026-11-05).

An architect sheet often holds two room types (a standard and an accessible kitchen, or a studio's
and a one-bedroom's), so pairing a vendor drawing with the whole sheet blurred them. These tests
hold the pieces the per-drawing pairing rests on. Invented sets and made-up names only: $0, no
model call.
"""

from __future__ import annotations

from app.review.assistant import set_pack
from app.review.assistant.simple_check import pairing, views

# ---- the words code reads ------------------------------------------------------------------------


def test_a_sheet_number_with_a_letter_after_it_is_a_sheet_number() -> None:
    assert pairing.references_in(["SEE SHEET Q-401A"]) == {"Q401A"}
    assert pairing.references_in(["2/Q-401A"]) == {"2/Q401A", "Q401A"}
    assert pairing.references_in(["Q7.10B"]) == {"Q7.10B"}
    assert set_pack._sheet_numbers("SHEET NO. A-401A") == ["A-401A"]
    assert set_pack._sheet_numbers("A-401A") == ["A-401A"]


def test_a_keynote_code_is_not_a_sheet_number() -> None:
    assert set_pack._sheet_numbers("KEYNOTE D-388 SEE M500") == []
    assert set_pack._sheet_numbers("A-501 and MW-101") == ["A-501", "MW-101"]
    # A pack made before the page map left them out: pairing drops them when it reads the pack.
    assert views.keynote_code("D-388") and views.keynote_code("M500")
    assert not views.keynote_code("A-501") and not views.keynote_code("MW-101")


def test_acc_and_a_misspelt_accessible_mark_the_accessible_drawing() -> None:
    assert pairing.markers_in(["ACC KITCHEN PLAN"])[0] == {"ACC"}
    assert pairing.markers_in(["ACC. KITCHEN ELEVATION"])[0] == {"ACC"}
    assert pairing.markers_in(["ACCESIBLE VANITY ELEVATION"])[0] == {"ACCESIBLE"}
    # An item's accessories after its tag, and other words starting ACC, are no marker.
    assert pairing.markers_in(["XX-213-ACC", "ACCESSORIES", "ACC-1"])[0] == frozenset()
