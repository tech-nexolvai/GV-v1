"""One item, one card (`one_card`): the full check's last step folds the copies of one item.

- Copies are joined by code facts only: the same printed vendor label, the same value at the same
  spot, the same architect label with the vendor spot on top of it, the same finish tags on the
  same page pair. Words and a big AI region alone never join two items.
- The card kept is the most severe copy (code-proved Wrong, Likely problem, Not compared, Right),
  unchanged; the others are kept inside it as evidence. One vendor number paired with two
  architect numbers is Not compared, with both values.
- A merged card is never redder or greener than its copies (every combination).
- A "no architect drawing" item on a Drawing-needed page folds under that page's card; the
  vendor's own checks stay.

Made-up drawings and values only.
"""

from __future__ import annotations

import itertools
from collections import Counter
from typing import Any

import pytest

from app.review.assistant import not_compared, one_card
from app.review.assistant.chat_contract import ChatCoveragePageOut, ChatFindingSameItemOut
from app.review.assistant.proof import proof_counts, proof_group

VENDOR_FILE = "vendor.pdf"
ARCH_FILE = "architect.pdf"


def _dim(label: str, page: int, inches: str, at: tuple[float, float], file: str) -> dict[str, Any]:
    x, y = at
    return {
        "id": label,
        "page": page,
        "file": file,
        "box": [x, y, x + 15, y + 10],
        "text": f'{inches}"',
        "inches": inches,
        "route": "text",
    }


def _vbox(at: tuple[float, float], size: tuple[float, float] = (15, 10)) -> dict[str, Any]:
    x, y = at
    return {"page": 3, "x0": x, "y0": y, "x1": x + size[0], "y1": y + size[1], "file": VENDOR_FILE}


def _item(
    fid: str,
    kind: str,
    *,
    vendor: str | None = "D5",
    architect: str | None = "D8",
    v_in: str | None = "36",
    a_in: str | None = "36",
    at: tuple[float, float] = (100.0, 100.0),
    **changes: Any,
) -> dict[str, Any]:
    """A finding on vendor page 3 (architect page 7) of the given card: `right`, `clouded`,
    `not_compared` or `likely_problem`."""
    dims: dict[str, Any] = {}
    if vendor:
        dims["vendor"] = _dim(vendor, 3, v_in or "0", at, VENDOR_FILE)
    if architect:
        dims["architect"] = _dim(architect, 7, a_in or "0", (300.0, 300.0), ARCH_FILE)
    finding: dict[str, Any] = {
        "id": fid,
        "page": 3,
        "vs_page": 7,
        "item": f"Vanity top: width ({fid})",
        "category": "countertop",
        "vendor": f'{v_in}"' if v_in else "see the drawing",
        "architect": f'{a_in}"' if a_in else "see the drawing",
        "inches": {"vendor": v_in, "architect": a_in, "difference": None},
        "source": "architect",
        "dims": dims or None,
        "vendor_box": _vbox(at),
        "arch_box": {"page": 7, "x0": 300, "y0": 300, "x1": 315, "y1": 310, "file": ARCH_FILE},
        "created_by": "helper:1",
        "checks": {"second_read": "agrees", "printed_vendor": "match"},
    }
    if kind == "right":
        finding.update(status="same", compared=True, trust_level="confirmed", witnesses=["printed"])
    elif kind == "clouded":
        finding.update(
            status="different", trust_level="confirmed", witnesses=["printed"], paired_by="code"
        )
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


def _apply(findings: list[dict[str, Any]], needed: tuple[int, ...] = ()) -> list[dict[str, Any]]:
    """The final list after the step, as the engine leaves it."""
    result = one_card.fold(findings, needed)
    kept = {str(f["id"]): f for f in result.kept}
    gone = {f.drop for f in result.folded}
    return [kept.get(str(f["id"]), f) for f in findings if str(f["id"]) not in gone]


def test_the_fixtures_are_the_cards_they_say() -> None:
    for kind in ("right", "clouded", "not_compared", "likely_problem"):
        assert proof_group(_item("F1", kind, a_in="36" if kind == "right" else "40")) == kind


def test_two_rights_on_one_vendor_label_are_one_right_keeping_the_better_proved() -> None:
    first = _item("F10", "right", trust_level="likely")
    again = _item("F35", "right")  # a look-again closer: the same label, confirmed
    final = _apply([first, again])
    assert [f["id"] for f in final] == ["F35"] and proof_group(final[0]) == "right"
    copies = final[0]["same_item"]["copies"]
    assert [(c["finding_id"], c["group"]) for c in copies] == [("F10", "right")]
    assert proof_counts(final)["right"] == 1


