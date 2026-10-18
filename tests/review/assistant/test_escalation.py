"""The escalation's free code steps (#1619), on synthetic facts, $0.

Every item code could not settle at first climbs the ladder, the biggest first, and stops at the
first step that settles it; code decides every outcome with the same witness rules as the red
clouds (a Right needs both values read by code and equal; a red cloud still needs code's proof):

- `labels`: code's label in the box that prints the item's value is a code reading; a value nobody
  could read becomes the only label in its box;
- `under_markup`: one size in the drawing's own ink under GV's markup is the vendor's value;
- `same_item`: a copy that is a proven Wrong, or a newer round of the same question, carries it;
- `vendor_sums`: a vendor string code proved does not add up carries it;
- `notes`: the architect's tags the vendor's drawing does not print;
- `pairs`: code's own pairing of the two labels proves the pairing (red when code read both);
- what nothing settles stays a question to the vendor, and every step tried is kept.
"""

from __future__ import annotations

from typing import Any

from app.review.assistant import proof, vendor_confirm
from app.review.assistant.escalation import (
    FREE_STEPS,
    EscalationContext,
    escalate,
)

FILE = "set.pdf"


def _label(
    id_: str, side: str, inches: str, at: tuple[float, float], text: str | None = None
) -> dict[str, Any]:
    x, y = at
    return {
        "id": id_,
        "text": text or f'{inches}"',
        "inches": inches,
        "box": [x - 8, y - 4, x + 8, y + 4],
        "side": side,
        "orientation": "h",
        "route": "text",
    }


def _box(at: tuple[float, float], page: int = 3) -> dict[str, Any]:
    x, y = at
    return {"page": page, "x0": x - 20, "y0": y - 10, "x1": x + 20, "y1": y + 10, "file": FILE}


def _wrong(**changes: Any) -> dict[str, Any]:
    """A Wrong item code has not proved: AI readings only, so a question to the vendor today."""
    finding: dict[str, Any] = {
        "id": "F1",
        "page": 3,
        "round": 1,
        "item": "Outlet height",
        "category": "cabinet",
        "architect": "3'-1\"",
        "vendor": '46"',
        "status": "different",
        "source": "architect",
        "trust_level": "to_check",
        "witnesses": [],
        "checks": {"printed_architect": "no_text", "printed_vendor": "no_text"},
        "inches": {"architect": "37", "vendor": "46", "difference": "9"},
        "vendor_box": _box((300, 300)),
        "arch_box": _box((100, 100)),
    }
    finding.update(changes)
    return finding


def _decision(**changes: Any) -> dict[str, Any]:
    finding = _wrong(
        id="F2",
        item="Counter height",
        status="check",
        vendor="partly cut off",
        inches={"architect": "37", "vendor": None},
        keep_reason="unreadable",
    )
    finding.update(changes)
    return finding


def _ctx(labels: list[dict[str, Any]] | None = None, **more: Any) -> EscalationContext:
    found = (
        labels
        if labels is not None
        else [
            _label("D1", "vendor", "46", (300, 300)),
            _label("D2", "architect", "37", (100, 100), "3'-1\""),
        ]
    )
    page_map = {
        "pages": [
            {"file": FILE, "page": page, "area": "Lobby desk", "section": "1"} for page in (3, 4, 5)
        ]
    }
    return EscalationContext(main_file=FILE, labels={(FILE, 3): found}, page_map=page_map, **more)


def _by_id(result: Any) -> dict[str, dict[str, Any]]:
    return {str(f["id"]): f for f in result.changed}


def test_the_ladder_is_cheapest_first_and_every_step_tried_is_kept() -> None:
    assert FREE_STEPS == ("labels", "under_markup", "same_item", "vendor_sums", "notes", "pairs")
    result = escalate([_wrong()], _ctx())
    finding = _by_id(result)["F1"]
    # Code read both values at their labels: a code witness, but no pairing proof yet, so the item
    # stays a question to the vendor (never red on a reading alone).
    assert finding["checks"]["printed_vendor"] == "match"
    assert finding["checks"]["printed_architect"] == "match"
    assert finding["dims"]["vendor"]["id"] == "D1" and finding["witnesses"] == ["printed"]
    assert proof.proof_group(finding) == "vendor_confirm"
    steps = [s["step"] for s in finding["escalation"]["steps"]]
    assert steps == ["labels", "pairs"] and finding["escalation"]["outcome"] == "undecided"
    assert result.tried == 1 and result.settled == {}


def test_code_s_own_pairing_makes_a_code_read_difference_red() -> None:
    result = escalate([_wrong()], _ctx(pairs={"p3.D1": "p3.D2"}))
    finding = _by_id(result)["F1"]
    assert finding["paired_by"] == "code" and proof.proof_group(finding) == "clouded"
    assert finding["escalation"]["outcome"] == "wrong" and result.settled == {"pairs": 1}
    # Code pairing the vendor's label with ANOTHER architect label proves nothing.
    other = escalate([_wrong()], _ctx(pairs={"p3.D1": "p3.D9"}))
    assert proof.proof_group(_by_id(other)["F1"]) == "vendor_confirm"


