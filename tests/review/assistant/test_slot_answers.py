"""A slot answer is marks, never numbers; the marks are shuffled per call and map back.

Mirrors `app/review/assistant/slot_answers.py`. Made-up labels only.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from app.review.assistant import slots
from app.review.assistant.slot_answers import AnswerRefused, MarkMap, parse, schema

V = "vendor.pdf"
A = "architect.pdf"


def _labels(count: int = 6) -> list[tuple[Any, str, int, Mapping[str, Any]]]:
    out: list[tuple[Any, str, int, Mapping[str, Any]]] = []
    for n in range(1, count + 1):
        out.append(("vendor", V, 2, {"id": f"D{n}", "text": f'{n}"', "inches": str(n)}))
        out.append(("architect", A, 5, {"id": f"D{n}", "text": f'{n}"', "inches": str(n)}))
    return out


def _slot(key: str) -> slots.Slot:
    found = slots.slot(key)
    assert found is not None
    return found


def _walk(node: object) -> Iterator[Mapping[str, Any]]:
    if isinstance(node, Mapping):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_the_reply_shape_holds_no_number_field() -> None:
    marks = MarkMap(_labels(), "call-1")
    shape = schema(slots.catalogue(), marks)
    types = {str(node.get("type")) for node in _walk(shape) if "type" in node}
    assert "number" not in types and "integer" not in types
    enums = [node["enum"] for node in _walk(shape) if "enum" in node]
    assert any(set(e) == set(marks.marks("vendor")) for e in enums)
    assert any(set(e) == set(marks.marks("architect")) for e in enums)


def _reply(shop: list[Any], arch: list[Any] | None = None, **extra: Any) -> dict[str, Any]:
    answer = {
        "what_it_measures": "thickness",
        "shop_mark_ids": shop,
        "shop_against_mark_ids": [],
        "arch_mark_ids": arch or [],
        "ends": {"left": "not_asked", "right": "not_asked"},
        **extra,
    }
    return {
        "slots": [
            {
                "slot": "ct_thickness",
                "status": "answered",
                "not_shown_reason": "none",
                "answers": [answer],
            }
        ]
    }


def test_a_typed_number_is_refused() -> None:
    marks = MarkMap(_labels(), "call-1")
    asked = [_slot("ct_thickness")]
    typed = parse(_reply(['1 1/4"']), asked, marks)
    assert typed[0].status == "not_shown" and "typed value is refused" in typed[0].refused[0]
    field = parse(_reply([marks.marks("vendor")[0]], value='1 1/4"'), asked, marks)
    assert field[0].status == "not_shown" and "unknown field 'value'" in field[0].refused[0]
    bare = parse(_reply([36]), asked, marks)
    assert bare[0].status == "not_shown"


def test_an_unknown_mark_is_refused() -> None:
    marks = MarkMap(_labels(), "call-1")
    asked = [_slot("ct_thickness")]
    unknown = parse(_reply(["M999"]), asked, marks)
    assert unknown[0].status == "not_shown"
    assert "not a mark on these pictures" in unknown[0].refused[0]
    wrong_side = parse(_reply([marks.marks("architect")[0]]), asked, marks)
    assert "architect's picture" in wrong_side[0].refused[0]


def test_a_good_answer_keeps_its_marks() -> None:
    marks = MarkMap(_labels(), "call-1")
    v, a = marks.marks("vendor")[0], marks.marks("architect")[0]
    found = parse(_reply([v], [a]), [_slot("ct_thickness")], marks)
    assert found[0].status == "answered"
    answer = found[0].answers[0]
    assert answer.shop[0].mark == v and answer.architect[0].mark == a
    assert answer.shop[0].label_id == marks.label_of(v).label_id


def test_the_reply_itself_must_follow_the_shape() -> None:
    marks = MarkMap(_labels(), "call-1")
    with pytest.raises(AnswerRefused):
        parse({"slots": [], "summary": "36 inches"}, [_slot("ct_thickness")], marks)
    with pytest.raises(AnswerRefused):
        parse({"slots": [{"slot": "not_a_slot", "status": "answered"}]}, [], marks)


def test_not_shown_takes_a_fixed_reason() -> None:
    marks = MarkMap(_labels(), "call-1")
    asked = [_slot("ct_thickness")]
    entry = {"slot": "ct_thickness", "status": "not_shown", "answers": []}
    found = parse(
        {"slots": [{**entry, "not_shown_reason": "printed_without_a_mark"}]}, asked, marks
    )
    assert found[0].reason == "printed_without_a_mark"
    with pytest.raises(AnswerRefused):
        parse({"slots": [{**entry, "not_shown_reason": "it is 36 inches"}]}, asked, marks)


def test_marks_are_shuffled_per_call_and_map_back() -> None:
    labels = _labels(12)
    first, again, other = MarkMap(labels, "call-1"), MarkMap(labels, "call-1"), MarkMap(labels, "x")
    assert first.as_dict() == again.as_dict()
    assert first.as_dict() != other.as_dict()
    for mark in first.marks():
        ref = first.label_of(mark)
        assert first.mark_of(ref.file, ref.page, ref.label_id) == mark
    in_order = [first.mark_of(V, 2, f"D{n}") for n in range(1, 13)]
    assert in_order != sorted(in_order, key=lambda m: int(str(m)[1:]))


def test_one_answer_unless_the_slot_takes_many() -> None:
    marks = MarkMap(_labels(), "call-1")
    v = marks.marks("vendor")
    reply = _reply([v[0]])
    reply["slots"][0]["answers"].append(dict(reply["slots"][0]["answers"][0], shop_mark_ids=[v[1]]))
    assert len(parse(reply, [_slot("ct_thickness")], marks)[0].answers) == 1
    reply["slots"][0]["slot"] = "filler_width"
    for answer in reply["slots"][0]["answers"]:
        answer["what_it_measures"] = "part_width"
    assert len(parse(reply, [_slot("filler_width")], marks)[0].answers) == 2