def test_one_vendor_number_with_two_architect_numbers_is_not_compared_with_both() -> None:
    right = _item("F1", "right")
    other = _item("F2", "not_compared", architect="D9", a_in="21")
    final = _apply([right, other])
    assert len(final) == 1
    assert proof_group(final[0]) == "not_compared"
    assert final[0]["same_item"]["conflict"] == ['21"', '36"']
    row = not_compared.rows(final)[0]
    assert "2 different architect sizes" in row.why
    # The same for a code-proved Wrong: its pair is in doubt, so it is never red.
    proved = _item("F3", "clouded", a_in="40")
    final = _apply([proved, _item("F4", "right")])
    assert proof_group(proved) == "clouded"
    assert [proof_group(f) for f in final] == ["not_compared"]


def test_a_proved_wrong_stays_wrong_and_keeps_the_likely_copy_as_evidence() -> None:
    proved = _item("F5", "clouded", a_in="40")
    likely = _item("F6", "likely_problem", a_in="40")
    final = _apply([likely, proved])
    assert [f["id"] for f in final] == ["F5"] and proof_group(final[0]) == "clouded"
    assert final[0]["same_item"]["copies"][0]["group"] == "likely_problem"


def test_a_likely_problem_at_the_spot_of_a_helper_label_carries_its_reason() -> None:
    """The look named the architect's label but no vendor label: its spot holds the helper's."""
    helper = _item("F42", "not_compared", vendor="D7", architect="D16", v_in="12", a_in="21.9375")
    look = _item(
        "F33",
        "likely_problem",
        vendor=None,
        architect="D16",
        v_in="12",
        a_in="21.9375",
        vendor_box=_vbox((80.0, 60.0), (60.0, 95.0)),
    )
    final = _apply([helper, look])
    assert [f["id"] for f in final] == ["F33"] and proof_group(final[0]) == "likely_problem"
    copy = final[0]["same_item"]["copies"][0]
    assert copy["finding_id"] == "F42" and copy["group"] == "not_compared" and copy["reason"]
    assert "conflict" not in final[0]["same_item"]
    # The review of #1916: the look reads another vendor number in its spot (22 1/2" there, the
    # helper's label prints 12"): two vendor dimensions against one architect one, two items.
    assert len(_apply([helper, {**look, "inches": {**look["inches"], "vendor": "22.5"}}])) == 2
    # Another architect label at that spot: not the same item.
    other = {**look, "dims": {"architect": _dim("D17", 7, "21", (300, 300), ARCH_FILE)}}
    assert len(_apply([helper, other])) == 2


def test_the_same_finish_tags_on_the_same_two_pages_are_one_card() -> None:
    helper = _item(
        "F6",
        "not_compared",
        vendor=None,
        architect=None,
        v_in=None,
        a_in=None,
        vendor_box=_vbox((50.0, 690.0), (550.0, 120.0)),
    )
    helper.update(vendor="Stone 2cm, mitered edge", architect="SS 01 solid surface top")
    look = _item(
        "F27",
        "likely_problem",
        vendor=None,
        architect=None,
        v_in=None,
        a_in=None,
        vendor_box=_vbox((430.0, 470.0), (240.0, 90.0)),
    )
    look.update(vendor="stone, 2cm, no finish tag", architect="SOLID SURFACE TOP SS-01 (section)")
    final = _apply([helper, look])
    assert [f["id"] for f in final] == ["F27"] and proof_group(final[0]) == "likely_problem"
    # Other codes, or another architect page: two items.
    assert len(_apply([helper, {**look, "architect": "QZ-02 quartz top"}])) == 2
    assert len(_apply([helper, {**look, "vs_page": 8}])) == 2
    # A finish code on one vendor side only: two items.
    assert len(_apply([helper, {**look, "vendor": "ST-3 stone"}])) == 2
    # The review of #1916: no tag on either vendor side and other materials (upper doors painted,
    # lower doors veneer, both against one architect tag): two items.
    painted = {**helper, "vendor": "painted white", "architect": "PL-1 laminate doors"}
    veneer = {**look, "vendor": "rift oak veneer", "architect": "PL 1 laminate (section)"}
    assert len(_apply([painted, veneer])) == 2
    # One shared colour word is not the same material (re-review of #1916).
    laminate = {**veneer, "vendor": "white laminate"}
    assert len(_apply([{**painted, "vendor": "painted white"}, laminate])) == 2


