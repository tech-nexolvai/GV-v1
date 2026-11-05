"""The slot catalogue is built from the question list, never from a second list.

Mirrors `app/review/assistant/slots.py`: every slot is a code-decided question of
`vocabulary/review_questions.yaml`; every code question that is not a slot says why; a slot naming
a question the list does not hold, or holds as an AI question, is refused; every GV-rule slot names
a rule the published rulebook holds; the switch is off by default.

Made-up questions only where the list itself is replaced.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.review.assistant import question_list, slot_rules, slots


def test_slots_come_from_the_code_questions_of_the_list() -> None:
    made = slots.catalogue()
    code = {one.key for one in question_list.code_questions()}
    keys = {one.key for one in made}
    assert keys <= code
    assert keys.isdisjoint(slots.not_slots())
    assert keys | set(slots.not_slots()) == code
    assert "no reason written" not in slots.not_slots().values()


def test_each_slot_carries_its_question_rule_and_anti_patterns() -> None:
    for made in slots.catalogue():
        asked = question_list.question(made.key)
        assert asked is not None
        assert made.asks == asked.asks
        assert made.rule == asked.rule
        assert made.not_a_problem_when == asked.not_a_problem_when
        assert made.compares == asked.compares
        assert made.measures and set(made.measures) <= set(slots.MEASURES)


def test_every_rule_slot_names_a_published_rule() -> None:
    for made in slots.catalogue():
        if made.maths != "rule":
            continue
        for measures in made.measures:
            rule_id = made.rule_id(measures)
            assert rule_id is not None
            assert slot_rules.snapshot(rule_id) is not None, rule_id


def test_a_slot_the_list_does_not_hold_is_refused() -> None:
    asked = [one for one in question_list.questions() if one.key != "ct_thickness"]
    with pytest.raises(question_list.QuestionsError, match="ct_thickness"):
        slots.catalogue(asked)


def test_a_slot_on_an_ai_question_is_refused() -> None:
    asked = [
        replace(one, decided_by="ai") if one.key == "ct_thickness" else one
        for one in question_list.questions()
    ]
    with pytest.raises(question_list.QuestionsError, match="not a question code decides"):
        slots.catalogue(asked)


def test_the_switch_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(slots.SLOTS_ENV, raising=False)
    assert not slots.slots_on()
    monkeypatch.setenv(slots.SLOTS_ENV, "on")
    assert slots.slots_on()
    monkeypatch.setenv(slots.SLOTS_ENV, "anything else")
    assert not slots.slots_on()
