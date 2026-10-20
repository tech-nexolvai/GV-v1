"""The compare answer's claims (#1775: C3, recipe step 2). $0, no model call.

Each pair is a small checkable claim in a fixed order: the vendor label, the architect label, what
it measures, where each end lands, then the verdict (same_points / different_points / unsure /
no_architect_value) and a weak point. The AI never says whether the values are equal; code does.
Answers saved in the older form (same / different, no weak point) still read, so replay works.
"""

from __future__ import annotations

from app.review.assistant.simple_check import prompts, resolve
from app.review.assistant.simple_check.schema import (
    LEGACY_VERDICTS,
    REPLY_SCHEMA,
    VERDICTS,
    WEAK_POINTS,
    CompareOutcome,
    Pair,
    Reply,
)

_PAIR = REPLY_SCHEMA["properties"]["pairs"]["items"]


def test_the_form_asks_points_never_values() -> None:
    assert _PAIR["properties"]["verdict"]["enum"] == [
        "same_points",
        "different_points",
        "unsure",
        "no_architect_value",
    ]
    assert not set(LEGACY_VERDICTS) & set(VERDICTS)
    assert _PAIR["properties"]["weak_point"]["enum"] == list(WEAK_POINTS)


def test_the_ends_come_before_the_verdict_and_the_weak_point_after() -> None:
    order = list(_PAIR["properties"])
    assert order.index("vendor_label") < order.index("architect_label") < order.index("vendor_from")
    assert max(order.index(e) for e in ("vendor_to", "architect_from", "architect_to")) < (
        order.index("verdict")
    )
    assert order.index("verdict") < order.index("weak_point")
    assert sorted(_PAIR["required"]) == sorted(_PAIR["properties"])


def test_the_prompt_says_code_compares_the_numbers() -> None:
    text = prompts.SYSTEM_PROMPT
    assert "Never judge whether the values are equal" in text
    assert "the values are equal;" not in text and "the values differ" not in text
    reply = Reply.model_validate(prompts.EXAMPLE_REPLY)
    assert {p.verdict for p in reply.pairs} <= set(VERDICTS)
    assert all(p.weak_point in WEAK_POINTS for p in reply.pairs)
    assert "Never judge whether the values are equal" in resolve.RESOLVE_SYSTEM
    assert resolve.PROPOSAL_SCHEMA["properties"]["verdict"]["enum"] == [
        "same_points",
        "different_points",
        "unsure",
    ]


def test_answers_saved_in_the_older_form_still_read() -> None:
    old = {
        "vendor_label": "p3.D1",
        "architect_label": "p2.D1",
        "measures": "width",
        "vendor_from": "wall_face (left)",
        "vendor_to": "wall_face (right)",
        "architect_from": "wall_face (left)",
        "architect_to": "wall_face (right)",
        "verdict": "Different",
        "note": "",
    }
    pair = Pair.model_validate(old)
    assert pair.verdict == "different" and pair.weak_point is None
    saved = {
        "work_item": "p3.v1",
        "status": "ok",
        "result": {
            "work_item": "p3.v1",
            "pairs": [old, {**old, "vendor_label": "p3.D2", "verdict": "same"}],
            "other_items": [],
            "unaccounted_labels": [],
        },
        "asked_model": "claude-sonnet-5-5",
        "effort": "high",
    }
    outcome = CompareOutcome.model_validate(saved)
    assert outcome.result is not None and [p.verdict for p in outcome.result.pairs] == [
        "different",
        "same",
    ]


def test_an_unknown_weak_point_is_other() -> None:
    pair = Pair.model_validate(
        {
            "vendor_label": "p3.D1",
            "architect_label": "p2.D1",
            "measures": "",
            "vendor_from": "floor",
            "vendor_to": "top_of_counter",
            "architect_from": "floor",
            "architect_to": "top_of_counter",
            "verdict": "same_points",
            "weak_point": "Blurry Scan",
            "note": "",
        }
    )
    assert pair.weak_point == "other"


def test_resolve_reads_same_points_as_the_older_different() -> None:
    assert resolve._verdict({"verdict": "same_points"}) == "different"
    proposal = resolve.Proposal.from_dict({"kind": "ends", "verdict": "SAME_POINTS"})
    assert proposal.verdict == "same_points"
