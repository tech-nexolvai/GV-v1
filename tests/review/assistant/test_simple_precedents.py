"""The "looks wrong but isn't" precedents and the blind look's evidence-first form (#1775, recipe
step 6). $0, no model call."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.review.assistant.simple_check import blind, precedents, prompts
from app.review.assistant.simple_check.precedents import Precedent

#: Words that tell the model to leave something out rather than say what is not the same thing.
_LEAVE_OUT = ("ignore", "is a gv note", "not a vendor error", "do not compare", "skip", "drop")


def test_every_precedent_says_what_is_not_the_same_never_what_to_leave_out() -> None:
    for one in precedents.precedents():
        rule = one.rule.lower()
        assert re.search(r"\bnot\b|\bnever\b", rule), one.id
        assert not any(words in rule for words in _LEAVE_OUT), one.id
        # The one "never report" is a sum the model made itself, and it says printed numbers
        # are still compared.
        if "never report" in rule:
            assert "never report a sum you made" in rule and "still compared" in rule, one.id
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


def test_a_prototype_reference_items_prompt_does_not_contradict_itself() -> None:
    """R1 (#1799): with no architect elevation the brand prototype IS the reference, and the item
    text says so; P5 keeps that exception in the same words."""
    from tests.review.assistant.test_prompt_fences import _item

    item = _item().model_copy(update={"prototype_reference": True})
    text = prompts.item_text(item, [])
    assert (
        "the architect gives no elevation of this area: the brand prototype is the reference"
        in (text.lower())
    )
    p5 = next(p.rule for p in precedents.precedents() if p.id == "P5").lower()
    assert "except where code's text says the architect gives no elevation" in p5
    assert "the brand prototype is the reference" in p5
    system = prompts.system_prompt("- a rule", "Cabinets", products=("cabinet",)).lower()
    assert "neither is the architect's dimension" not in system


def test_millwork_wall_panels_and_printed_numbers_stay_compared() -> None:
    p1 = next(p.rule for p in precedents.precedents() if p.id == "P1")
    assert "Millwork wall panels are millwork: compare them" in p1
    assert "panel or covering above the unit" not in p1
    p6 = next(p.rule for p in precedents.precedents() if p.id == "P6")
    assert "report the printed labels only" not in p6
    assert "Every printed number, with or without a tag, is still compared" in p6


def test_a_chain_look_is_not_told_that_sums_are_not_values() -> None:
    """The chain look picks a string of parts code adds up: P6 is left out of its prompt."""
    assert "P6." in blind.blind_system(("cabinet",))
    chain = blind.blind_system(("cabinet",), kind="chain")
    assert "P6." not in chain and "P5." in chain


@pytest.mark.parametrize("value", [True, False])
def test_the_precedents_switch_reaches_the_compare_call_and_the_blind_look(
    value: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One setting, `assistant_simple_precedents`: the compare call's system prompt and the blind
    look's follow it, through the chat's options (`_simple_options` -> `SimpleOptions.of`)."""
    from app.api.assistant_chats import _simple_options
    from app.review.assistant.chat_contract import ChatMessageIn
    from app.review.assistant.simple_check import compare as compare_module
    from tests.review.assistant.test_simple_run_stop import _answer, _Pictures, _Run

    base = {
        "assistant_full_check_mode": "simple",
        "assistant_simple_compare_model": "claude-sonnet-5-5",
        "assistant_simple_compare_effort": "high",
        "assistant_simple_hard_model": "claude-opus-5-5",
        "assistant_simple_hard_effort": "high",
        "assistant_simple_parallel": 8,
        "assistant_simple_max_tokens": 64000,
        "assistant_simple_retries": 3,
        "assistant_simple_blind_model": "claude-opus-5-5",
        "assistant_simple_blind_effort": "high",
        "assistant_simple_resolve": "off",
        "assistant_simple_resolve_rounds": 1,
        "assistant_simple_resolve_model": "claude-sonnet-5-5",
        "assistant_simple_resolve_effort": "medium",
        "assistant_simple_resolve_web_search": False,
    }
    body = ChatMessageIn.model_validate(
        {"model": "claude-opus-5-5", "effort": "high", "thinking": True, "mode": "full_check"}
    )
    settings = SimpleNamespace(**base, assistant_simple_precedents=value)
    plain = _simple_options(settings, body)  # type: ignore[arg-type]
    assert plain is not None
    check = _Run(tmp_path / str(value), plain)
    systems: dict[str, list[str]] = {"compare": [], "blind": []}

    def compare_all(items: Any, *, source: Any, system: str, on_outcome: Any, **_: Any) -> Any:
        systems["compare"].append(system)
        done = []
        for item in items:
            source(item)
            outcome = _answer(item)
            on_outcome(outcome)
            done.append(outcome)
        return done

    def call(**kwargs: Any) -> Any:
        systems["blind"].append(kwargs["system"])
        raise RuntimeError("no model in a test")

    monkeypatch.setattr(compare_module, "compare_all", compare_all)
    monkeypatch.setattr(compare_module, "PictureSource", _Pictures)
    monkeypatch.setattr(blind, "pictures", lambda *_a, **_k: [{"type": "text", "text": "x"}])
    monkeypatch.setattr(check.engine, "_call", call)
    check.engine.run()
    assert systems["compare"] and systems["blind"], systems
    for kind in ("compare", "blind"):
        for system in systems[kind]:
            assert ("Looks wrong but isn't" in system) is value, (kind, value)


def test_views_differ_is_not_a_default_different_points() -> None:
    """P7 says a depth is not a height, never that two views differ by default: a width on a plan
    and on an elevation between the same edges is still compared."""
    p7 = next(p.rule for p in precedents.precedents() if p.id == "P7")
    assert "by default" not in p7 and "are compared then" in p7


def test_a_prototype_reference_look_is_not_told_the_prototype_is_reference_only() -> None:
    assert "P5." in blind.blind_system(("cabinet",))
    reference = blind.blind_system(("cabinet",), prototype_reference=True)
    assert "P5." not in reference and "P4." in reference