def test_a_big_ai_region_or_the_same_words_alone_never_merge() -> None:
    helper = _item("F24", "not_compared", vendor="D9", architect="D14", v_in="12", a_in="23")
    # The look's big region holds the helper's label, but names no architect label.
    look = _item(
        "F32",
        "likely_problem",
        vendor=None,
        architect=None,
        v_in=None,
        a_in=None,
        vendor_box=_vbox((50.0, 50.0), (300.0, 200.0)),
    )
    assert len(_apply([helper, look])) == 2
    # The same words on two printed labels: two items.
    twin = _item("F49", "not_compared", vendor="D10", architect="D14", v_in="12", a_in="23")
    twin["item"] = helper["item"]
    assert len(_apply([helper, twin])) == 2
    # Two AI boxes that barely overlap with the same value: two items.
    a = _item(
        "A1", "not_compared", vendor=None, architect=None, vendor_box=_vbox((0, 0), (100, 100))
    )
    b = _item(
        "A2", "not_compared", vendor=None, architect=None, vendor_box=_vbox((90, 90), (100, 100))
    )
    assert len(_apply([a, b])) == 2
    # The same value at one spot: one item.
    c = _item(
        "A3", "not_compared", vendor=None, architect=None, vendor_box=_vbox((2, 2), (100, 100))
    )
    assert len(_apply([a, c])) == 1
    # Another page or round: never one item.
    assert len(_apply([helper, {**helper, "id": "F99", "page": 4}])) == 2
    assert len(_apply([{**helper, "round": 1}, {**helper, "id": "F98", "round": 2}])) == 2


def test_a_gv_rule_or_a_vendor_check_never_folds_with_an_architect_comparison() -> None:
    """The review of #1916: GV's rule (run fit: 24" should be 23 1/2") and a reader's comparison
    at the same vendor spot ask two questions; the proved rule Wrong stays red."""
    rule = _item(
        "F3", "clouded", vendor=None, architect=None, v_in="24", a_in="23.5",
        source="rules", rule_check_id="RUN-FIT", witnesses=["rule"], settled=None,
    )  # fmt: skip
    rule.update(architect='23 1/2"', created_by="run_fit")
    reader = _item("F4", "right", vendor=None, architect=None, v_in="24", a_in="24")
    final = _apply([rule, reader])
    assert len(final) == 2
    assert {f["id"] for f in final if "same_item" in f} == set()
    own = {**rule, "id": "F5", "source": "vendor_arithmetic", "rule_check_id": None}
    assert len(_apply([own, reader])) == 2


def test_two_printed_vendor_labels_joined_through_one_spot_stay_apart() -> None:
    one = _item("F1", "not_compared", vendor="D7", architect="D16", at=(100.0, 100.0))
    two = _item("F2", "not_compared", vendor="D8", architect="D16", at=(120.0, 100.0))
    region = _item(
        "F3",
        "likely_problem",
        vendor=None,
        architect="D16",
        vendor_box=_vbox((90.0, 90.0), (60.0, 30.0)),
    )
    assert len(_apply([one, two, region])) == 3


def test_an_architect_value_the_resolver_dropped_never_comes_back_as_a_conflict() -> None:
    """Re-review of #1916: the resolver sets the architect's inches to none on purpose (several
    values); the label's old value is never read back into the card's conflict."""
    merged = _item("F90", "not_compared", a_in="32")
    merged["inches"] = {**merged["inches"], "architect": None}
    right = _item("F91", "right", architect="D9", a_in="36")
    final = _apply([merged, right])
    assert len(final) == 1 and "conflict" not in final[0]["same_item"]


def test_a_copy_that_must_stay_is_never_folded_away() -> None:
    recorded = _item("F1", "right", record_id="r1", trust_level="likely")
    again = _item("F2", "right")
    assert len(_apply([again, recorded])) == 2
    decided = _item("F3", "not_compared", decision={"decision": "not_a_problem"}, a_in="40")
    assert len(_apply([_item("F4", "clouded", a_in="40"), decided])) == 2


KINDS = ("clouded", "likely_problem", "not_compared", "right")


@pytest.mark.parametrize("size", [2, 3])
def test_a_merged_card_is_never_redder_or_greener_than_its_copies(size: int) -> None:
    """Every combination of cards on one vendor label (it prints 36"): a Right copy pairs it with
    an architect 36", the others with an architect 40" or 44"."""
    for kinds in itertools.product(KINDS, repeat=size):
        for numbers in itertools.product(("40", "44"), repeat=size):
            copies = [
                _item(f"F{at}", kind, a_in="36" if kind == "right" else numbers[at])
                for at, kind in enumerate(kinds)
            ]
            architect = {c["inches"]["architect"] for c in copies}
            groups = [proof_group(c) for c in copies]
            final = _apply(copies)
            assert len(final) == 1, (kinds, numbers)
            card = proof_group(final[0])
            assert card in (*groups, "not_compared"), (kinds, numbers, card)
            if len(architect) > 1:
                # The copies pair the one vendor number with different architect numbers.
                assert card == "not_compared", (kinds, numbers, card)
                assert final[0]["same_item"]["conflict"]
            else:
                assert card == min(groups, key=one_card.RANK.__getitem__), (kinds, card)
            if card == "clouded":
                assert "clouded" in groups
            if card == "right":
                assert set(groups) == {"right"}
            assert len(final[0]["same_item"]["copies"]) == size - 1


