"""The simple full check's shapes (`simple_check/schema.py`): the structured-output schema stays
inside Anthropic's limits, and code's checks catch every gap in an answer. Made-up values only."""

from __future__ import annotations

from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.review.assistant.simple_check.schema import (
    REPLY_SCHEMA,
    CompareResult,
    Drawing,
    Label,
    OtherItem,
    Pair,
    Reply,
    WorkItem,
    canonical_ref,
    check_result,
    end_word,
    label_ref,
    parse_label_ref,
)

_UNSUPPORTED = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
}


def _walk(node: Any, path: str = "$") -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    if isinstance(node, dict):
        found.append((path, node))
        for key, value in node.items():
            found += _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for n, value in enumerate(node):
            found += _walk(value, f"{path}[{n}]")
    return found


def test_reply_schema_keeps_to_the_structured_output_limits() -> None:
    unions = 0
    for path, node in _walk(REPLY_SCHEMA):
        assert not (_UNSUPPORTED & node.keys()), path
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, path
            assert sorted(node["required"]) == sorted(node["properties"]), path
        if "anyOf" in node or isinstance(node.get("type"), list):
            unions += 1
        if "minItems" in node:
            assert node["minItems"] in (0, 1), path
    # Anthropic: at most 16 union-typed parameters across a request's strict schemas.
    assert 0 < unions <= 16
    jsonschema.Draft202012Validator.check_schema(REPLY_SCHEMA)


def _reply() -> dict[str, Any]:
    return {
        "work_item": "p7.v1",
        "pairs": [
            {
                "vendor_label": "p7.D3",
                "architect_label": "p6.D1",
                "measures": "overall width",
                "vendor_from": "wall_face (left)",
                "vendor_to": "wall_face (right)",
                "architect_from": "wall_face (left)",
                "architect_to": "wall_face (right)",
                "verdict": "Same_Points",
                "weak_point": "None",
                "note": "",
            }
        ],
        "other_items": [
            {
                "kind": "MISSING_ON_VENDOR",
                "what": "outlet in splash",
                "vendor": None,
                "architect": "outlet",
                "vendor_image": None,
                "vendor_box_px": None,
                "architect_image": 2,
                "architect_box_px": [10, 10, 40, 40],
                "verdict": "different",
                "millwork": True,
            }
        ],
        "unaccounted_labels": ["p7.D4"],
    }


def test_the_reply_model_reads_what_the_schema_allows_and_lower_cases_enums() -> None:
    document = _reply()
    jsonschema.validate(Reply.model_validate(document).model_dump(mode="json"), REPLY_SCHEMA)
    # The API does not guarantee an enum's case: code reads "Same_Points" as "same_points".
    reply = Reply.model_validate(document)
    assert reply.pairs[0].verdict == "same_points" and reply.pairs[0].weak_point == "none"
    assert reply.other_items[0].kind == "missing_on_vendor"
    broken = {**document, "extra": 1}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(broken, REPLY_SCHEMA)
    with pytest.raises(ValidationError):
        Reply.model_validate(broken)


def test_label_ids() -> None:
    assert label_ref(23, "D17") == "p23.D17"
    assert parse_label_ref("p23.D17") == (23, "D17")
    with pytest.raises(ValueError):
        parse_label_ref("D17")
    assert canonical_ref("P23 D17") == "p23.D17"
    assert canonical_ref("p.23:d17") == "p23.D17"
    assert canonical_ref("D17", 5) == "p5.D17"
    assert canonical_ref("D17") is None
    assert canonical_ref("17") is None
    assert canonical_ref(None) is None


def test_end_words() -> None:
    assert end_word("centre_line (fridge outlet)") == "centre_line"
    assert end_word("Center line of sink") == "centre_line"
    assert end_word("top of counter") == "top_of_counter"
    assert end_word("underside_of_counter") == "underside_of_counter"
    assert end_word("soffit above") == "ceiling_soffit"
    assert end_word("somewhere") is None


def _label(page: int, n: int, side: str) -> Label:
    return Label(
        ref=f"p{page}.D{n}",
        page=page,
        id=f"D{n}",
        text='12"',
        inches="12",
        box=(10.0 * n, 10.0, 10.0 * n + 8, 18.0),
        side=side,
    )


