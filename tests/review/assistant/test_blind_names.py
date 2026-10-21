"""#1833: a blind question never carries a value inside the helper's item name.

The blind pick (the devil's advocate's, also the escalation's) is shown the two crops and the label
ids only. A helper writes the item's name and may put the value it claimed in it; told that number,
the pick could name the label that prints it because it was told. Code takes every number and
written dimension out of the name before it goes in; code still has the values and the original
name. The kept picks' key changes with the cleaned question, so a pick asked with a value in the
name is never reused.

Every drawing value and name here is made up.
"""

from __future__ import annotations

import inspect
import json
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.review.assistant import full_check_roster, pick_cache
from app.review.assistant.chat_request import BY_SHORT
from app.review.assistant.full_check_advocate import (
    ADVOCATE_SCHEMA,
    ADVOCATE_SYSTEM,
    NAME_WORDS,
    advocate_question,
    neutral_name,
    room_end,
)
from app.review.assistant.simple_check import blind
from app.review.assistant.untrusted import fence

_DIGITS = re.compile(r"[\d½¼¾⅛⅜⅝⅞]")


@pytest.mark.parametrize(
    ("name", "neutral"),
    [
        ("outlet spacing 13'-7 5/16\"", "outlet spacing"),
        ("Pantry: floor to underside of upper 11′-13⁄16″", "Pantry: floor to underside of upper"),
        ("Pantry: floor to underside of upper 17′-11″", "Pantry: floor to underside of upper"),
        ('Island-9: overall height 2861 mm [38 7/8"]', "Island: overall height"),
        ("Island top depth 2861mm (38 7/8 in)", "Island top depth"),
        ('Bench seat: segment 7 width (11/32")', "Bench seat: segment width"),
        ('Bench apron 0.293" +/- 1/64"', "Bench apron"),
        ("Bench back 13 ft 11 in", "Bench back"),
        ("Bench back 3 1/16 inches tall", "Bench back tall"),
        ("Locker run 19 x 37 in.", "Locker run"),
        ('Locker reveal ⅞"', "Locker reveal"),
        ("Locker width 2,861mm", "Locker width"),
        ("the 3rd shelf from the top", "the shelf from the top"),
        ("Locker J-box height", "Locker J-box height"),
        ("vendor's outlet height", "vendor's outlet height"),
        ("", ""),
        ('97 3/16"', ""),
    ],
)
def test_the_name_loses_every_number_and_keeps_its_words(name: str, neutral: str) -> None:
    cleaned = neutral_name(name)
    assert cleaned == neutral
    assert not _DIGITS.search(cleaned)


def test_the_advocate_question_carries_no_value_from_the_name() -> None:
    question = advocate_question("D4", "outlet spacing 13'-7 5/16\"")
    assert "13" not in question and "5/16" not in question
    assert fence("outlet spacing", "item") in question  # the neutral words, still fenced as data
    assert NAME_WORDS in question


def _engine(sent: list[list[dict[str, Any]]], pack: Path | None = None) -> SimpleNamespace:
    def zoom(raw: Any, step: Any, who: str) -> SimpleNamespace:
        return SimpleNamespace(
            content=[
                {"type": "image", "source": {"type": "base64", "data": ""}},
                {"type": "text", "text": "D1 D2 D3"},
            ]
        )

    def call(**given: Any) -> SimpleNamespace:
        sent.append(given["messages"][0]["content"])
        reply = {
            "architect_pick": "D7",
            "vendor_spans": {"from": "floor", "to": "top_of_counter"},
            "architect_spans": {"from": "floor", "to": "top_of_counter"},
            "vendor_ink_black": "yes",
        }
        return SimpleNamespace(text=lambda: json.dumps(reply))

    return SimpleNamespace(
        _price=lambda model, prompt, out: 100,
        _call=call,
        spent=0,
        cancel=threading.Event(),
        provider=SimpleNamespace(name="anthropic"),
        book=SimpleNamespace(
            dim_labels=SimpleNamespace(on_page=lambda file, page_no, read_now=False: [_ARCHITECT])
        ),
        tools=SimpleNamespace(zoom=zoom, workspace=SimpleNamespace(set_pack=pack)),
    )


_VENDOR = {
    "id": "D4",
    "page": 2,
    "file": "shop.pdf",
    "text": "13'-7 5/16\"",
    "box": [10, 10, 30, 20],
}
_ARCHITECT = {"id": "D7", "page": 1, "file": "shop.pdf", "text": "13'-9\"", "box": [50, 10, 80, 20]}
_FINDING = {"id": "F1", "item": "Bench outlet spacing 13'-9\" (architect)"}


def test_the_pick_sent_to_the_model_carries_no_value_from_the_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(full_check_roster._Run, "_phase_room", lambda self, phase, money: True)
    run = full_check_roster._Run(engine=_engine(sent))  # type: ignore[arg-type]
    answer = run._blind_pick(_FINDING, _VENDOR, _ARCHITECT, None, strong=True)
    assert answer is not None and answer["pick"] == "p1.D7"
    question = str(sent[0][-1]["text"])  # the question: the last block, after both crops
    assert "13'-9" not in question and "13" not in question
    assert "Bench outlet spacing (architect)" in question
    # Code's own use of the name keeps the original (the room-end rule reads it).
    assert room_end(item="upper to soffit 13'-9\"") == "the soffit"


def _old_prompt() -> str:
    """The pick's fixed prompt as #1829 keyed it, before the name was cleaned."""
    from app.review.assistant.chat_engine import HARNESS_ID

    old_question = (
        "The first picture is the vendor's drawing; the dimension marked there is D0, "
        f"for this item:\n{fence('', 'item')}\nThe second picture is the architect's drawing with "
        "every printed dimension numbered. Which numbered dimension measures the same thing as "
        "the marked one? Answer none if none does. Then, for the marked one and the one you "
        "picked, say where each end lands (top_of_counter, underside_of_counter, top_of_splash, "
        "floor, wall_face, cabinet_side, cabinet_end, centre_line, box_above_unit, blocking, "
        "other)."
    )
    return "\n".join(
        (HARNESS_ID, ADVOCATE_SYSTEM, json.dumps(ADVOCATE_SCHEMA, sort_keys=True), old_question)
    )


def test_a_pick_kept_from_the_old_question_is_asked_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert NAME_WORDS in full_check_roster.pick_prompt()
    assert full_check_roster.pick_prompt() != _old_prompt()
    sent: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(full_check_roster._Run, "_phase_room", lambda self, phase, money: True)
    run = full_check_roster._Run(engine=_engine(sent, tmp_path))  # type: ignore[arg-type]
    old_key = pick_cache.pick_key(
        vendor=_VENDOR,
        architect_page=[_ARCHITECT],
        crops=[run._half_box(None, _VENDOR), run._half_box(None, _ARCHITECT)],
        model=BY_SHORT["opus"].id,
        effort="high",
        prompt=_old_prompt(),
    )
    kept = {"architect_pick": "D7", "pick": "p1.D7", "note": ""}
    pick_cache.store(tmp_path, old_key, kept, model="opus", effort="high")
    answer = run._blind_pick(_FINDING, _VENDOR, _ARCHITECT, None, strong=True)
    assert len(sent) == 1  # asked again: the old pick was not reused
    assert answer is not None and pick_cache.CACHED not in answer


def test_the_simple_checks_blind_question_has_no_place_for_a_name() -> None:
    """The simple check's blind look is asked with code's label ids and the files' kinds only:
    no helper-written words can reach it."""
    params = list(inspect.signature(blind.question).parameters)
    assert params == ["kind", "vendor_ref", "architect_ref", "sheets"]
