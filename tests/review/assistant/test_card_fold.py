"""Fold and order the cards (`card_fold`, final plan change #8): one card per distinct question.

- The copies of one question on the sibling pages of a unit (two vendor pages paired with one
  reference page) are one card when code facts agree: the same outcome, the same reason, the same
  vendor value and the same reference dimension (its printed label).
- Never joined: different values, a red with a non-red, a Wrong or Likely problem with no reference
  label, two unlabelled items on one page, another reference page, GV's rules.
- Nothing is dropped: every item is on exactly one card, and the lead lists the others.
- A Right whose ends code did not prove alike leads only when no proved sibling exists.
- "Nothing to compare here" (`aside`): the reference gives no size, a different extent, a
  field-verify note; "could not read" stays a question. Questions carry their reason's key.

Made-up drawings and values only.
"""

from __future__ import annotations

import itertools
from typing import Any

from app.review.assistant import card_fold, dim_ends
from app.review.assistant.chat_contract import ChatFindingCardOut
from app.review.assistant.proof import proof_group
from tests.review.assistant.reading_fixture import two_routes

VENDOR_FILE = "vendor.pdf"
ARCH_FILE = "architect.pdf"


def _dim(label: str, page: int, inches: str, file: str) -> dict[str, Any]:
    return {
        "id": label,
        "page": page,
        "file": file,
        "box": [100.0, 100.0, 115.0, 110.0],
        "text": f'{inches}"',
        "inches": inches,
        "route": "text",
    }


def _item(
    fid: str,
    kind: str,
    *,
    page: int = 3,
    vs_page: int = 7,
    vendor: str | None = "D5",
    architect: str | None = "D8",
    v_in: str | None = "36",
    a_in: str | None = "36",
    **changes: Any,
) -> dict[str, Any]:
    """A finding on vendor `page` (reference page `vs_page`) of the given group: `right`,
    `clouded`, `not_compared` or `likely_problem`."""
    dims: dict[str, Any] = {}
    if vendor:
        dims["vendor"] = _dim(vendor, page, v_in or "0", VENDOR_FILE)
    if architect:
        dims["architect"] = _dim(architect, vs_page, a_in or "0", ARCH_FILE)
    finding: dict[str, Any] = {
        "id": fid,
        "page": page,
        "vs_page": vs_page,
        "item": f"Vanity top: width ({fid})",
        "category": "countertop",
        "vendor": f'{v_in}"' if v_in else "see the drawing",
        "architect": f'{a_in}"' if a_in else "see the drawing",
        "inches": {"vendor": v_in, "architect": a_in, "difference": None},
        "source": "architect",
        "dims": dims or None,
        "vendor_box": {"page": page, "x0": 100, "y0": 100, "x1": 115, "y1": 110},
        "arch_box": {"page": vs_page, "x0": 300, "y0": 300, "x1": 315, "y1": 310},
        "created_by": "helper:1",
        "checks": {"second_read": "agrees", "printed_vendor": "match"},
    }
    if kind == "right":
        finding.update(status="same", compared=True, trust_level="confirmed", witnesses=["printed"])
    elif kind == "clouded":
        finding.update(
            status="different", trust_level="confirmed", witnesses=["printed"], paired_by="code"
        )
        two_routes(finding)
    elif kind == "not_compared":
        finding.update(status="different", trust_level="to_check", witnesses=[])
    elif kind == "likely_problem":
        finding.update(
            status="check",
            trust_level="to_check",
            witnesses=[],
            created_by="whole_look",
            check_reason="pairing_not_certain",
            whole_look={"by": "whole_drawing_look", "kind": "size", "proof": "likely_only"},
        )
    else:  # pragma: no cover - a test's own mistake
        raise AssertionError(kind)
    finding.update(changes)
    return finding


def _cards(findings: list[dict[str, Any]]) -> list[list[str]]:
    return [[str(m["id"]) for m in card.members] for card in card_fold.cards(findings)]


def test_the_fixtures_are_the_groups_they_say() -> None:
    for kind in ("right", "clouded", "not_compared", "likely_problem"):
        assert proof_group(_item("F1", kind, a_in="36" if kind == "right" else "40")) == kind