def _drawing(ref: str, page: int, role: str, labels: list[Label]) -> Drawing:
    return Drawing(
        ref=ref,
        file="set.pdf",
        page=page,
        half=0,
        role=role,  # type: ignore[arg-type]
        box=(0.0, 0.0, 400.0, 300.0),
        crop_box=(0.0, 0.0, 400.0, 300.0),
        labels=tuple(labels),
    )


def _item() -> WorkItem:
    return WorkItem(
        id="p7.v1",
        file="set.pdf",
        page=7,
        vendor=_drawing("p7.v1", 7, "vendor", [_label(7, n, "vendor") for n in (3, 4, 5)]),
        architect=(_drawing("p6.a1", 6, "architect", [_label(6, 1, "architect")]),),
    )


def _pair(vendor: str, architect: str | None, verdict: str) -> Pair:
    return Pair(
        vendor_label=vendor,
        architect_label=architect,
        measures="width",
        vendor_from="wall_face",
        vendor_to="wall_face",
        architect_from="wall_face",
        architect_to="wall_face",
        verdict=verdict,  # type: ignore[arg-type]
        note="",
    )


def test_a_complete_answer_passes() -> None:
    result = CompareResult(
        work_item="p7.v1",
        pairs=(_pair("p7.D3", "p6.D1", "different"), _pair("p7.D4", None, "no_architect_value")),
        other_items=(),
        unaccounted_labels=("p7.D5",),
    )
    assert check_result(result, _item()) == []


def test_every_gap_is_named() -> None:
    result = CompareResult(
        work_item="p9.v1",
        pairs=(
            _pair("p7.D3", None, "same"),
            _pair("p7.D9", "p6.D1", "different"),
            _pair("p7.D4", "p6.D7", "no_architect_value"),
        ),
        other_items=(
            OtherItem(
                kind="missing_on_vendor",
                what="outlet",
                vendor=None,
                architect=None,
                vendor_box=None,
                architect_box=None,
                architect_page=None,
                verdict="different",
                millwork=True,
            ),
            OtherItem(
                kind="tag",
                what="finish tag",
                vendor="FN-1",
                architect="FN-2",
                vendor_box=None,
                architect_box=(1.0, 2.0, 3.0, 4.0),
                architect_page=8,
                verdict="different",
                millwork=True,
            ),
        ),
        unaccounted_labels=("p7.D4", "p6.D1"),
    )
    problems = "\n".join(check_result(result, _item()))
    assert "work_item is 'p9.v1'" in problems
    assert "p7.D9 is not a label of the vendor drawing" in problems
    assert "p6.D7 is not a label of this item's architect drawings" in problems
    assert "says same but names no architect label" in problems
    assert "says no_architect_value but names p6.D7" in problems
    assert "not vendor labels: p6.D1" in problems
    assert "also in unaccounted_labels: p7.D4" in problems
    assert "not accounted for (1): p7.D5" in problems
    assert "missing on the vendor needs" in problems
    assert "architect_page 8 is not one of this item's architect pages [6]" in problems


def test_label_ids_in_a_result_must_be_codes_own() -> None:
    with pytest.raises(ValidationError):
        _pair("D3", None, "unsure")
    with pytest.raises(ValidationError):
        CompareResult(work_item="p7.v1", pairs=(), other_items=(), unaccounted_labels=("7.3",))


def test_the_settings_default_to_the_roster_and_sonnet_high() -> None:
    settings = Settings(database_url="postgresql+psycopg://x/y", _env_file=None)  # type: ignore[call-arg]
    assert settings.assistant_full_check_mode == "roster"
    assert settings.assistant_simple_compare_model == "claude-sonnet-5-5"
    assert settings.assistant_simple_compare_effort == "high"
    assert settings.assistant_simple_hard_model == "claude-opus-5-5"
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+psycopg://x/y",
            assistant_full_check_mode="other",  # type: ignore[arg-type]
            _env_file=None,  # type: ignore[call-arg]
        )
