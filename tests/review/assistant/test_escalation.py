"""The escalation's free code steps (#1619), on synthetic facts, $0.

Every item code could not settle at first climbs the ladder, the biggest first, and stops at the
first step that settles it; code decides every outcome with the same witness rules as the red
clouds (a Right needs both values read by code and equal; a red cloud still needs code's proof):

- `labels`: code's label in the box that prints the item's value is a code reading; a value nobody
  could read becomes the only label in its box;
- `under_markup`: one size in the drawing's own ink under GV's markup is the vendor's value;
- `same_item`: a copy that is a proven Wrong, or a newer round of the same question, carries it;
- `vendor_sums`: a vendor string code proved does not add up carries it;
- `pairs`: code's own pairing of the two labels proves the pairing (red when code read both);
- what nothing settles is Not compared (with its reason), and every step tried is kept.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.review.assistant import proof
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
    """A Wrong item code has not proved: AI readings only, so Not compared today."""
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
    # Missed, changed or added finishes are code's own finish check (#1623), not a step here.
    assert FREE_STEPS == (
        "labels",
        "under_markup",
        "same_item",
        "vendor_sums",
        "pairs",
        "chain_sums",
        "vendor_search",
    )
    result = escalate([_wrong()], _ctx())
    finding = _by_id(result)["F1"]
    # Code read both values at their labels: a code witness, but no pairing proof yet, so the item
    # stays Not compared (never red on a reading alone): different reference points not ruled out.
    assert finding["checks"]["printed_vendor"] == "match"
    assert finding["checks"]["printed_architect"] == "match"
    assert finding["dims"]["vendor"]["id"] == "D1" and finding["witnesses"] == ["printed"]
    assert proof.proof_group(finding) == "not_compared"
    assert proof.proof_of(finding)["not_compared"] == "different_reference"
    steps = [s["step"] for s in finding["escalation"]["steps"]]
    assert steps == ["labels", "pairs"] and finding["escalation"]["outcome"] == "undecided"
    assert result.tried == 1 and result.settled == {}


@pytest.mark.usefixtures("read_twice")
def test_code_s_own_pairing_makes_a_code_read_difference_red() -> None:
    result = escalate([_wrong()], _ctx(pairs={"p3.D1": "p3.D2"}))
    finding = _by_id(result)["F1"]
    assert finding["paired_by"] == "code" and proof.proof_group(finding) == "clouded"
    assert finding["escalation"]["outcome"] == "wrong" and result.settled == {"pairs": 1}
    # Code pairing the vendor's label with ANOTHER architect label proves nothing.
    other = escalate([_wrong()], _ctx(pairs={"p3.D1": "p3.D9"}))
    assert proof.proof_group(_by_id(other)["F1"]) == "not_compared"


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
    assert proof.proof_group(finding) == "not_compared"
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
    assert proof.proof_group(finding) == "not_compared"  # the architect's side is not read


@pytest.mark.usefixtures("read_twice")
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


@pytest.mark.usefixtures("read_twice")
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


def test_a_tag_item_is_never_made_red_here() -> None:
    """A tag item stays Not compared: the finish check (#1623) alone proves a missed finish."""
    tags = _wrong(
        id="F9",
        item="Counter top material tag",
        architect="CT-001",
        vendor="ST-002",
        inches={},
    )
    finding = _by_id(escalate([tags], _ctx([])))["F9"]
    assert proof.proof_group(finding) == "not_compared" and "missing_tags" not in finding


@pytest.mark.usefixtures("read_twice")
def test_items_already_settled_or_decided_are_left_alone() -> None:
    right = _wrong(id="R1", status="same", compared=True)
    agreed = _wrong(id="A1", decision={"decision": "agree", "by": "Ann"})
    result = escalate([right, agreed], _ctx())
    assert result.tried == 0 and result.changed == []


# ---- EQ marks and a vendor value with no spot (round 1, 2026-10-20) ---------------------------------


def _eq_label(id_: str, at: tuple[float, float], inches: str, overall: str) -> dict[str, Any]:
    """`dims.json`'s derived label: an EQ part code worked out from the printed overall."""
    return {
        **_label(id_, "vendor", inches, at, text="EQ"),
        "derived": {"rule": "equal_parts", "from": overall, "parts": 2, "less": []},
    }


def test_eq_marks_take_their_equal_parts_value_and_are_compared() -> None:
    """The vendor's sink centre is EQ / EQ of a 40" run: code's 20" part against the
    architect's 26" is a difference code read (its pairing still has to be proved)."""
    labels = {
        (FILE, 3): [
            _label("D1", "architect", "26", (100, 100)),
            _label("D9", "vendor", "40", (300, 280)),
            _eq_label("D10", (280, 300), "20", "D9"),
            _eq_label("D11", (320, 300), "20", "D9"),
        ]
    }
    finding = _wrong(
        status="check",
        vendor="EQ",
        architect="2'-2\"",
        inches={"architect": "26", "vendor": None},
        vendor_box=_box((280, 300)) | {"x0": 272, "x1": 288},
        dims={"vendor": {"id": "D10", "page": 3, "file": FILE}},
        checks={"printed_architect": "match", "printed_vendor": "not_a_number"},
    )
    result = escalate([finding], EscalationContext(main_file=FILE, labels=labels))
    found = result.changed[0]
    assert found["vendor"] == 'EQ (20")'
    assert found["inches"]["vendor"] == "20"
    assert found["status"] == "different"
    assert found["checks"]["printed_vendor"] == "match"
    assert found["dims"]["vendor"]["derived"]["from"] == "D9"
    steps = [s["step"] for s in found["escalation"]["steps"]]
    assert steps[0] == "equal_parts"
    assert not proof.code_proved(found)  # code read both, but the pairing is not proved


def test_eq_with_no_derived_label_is_left_as_it_is() -> None:
    labels = {(FILE, 3): [_label("D10", "vendor", "20", (280, 300), text="EQ")]}
    finding = _wrong(
        status="check",
        vendor="EQ",
        inches={"architect": "26", "vendor": None},
        dims={"vendor": {"id": "D10", "page": 3, "file": FILE}},
    )
    result = escalate([finding], EscalationContext(main_file=FILE, labels=labels))
    assert result.changed[0]["vendor"] == "EQ"
    assert result.changed[0]["status"] == "check"


class _Reading:
    def __init__(self, outcome: str, box: tuple[float, ...] | None, reason: str) -> None:
        self.outcome, self.box, self.reason, self.label = outcome, box, reason, None

    def matches(self, claim: object) -> bool:
        return self.outcome == "read"

    def as_verify(self, claim: object) -> dict[str, Any]:
        return {
            "result": "match" if self.outcome == "read" else "cannot_read",
            "route": "drawn",
            "reason": self.reason,
            "value": claim,
            "pending": False,
            "read_number": "ocr_shape" if self.outcome == "read" else "no_spot",
        }


@pytest.mark.usefixtures("read_twice")
def test_a_vendor_value_with_no_spot_is_sent_to_the_read_number_tool() -> None:
    asked: list[Any] = []

    def unread(request: Any) -> _Reading:
        asked.append(request)
        return _Reading("unread", None, "the value has no spot on the drawing")

    finding = _wrong(vendor_box=None, dims={"architect": {"id": "D3", "page": 3, "file": FILE}})
    result = escalate([finding], EscalationContext(main_file=FILE, reread=unread))
    found = result.changed[0]
    # The vendor value is asked for by its claim; the reading record asks for labels by id only.
    claims = [(r.file, r.page, r.side, r.claim) for r in asked if r.claim is not None]
    assert claims == [(FILE, 3, "vendor", '46"')]
    assert found["checks"]["verify_vendor"]["result"] == "cannot_read"
    assert found["status"] == "different" and found.get("vendor_box") is None
    assert proof.proof_group(found) == "not_compared"  # listed, never red

    def read(request: Any) -> _Reading:
        return _Reading("read", (290.0, 295.0, 310.0, 305.0), "read by ocr and shape")

    found = escalate([finding], EscalationContext(main_file=FILE, reread=read)).changed[0]
    assert found["vendor_box"]["x0"] == 290.0 and found["vendor_box"]["page"] == 3
    assert found["checks"]["verify_vendor"]["result"] == "match"
    assert not proof.code_proved(found)  # a reading is no pairing proof: still not red


def test_a_value_that_has_its_spot_is_never_re_read() -> None:
    def never(request: Any) -> _Reading:
        raise AssertionError("not asked")

    escalate([_wrong()], EscalationContext(main_file=FILE, reread=never))


@pytest.mark.usefixtures("read_twice")
def test_stop_ends_the_ladder_and_counts_each_item_tried() -> None:
    """2026-10-23: Stop pressed during the free steps: no further item is tried (each one tried
    keeps what code found; a Continue tries the rest), and the live line counts the items."""
    counts: list[int] = []
    tried: list[int] = []

    def count(total: int) -> Any:
        counts.append(total)
        return lambda: tried.append(1)

    stopped = escalate(
        [_wrong(), _wrong(id="F2", page=3)],
        _ctx(stop=lambda: len(tried) >= 1, count=count),
    )
    assert counts == [2] and len(tried) == 1 and stopped.tried == 1
    assert list(_by_id(stopped)) == ["F1"]
    whole = escalate([_wrong(), _wrong(id="F2", page=3)], _ctx())
    assert whole.tried == 2
    assert _by_id(stopped)["F1"] == _by_id(whole)["F1"]


def test_a_reading_record_that_settles_an_item_is_never_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-10-28: the first reading pass wrote an item's reading record on the escalation's
    working copy without listing it as changed; when that record settled the item, the ladder
    skipped it and the last pass found nothing new, so the record never reached the answer (on a
    real Continue a vendor string lost its record that way and was held back as "no record of how
    the string's other numbers were read"). Every record that changed is listed."""
    from app.review.assistant import escalation, reading_routes

    record = {"values": [{"side": "vendor", "label": "p3.D1"}], "doubts": [], "enough": True}

    def mark(finding: dict[str, Any], *_given: Any) -> bool:
        before = finding.get(reading_routes.FIELD)
        finding[reading_routes.FIELD] = record
        return before != record

    monkeypatch.setattr(reading_routes, "mark", mark)
    # The record is what settles the item here: the ladder then leaves it alone.
    monkeypatch.setattr(
        escalation, "undecided", lambda finding: reading_routes.FIELD not in finding
    )

    result = escalate([_wrong()], _ctx())

    assert _by_id(result)["F1"][reading_routes.FIELD] == record