def test_no_drawing_items_fold_under_drawing_needed_and_the_vendor_checks_stay() -> None:
    nothing = _item(
        "F1",
        "not_compared",
        vendor=None,
        architect=None,
        v_in=None,
        a_in=None,
        vs_page=None,
        arch_box=None,
        status="check",
        keep_reason="architect_prints_nothing",
    )
    nothing.update(vendor="sink model K-1 and full splash", architect="no architect drawing paired")
    own_sum = {
        **nothing,
        "id": "F2",
        "source": "vendor_arithmetic",
        "status": "different",
        "item": "Dimension string",
    }
    result = one_card.fold([nothing, own_sum], (3,))
    assert [f.drop for f in result.folded] == ["F1"] and result.folded[0].keep == "page:3"
    assert result.drawing_needed[3][0]["finding_id"] == "F1"
    # Not a Drawing-needed page: it stays.
    assert one_card.fold([nothing, own_sum], (4,)).folded == []
    # With an architect page or spot, it is not a "no drawing" item.
    assert one_card.fold([{**nothing, "vs_page": 7}], (3,)).folded == []
    # Nor with an architect finish code read.
    tagged = {**nothing, "architect": "PL-1 laminate"}
    assert one_card.fold([tagged], (3,)).folded == []
    # Nor with an architect value read: it says more than the page's card.
    sized = {**nothing, "inches": {"vendor": None, "architect": "36", "difference": None}}
    assert one_card.fold([sized], (3,)).folded == []


def test_every_output_counts_the_meeting_shapes_once() -> None:
    """The four shapes of the meeting set: two Rights, Likely + Not compared, tags, no drawing."""
    findings = [
        _item("F10", "right"),
        _item("F35", "right"),
        _item("F42", "not_compared", vendor="D7", architect="D16", v_in="12", a_in="21.9375"),
        _item(
            "F33",
            "likely_problem",
            vendor=None,
            architect="D16",
            v_in="12",
            a_in="21.9375",
            vendor_box=_vbox((80.0, 60.0), (60.0, 95.0)),
        ),
        _item(
            "F2",
            "not_compared",
            vendor=None,
            architect=None,
            v_in=None,
            a_in=None,
            vs_page=None,
            arch_box=None,
            page=1,
            vendor_box={"page": 1, "x0": 1, "y0": 1, "x1": 9, "y1": 9, "file": VENDOR_FILE},
        ),
    ]
    final = _apply(findings, (1,))
    counts = {k: v for k, v in proof_counts(final).items() if v}
    assert counts == {"right": 1, "likely_problem": 1}
    assert not_compared.rows(final) == []
    assert Counter(f["id"] for f in final) == Counter({"F10": 1, "F33": 1})
    # The letter's "Please check and confirm" and the register: once each.
    from app.review.assistant.send_package import (
        CheckState,
        PackageContext,
        checks_of,
        register_rows,
    )
    from tests.review.assistant.test_send_package import IDENTITY

    assert len(checks_of(final)) == 1
    context = PackageContext(
        identity=IDENTITY, date="2026-10-23", round_no=1, rounds=None, state=CheckState(),
        labels={},
    )  # fmt: skip
    assert len(register_rows(final, context)) == 2
    # Each kept card's copies are valid for the screen.
    for finding in final:
        shown = ChatFindingSameItemOut.model_validate(finding["same_item"])
        assert len(shown.copies) == 1


def test_a_drawing_needed_page_keeps_its_folded_items_on_its_coverage(tmp_path: Any) -> None:
    from app.review.assistant.chat_findings import FindingBook
    from app.review.assistant.sandbox import ChatWorkspace

    workspace = ChatWorkspace.open(tmp_path / "chat")
    book = FindingBook(workspace=workspace, listing=(), page_count=3)
    book.fold_into_page(2, [{"finding_id": "F1", "item": "Kitchen: sink", "vendor": "K-1"}])
    book.set_coverage({"pages": [{"page": 2, "status": "not_comparable", "note": "x"}]}, "code")
    page = next(p for p in book.coverage_map()["pages"] if p["page"] == 2)
    shown = ChatCoveragePageOut.model_validate(page)
    assert shown.folded is not None and shown.folded[0].finding_id == "F1"
