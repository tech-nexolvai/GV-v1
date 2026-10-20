"""The "looks wrong but isn't" precedents and the blind look's evidence-first form (#1775, recipe
step 6). $0, no model call."""

from __future__ import annotations

import re

from app.review.assistant.simple_check import blind, precedents, prompts
from app.review.assistant.simple_check.precedents import Precedent


def test_every_precedent_says_what_is_not_the_same_never_what_to_leave_out() -> None:
    for one in precedents.precedents():
        assert re.search(r"\bnot\b|\bnever\b", one.rule), one.id
        assert "never report" not in one.rule.lower() and "ignore" not in one.rule.lower()
        # Generic words only: no printed values, no page or drawing numbers (no client values).
        assert not re.search(r"\d", one.rule), one.id
    assert len({p.id for p in precedents.precedents()}) == len(precedents.precedents())


def test_products_pick_their_own_precedents() -> None:
    lighting = {p.id for p in precedents.precedents(("lighting",))}
    cabinets = {p.id for p in precedents.precedents(("cabinet",))}
    assert "C1" not in lighting and "P1" in lighting
    assert {"C1", "C2", "C3"} <= cabinets
    # A whole-set check (no scope) gets every product's.
    assert {"C1", "C3"} <= {p.id for p in precedents.precedents()}


def test_a_product_pack_can_add_its_own() -> None:
    def seating(products: tuple[str, ...]) -> list[Precedent]:
        if "seating" not in products:
            return []
        return [Precedent("S1", "A seat height is not the height of the seat back.", "seating")]

    precedents.add_source(seating)
    try:
        assert "S1" in {p.id for p in precedents.precedents(("seating",))}
        assert "S1" not in {p.id for p in precedents.precedents(("cabinet",))}
    finally:
        precedents._SOURCES.remove(seating)


def test_compare_and_blind_prompts_carry_them_behind_a_switch() -> None:
    on = prompts.system_prompt("- a rule", "Cabinets", products=("cabinet",))
    off = prompts.system_prompt("- a rule", "Cabinets", products=("cabinet",), precedents=False)
    assert "Looks wrong but isn't" in on and "C3." in on
    assert "Looks wrong but isn't" not in off
    # The same bytes for every drawing of a run (cached).
    assert on == prompts.system_prompt("- a rule", "Cabinets", products=("cabinet",))
    assert "P4." in blind.blind_system(("countertop",))
    assert blind.blind_system(("countertop",), precedents=False) == blind.BLIND_SYSTEM


def test_the_blind_form_puts_the_evidence_before_the_verdict() -> None:
    order = list(blind.BLIND_SCHEMA["properties"])
    assert order.index("vendor_from") < order.index("pick") < order.index("verdict")
    assert max(order.index(e) for e in ("vendor_to", "architect_from", "architect_to")) < (
        order.index("verdict")
    )
    assert blind.BLIND_SCHEMA["required"] == order
    for kind in ("pair", "chain", "values", "absence"):
        text = blind.question(kind, "p3.D1", "p2.D1")
        assert "Only then the verdict" in text
        assert text.index("Only then the verdict") > text.index("architect_from")