def test_the_regular_and_reversed_sheet_of_one_unit_are_one_card() -> None:
    regular = _item("F1", "right", page=3)
    reversed_ = _item("F2", "right", page=4)
    found = card_fold.cards([regular, reversed_])
    assert [[m["id"] for m in c.members] for c in found] == [["F1", "F2"]]
    assert found[0].pages == [3, 4]
    assert found[0].how == "same_reference_dimension"
    assert found[0].agree_all is False  # a Right: nothing to agree with


def test_a_plan_and_an_elevation_copy_on_one_page_are_one_card() -> None:
    plan = _item("F1", "right", vendor="D5")
    elevation = _item("F2", "right", vendor="D9")
    found = card_fold.cards([plan, elevation])
    assert [len(c.members) for c in found] == [2]
    assert found[0].how == "same_page"


def test_different_values_are_never_one_card() -> None:
    assert _cards([_item("F1", "right"), _item("F2", "right", page=4, v_in="37", a_in="37")]) == [
        ["F1"],
        ["F2"],
    ]
    # The same vendor value against two reference dimensions: two questions.
    assert _cards([_item("F1", "right"), _item("F2", "right", page=4, architect="D9")]) == [
        ["F1"],
        ["F2"],
    ]


def test_the_same_sizes_with_another_outcome_are_named_never_joined() -> None:
    red = _item("F1", "clouded", a_in="40")
    copy = _item("F2", "not_compared", page=4, a_in="40")
    blocks = card_fold.blocks_with_cards([{"type": "finding", "finding": f} for f in (red, copy)])
    first, second = (ChatFindingCardOut.model_validate(b["finding"]["card"]) for b in blocks)
    assert first.lead and second.lead
    assert [(r.finding_id, r.page, r.group) for r in first.related] == [("F2", 4, "not_compared")]
    assert [r.finding_id for r in second.related] == ["F1"]


def test_a_red_never_joins_a_non_red() -> None:
    for other in ("not_compared", "likely_problem", "right"):
        red = _item("F1", "clouded", a_in="40")
        copy = _item("F2", other, page=4, a_in="36" if other == "right" else "40")
        assert _cards([red, copy]) == [["F1"], ["F2"]], other


def test_the_same_proven_difference_is_one_card_and_one_agree_applies_to_all() -> None:
    found = card_fold.cards(
        [_item("F1", "clouded", a_in="40"), _item("F2", "clouded", page=4, a_in="40")]
    )
    assert [[m["id"] for m in c.members] for c in found] == [["F1", "F2"]]
    assert found[0].agree_all is True


def test_a_proven_difference_joins_only_on_the_same_reference_label() -> None:
    one = _item("F1", "clouded", a_in="40", architect=None)
    two = _item("F2", "clouded", page=4, a_in="40", architect=None)
    assert _cards([one, two]) == [["F1"], ["F2"]]


def test_another_reference_page_is_another_unit() -> None:
    assert _cards([_item("F1", "right"), _item("F2", "right", page=4, vs_page=9)]) == [
        ["F1"],
        ["F2"],
    ]


def test_two_unlabelled_items_on_one_page_stay_two() -> None:
    one = _item("F1", "not_compared", vendor=None, architect=None, a_in="40")
    two = _item("F2", "not_compared", vendor=None, architect=None, a_in="40")
    assert _cards([one, two]) == [["F1"], ["F2"]]
    # On two sibling pages the same values with no label are one card.
    sibling = _item("F3", "not_compared", page=4, vendor=None, architect=None, a_in="40")
    assert _cards([one, sibling]) == [["F1", "F3"]]
    assert card_fold.cards([one, sibling])[0].how == "same_values"