def test_an_unreadable_value_is_read_from_the_only_label_in_its_box() -> None:
    # The label prints the architect's value: code read both, they are the same: Right.
    labels = [
        _label("D1", "vendor", "37", (300, 300)),
        _label("D2", "architect", "37", (100, 100), "3'-1\""),
    ]
    result = escalate([_decision()], _ctx(labels))
    finding = _by_id(result)["F2"]
    assert finding["status"] == "same" and finding["vendor"] == '37"'
    assert finding["settled"]["method"] == "escalation_labels"
    assert finding["settled"]["was"] == "check" and proof.proof_group(finding) == "right"
    # Another value: a difference code read, still asked (no pairing proof).
    labels[0] = _label("D1", "vendor", "40", (300, 300))
    finding = _by_id(escalate([_decision()], _ctx(labels)))["F2"]
    assert finding["status"] == "different" and finding["inches"]["vendor"] == "40"
    assert proof.proof_group(finding) == "vendor_confirm"
    # Two labels in the box: code does not guess which one.
    labels.append(_label("D3", "vendor", "41", (305, 302)))
    finding = _by_id(escalate([_decision()], _ctx(labels)))["F2"]
    assert finding["status"] == "check" and finding["inches"]["vendor"] is None


def test_one_size_under_gvs_markup_is_the_vendors_value() -> None:
    def under(file: str, page: int, box: Any) -> list[dict[str, Any]]:
        assert (file, page) == (FILE, 3)
        return [
            {"text": "33", "box": (290, 296, 300, 304), "coloured": False},
            {"text": '1/4"', "box": (301, 296, 312, 304), "coloured": False},
            {"text": '36"', "box": (290, 306, 300, 312), "coloured": True},  # GV's own number
        ]

    result = escalate([_decision()], _ctx([], under=under))
    finding = _by_id(result)["F2"]
    assert finding["inches"]["vendor"] == "33.25" and finding["vendor"] == '33 1/4"'
    assert finding["checks"]["printed_vendor"] == "match"
    steps = {s["step"]: s for s in finding["escalation"]["steps"]}
    assert steps["under_markup"]["outcome"] == "read"
    assert proof.proof_group(finding) == "vendor_confirm"  # the architect's side is not read


def test_the_same_item_on_another_round_or_a_proven_copy_carries_it() -> None:
    older = _wrong(id="F1", page=3, round=1)
    newer = _wrong(id="F5", page=4, round=2, vendor_box=_box((300, 300), page=4))
    result = escalate([older, newer], _ctx())
    assert set(result.absorbed) == {"F1"}
    assert "Same item as F5 (page 4)" in result.absorbed["F1"]
    carrier = _by_id(result)["F5"]
    assert [a["finding_id"] for a in carrier["also_on"]] == ["F1"]
    # A proven Wrong copy carries an undecided one, whatever the round.
    proven = _wrong(
        id="F6",
        page=5,
        round=1,
        source="rules",
        rule_check_id="C1",
        trust_level="likely",
        witnesses=["rule"],
        checks={"rule": "agrees"},
    )
    result = escalate([_wrong(id="F7", round=2), proven], _ctx())
    assert "a proven Wrong" in result.absorbed["F7"]
    # Another area or another value is another item.
    elsewhere = escalate([older, {**newer, "inches": {"architect": "37", "vendor": "45"}}], _ctx())
    assert elsewhere.absorbed == {}


def test_a_vendor_string_code_proved_carries_the_question_about_it() -> None:
    string = {
        **_wrong(id="C1", created_by="consistency", source="vendor_arithmetic"),
        "trust_level": "likely",
        "witnesses": ["arithmetic"],
        "checks": {"arithmetic": "does_not_add_up"},
        "printed_total": '46"',
        "parts_sum": "45.5",
        "inches": {"architect": None, "vendor": "46"},
    }
    asked = _decision(
        id="F8",
        keep_reason="vendor_disagrees",
        vendor='46" overall',
        inches={"architect": None, "vendor": "46"},
    )
    assert proof.proof_group(string) == "clouded"
    result = escalate([asked, string], _ctx([]))
    assert "Same item as C1" in result.absorbed["F8"]


def test_tags_the_vendor_leaves_out_are_named() -> None:
    tags = _wrong(
        id="F9",
        item="Counter top material tag",
        architect="CT-001",
        vendor="ST-002",
        inches={},
    )
    notes = {(FILE, 3): {"architect_only_tags": ["CT-001"], "vendor_only_tags": ["ST-002"]}}
    finding = _by_id(escalate([tags], _ctx([], notes=notes)))["F9"]
    assert finding["missing_tags"] == ["CT-001"]
    assert vendor_confirm.question_of(finding) == (
        "The architect tags CT-001 on the same sheet for the counter top material tag; the "
        "vendor's drawing on page 3 does not print it. Confirm the material and finish."
    )
    assert any(
        s["step"] == "notes" and s["outcome"] == "read" for s in finding["escalation"]["steps"]
    )


def test_items_already_settled_or_decided_are_left_alone() -> None:
    right = _wrong(id="R1", status="same", compared=True)
    agreed = _wrong(id="A1", decision={"decision": "agree", "by": "Ann"})
    result = escalate([right, agreed], _ctx())
    assert result.tried == 0 and result.changed == []