def test_gv_rules_and_reviewer_decisions_keep_their_own_card() -> None:
    rule = _item("F1", "not_compared", a_in="40", source="rules", rule_check_id="CT001")
    copy = _item("F2", "not_compared", page=4, a_in="40", source="rules", rule_check_id="CT001")
    assert _cards([rule, copy]) == [["F1"], ["F2"]]
    # One copy marked fine by the reviewer: a different outcome now, so a card of its own.
    mine = _item("F3", "not_compared", a_in="40", decision={"decision": "not_a_problem"})
    other = _item("F4", "not_compared", page=4, a_in="40")
    assert _cards([mine, other]) == [["F3"], ["F4"]]


def test_different_reasons_are_never_one_card() -> None:
    unread = _item("F1", "not_compared", a_in="40", status="check", keep_reason="unreadable")
    unproved = _item("F2", "not_compared", page=4, a_in="40")
    assert card_fold.question_key(unread)[0] != card_fold.question_key(unproved)[0]
    assert _cards([unread, unproved]) == [["F1"], ["F2"]]


def test_nothing_to_compare_leaves_the_work_list_and_could_not_read_stays() -> None:
    nothing = _item("F1", "not_compared", status="check", architect=None, a_in=None) | {
        "architect": "not shown on the architect's drawing"
    }
    unread = _item("F2", "not_compared", a_in="40", status="check", keep_reason="unreadable")
    found = {str(c.lead["id"]): c for c in card_fold.cards([nothing, unread])}
    assert card_fold.question_key(nothing)[0] == "nothing_to_compare"
    assert found["F1"].aside is True
    assert card_fold.question_key(unread)[0] == "unreadable"
    assert found["F2"].aside is False
    counts = card_fold.card_counts([nothing, unread])
    assert counts["aside"] == 1 and counts["not_compared"] == 1


def test_a_missing_job_value_is_named_in_the_question() -> None:
    record = {
        "rule": "R-depth",
        "name": "Countertop depth",
        "formula": "cabinet depth + front overhang",
        "missing": ["front overhang"],
    }
    item = _item("F1", "not_compared", a_in="40", job_value_rule=record)
    assert card_fold.question_key(item) == (
        "missing_setting:front overhang",
        "Job value missing: front overhang",
    )
    other = _item("F2", "not_compared", page=4, a_in="40", job_value_rule=record)
    assert _cards([item, other]) == [["F1", "F2"]]
    assert card_fold.cards([item, other])[0].aside is False


def test_a_match_with_ends_not_proved_folds_under_its_proved_sibling() -> None:
    record_of = {"vendor": "vendor.pdf|3|D5", "architect": "architect.pdf|7|D8"}
    unproved = _item("F1", "right")
    unproved[dim_ends.LIKE_FOR_LIKE] = {**record_of, "proved": False}
    proved = _item("F2", "right", page=4)
    proved[dim_ends.LIKE_FOR_LIKE] = {
        "vendor": "vendor.pdf|4|D5",
        "architect": "architect.pdf|7|D8",
        "proved": True,
    }
    found = card_fold.cards([unproved, proved])
    assert [[m["id"] for m in c.members] for c in found] == [["F2", "F1"]]


def test_nothing_is_dropped_and_the_payload_is_the_contract() -> None:
    kinds = ("right", "clouded", "not_compared", "likely_problem")
    findings = []
    for n, (kind, page, label) in enumerate(itertools.product(kinds, (3, 4), ("D8", "D9"))):
        a_in = "36" if kind == "right" else "40"
        findings.append(_item(f"F{n}", kind, page=page, architect=label, a_in=a_in))
    found = card_fold.cards(findings)
    ids = [str(m["id"]) for card in found for m in card.members]
    assert sorted(ids) == sorted(str(f["id"]) for f in findings)
    assert len(ids) == len(set(ids))
    for card in found:
        assert len({proof_group(m) for m in card.members}) == 1
    blocks = [{"type": "finding", "finding": f} for f in findings] + [{"type": "text"}]
    shown = card_fold.blocks_with_cards(blocks)
    payloads = [ChatFindingCardOut.model_validate(b["finding"]["card"]) for b in shown[:-1]]
    leads = [p for p in payloads if p.lead]
    assert len(leads) == len(found)
    assert sum(1 + len(p.copies) for p in leads) == len(findings)
    assert shown[-1] == {"type": "text"}
