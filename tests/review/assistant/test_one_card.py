"""One item, one card (`one_card`): the full check's last step folds the copies of one item.

- Copies are joined by code facts only: the same printed vendor label, the same value at the same
  spot, the same architect label with the vendor spot on top of it, the same finish tags on the
  same page pair. Words and a big AI region alone never join two items.
- The card kept is the most severe copy (code-proved Wrong, Likely problem, Not compared, Right),
  unchanged; the others are kept inside it as evidence. One vendor number paired with two
  architect numbers is Not compared, with both values, when no copy is more than Not compared;
  with a Wrong, Likely problem or Ask the architect among them, every copy stays its own card.
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


@pytest.mark.usefixtures("read_twice")
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


@pytest.mark.usefixtures("read_twice")
def test_one_vendor_number_with_two_architect_numbers_is_not_compared_with_both() -> None:
    right = _item("F1", "right")
    other = _item("F2", "not_compared", architect="D9", a_in="21")
    final = _apply([right, other])
    assert len(final) == 1
    assert proof_group(final[0]) == "not_compared"
    assert final[0]["same_item"]["conflict"] == ['21"', '36"']
    row = not_compared.rows(final)[0]
    assert "2 different architect sizes" in row.why
    # A code-proved Wrong is never folded into such a card (folding never hides a Wrong): both
    # stay as they were.
    proved = _item("F3", "clouded", a_in="40")
    final = _apply([proved, _item("F4", "right")])
    assert [(f["id"], proof_group(f)) for f in final] == [("F3", "clouded"), ("F4", "right")]
    assert "same_item" not in final[0]
    # Two code-proved Wrongs against two architect dimensions: two red cards.
    final = _apply([proved, _item("F5", "clouded", a_in="38")])
    assert [proof_group(f) for f in final] == ["clouded", "clouded"]
    # A Likely problem with a disagreeing copy stays a Likely problem.
    final = _apply(
        [_item("F6", "likely_problem", a_in="40"), _item("F7", "not_compared", a_in="21")]
    )
    assert sorted(proof_group(f) for f in final) == ["likely_problem", "not_compared"]


@pytest.mark.usefixtures("read_twice")
def test_a_carried_conflict_never_turns_a_card_red_by_folding() -> None:
    """Continue: a card folded before (Not compared, its copies disagreed) and a new code-proved
    copy at the same label. The new copy is never folded into the grey card, and the grey card
    keeps its values."""
    carried = _item("F1", "not_compared", a_in="36")
    carried["same_item"] = {
        "copies": [{"finding_id": "F0", "architect": '40"'}],
        "conflict": ['36"', '40"'],
    }
    assert proof_group(carried) == "not_compared"
    proved = _item("F2", "clouded", a_in="40")
    final = _apply([carried, proved])
    assert sorted((f["id"], proof_group(f)) for f in final) == [
        ("F1", "not_compared"),
        ("F2", "clouded"),
    ]
    # With a Right copy instead, the grey card keeps the copy and its values.
    final = _apply([carried, _item("F3", "right")])
    assert [f["id"] for f in final] == ["F1"]
    assert (
        final[0]["same_item"]["conflict"] == ['36"', '40"']
        and proof_group(final[0]) == "not_compared"
    )


def test_a_check_against_the_vendors_earlier_round_never_folds() -> None:
    proved = _item("F1", "clouded", a_in="40")
    earlier = _item("F2", "not_compared", a_in="38")
    earlier["dims"]["architect"]["side"] = "vendor"  # the "architect" label is the vendor's round 1
    assert not one_card.compares_architect(earlier)
    assert [f["id"] for f in _apply([proved, earlier])] == ["F1", "F2"]
    same = _item("F3", "not_compared", a_in="36", reference_side="vendor")
    assert [f["id"] for f in _apply([_item("F4", "right"), same])] == ["F4", "F3"]


def test_a_chain_of_spots_never_joins_two_vendor_numbers() -> None:
    """A (12") and C (22 1/2") each overlap B (vendor not read) at one architect label: A and C are
    two vendor dimensions, so nothing is folded."""
    a = _item("F1", "not_compared", vendor=None, v_in="12", a_in="21", at=(100.0, 100.0))
    b = _item("F2", "not_compared", vendor=None, v_in=None, a_in="21", at=(104.0, 100.0))
    c = _item("F3", "not_compared", vendor=None, v_in="22.5", a_in="21", at=(108.0, 100.0))
    assert one_card.same_item(a, b) and one_card.same_item(b, c) and not one_card.same_item(a, c)
    assert len(_apply([a, b, c])) == 3


@pytest.mark.usefixtures("read_twice")
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
            worst = min(groups, key=one_card.RANK.__getitem__)
            if len(architect) > 1 and one_card.RANK[worst] < one_card.RANK["not_compared"]:
                # Copies that disagree, one of them Wrong / Likely: nothing folds, no card changes.
                assert [proof_group(f) for f in final] == groups, (kinds, numbers)
                continue
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


# ---- one vendor string that does not add up, one card (2026-10-30) ---------------------------------


def _spot(x0: float, y0: float, x1: float, y1: float) -> dict[str, Any]:
    return {"page": 3, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "file": VENDOR_FILE}


def _ledger_sum(
    fid: str, parts: list[str], total: str, spot: dict[str, Any], **changes: Any
) -> dict[str, Any]:
    """The reading ledger's copy of a vendor string whose parts miss its printed overall: Not
    compared ("vendor's string not verified on the drawing")."""
    finding: dict[str, Any] = {
        "id": fid,
        "page": 3,
        "vs_page": None,
        "item": "Dimension string (elevation)",
        "category": "cabinet",
        "architect": "see the sum",
        "vendor": total,
        "status": "different",
        "why": f"Vendor's parts don't add up: {' + '.join(parts)} is not {total} printed.",
        "vendor_box": spot,
        "source": "vendor_arithmetic",
        "parts": parts,
        "printed_total": total,
        "created_by": "ledger",
        "ledger_ids": ["S1"],
        "checks": {"arithmetic": "does_not_add_up", "printed_vendor": "match"},
        "inches": {"architect": None, "vendor": None, "difference": None},
        "trust_level": "confirmed",
        "witnesses": ["arithmetic"],
    }
    finding.update(changes)
    return finding


def _reviewer_sum(fid: str, words: str, spot: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """A page reviewer's please-check of a vendor string, filed with no parts and no architect
    side (the numbers only in its words): a Likely problem."""
    finding: dict[str, Any] = {
        "id": fid,
        "page": 3,
        "vs_page": None,
        "item": "Front view: width string conflicts with overall",
        "category": "other",
        "architect": "n/a",
        "vendor": "see the drawing",
        "status": "check",
        "why": words,
        "vendor_box": spot,
        "source": "architect",
        "parts": None,
        "printed_total": None,
        "created_by": "page_reviewer",
        "checks": {"printed_architect": "not_a_number", "printed_vendor": "not_a_number"},
        "inches": {"architect": None, "vendor": None, "difference": None},
        "trust_level": "to_check",
        "witnesses": [],
        "whole_look": {"by": "page_reviewer", "proof": "likely_only", "kind": "completeness"},
    }
    finding.update(changes)
    return finding


_FRONT = 'The dimensions 3/4" + 48 1/8" + 2" add up to 50 7/8", but the overall reads 50 3/4".'
_PLAN = 'The dimensions 24 7/8" + 24" + 2" add up to 50 7/8", but the overall reads 50 3/4".'
_FRONT_PARTS = ['3/4"', '48 1/8"', '2"']


def test_the_string_fixtures_are_the_cards_they_say() -> None:
    ledger = _ledger_sum("F20", _FRONT_PARTS, '50 3/4"', _spot(100, 300, 460, 340))
    reviewer = _reviewer_sum("F5", _FRONT, _spot(120, 310, 470, 345))
    assert proof_group(ledger) == "not_compared"
    assert proof_group(reviewer) == "likely_problem"


def test_a_string_the_page_reviewer_and_the_ledger_both_report_is_one_card() -> None:
    """A real full check (2026-10-30): the reviewer's Likely problem and the ledger's Not compared
    copy of one string were two cards. One stays (the Likely problem); the ledger's copy is kept on
    it as evidence, and nothing changes colour."""
    ledger = _ledger_sum("F20", _FRONT_PARTS, '50 3/4"', _spot(100, 300, 460, 340))
    reviewer = _reviewer_sum("F5", _FRONT, _spot(120, 310, 470, 345))
    final = _apply([reviewer, ledger])
    assert [f["id"] for f in final] == ["F5"]
    assert proof_group(final[0]) == "likely_problem"
    copies = final[0]["same_item"]["copies"]
    assert [c["finding_id"] for c in copies] == ["F20"]
    assert copies[0]["group"] == "not_compared"
    ChatFindingSameItemOut.model_validate(final[0]["same_item"])
    folded = one_card.fold([reviewer, ledger]).folded
    assert [(f.keep, f.drop) for f in folded] == [("F5", "F20")]
    assert "same vendor dimension string" in folded[0].reason


def test_a_reviewer_string_that_read_one_more_part_is_the_same_string() -> None:
    """One reader missed a part (the reviewer read 7 parts, the ledger 6): the same overall, one
    list of parts within the other, at the same spot, is one string."""
    parts = ['2 3/4"', '16"', '3/4"', '31 5/8"', '3/4"', '27 1/8"']
    ledger = _ledger_sum("F20", parts, '79 1/2"', _spot(104, 438, 393, 470))
    reviewer = _reviewer_sum(
        "F6",
        "The parts add to 79 3/4, but the overall reads 79 1/2.",
        _spot(100, 440, 455, 482),
        source="vendor_arithmetic",
        parts=[p.rstrip('"') for p in parts] + ["3/4"],
        printed_total="79 1/2",
        status="different",
    )
    assert proof_group(reviewer) == "likely_problem"
    assert [f["id"] for f in _apply([reviewer, ledger])] == ["F6"]


def test_two_open_copies_of_one_string_keep_codes_own() -> None:
    """Both copies Not compared: the ledger's (code's own sum) is the card, the AI's its copy."""
    ledger = _ledger_sum("F20", _FRONT_PARTS, '50 3/4"', _spot(100, 300, 460, 340))
    reviewer = _reviewer_sum("F5", _FRONT, _spot(120, 310, 470, 345), whole_look=None)
    assert proof_group(reviewer) == "not_compared"
    final = _apply([reviewer, ledger])
    assert [f["id"] for f in final] == ["F20"]
    assert [c["finding_id"] for c in final[0]["same_item"]["copies"]] == ["F5"]


def test_two_different_strings_are_never_one_card() -> None:
    """Two strings of one page with the same overall (a plan and a front view), another overall,
    another page or round, words naming other parts, a spot apart: each stays its own card."""
    front = _ledger_sum("F20", _FRONT_PARTS, '50 3/4"', _spot(100, 300, 460, 340))
    # The plan's string at its own spot, with the same overall: two strings.
    plan = _reviewer_sum("F5", _PLAN, _spot(118, 200, 480, 250))
    assert len(_apply([plan, front])) == 2
    # The plan's words at the front view's spot: its parts are not the ledger's.
    assert len(_apply([{**plan, "vendor_box": _spot(120, 310, 470, 345)}, front])) == 2
    # The front view's words, but a spot apart from the ledger's.
    assert len(_apply([_reviewer_sum("F6", _FRONT, _spot(100, 500, 460, 540)), front])) == 2
    # Another overall, another page, another round: never one string.
    other_total = _reviewer_sum("F7", _FRONT.replace("50 3/4", "50 1/2"), front["vendor_box"])
    assert len(_apply([other_total, front])) == 2
    same = _reviewer_sum("F8", _FRONT, _spot(120, 310, 470, 345))
    assert len(_apply([{**same, "page": 4}, front])) == 2
    assert len(_apply([{**same, "round": 1}, {**front, "round": 2}])) == 2
    # Two lists of parts, neither within the other (the same overall): two strings.
    apart = {**same, "source": "vendor_arithmetic", "status": "different"}
    apart.update(parts=["1", "47 7/8", "2"], printed_total="50 3/4")
    assert len(_apply([apart, front])) == 2
    # A ledger string that adds up says nothing is wrong: never folded.
    adds_up = {**front, "id": "F21", "parts": ['3/4"', '48"', '2"']}
    assert len(_apply([same, adds_up])) == 2


def test_a_wrong_is_never_folded_away_by_the_string_fold(monkeypatch: pytest.MonkeyPatch) -> None:
    """A red stays red and stays shown: two red copies both stay; a red ledger copy is the card
    and the reviewer's Likely problem is kept on it as evidence (nothing turns red)."""
    ledger = _ledger_sum("F20", _FRONT_PARTS, '50 3/4"', _spot(100, 300, 460, 340))
    reviewer = _reviewer_sum("F5", _FRONT, _spot(120, 310, 470, 345))
    real = one_card.proof.proof_group
    reds: set[str] = set()

    def group(finding: Any) -> Any:
        return "clouded" if finding.get("id") in reds else real(finding)

    monkeypatch.setattr(one_card.proof, "proof_group", group)
    reds.update({"F5", "F20"})
    assert {f["id"] for f in _apply([reviewer, ledger])} == {"F5", "F20"}
    reds.clear()
    reds.add("F20")
    final = _apply([reviewer, ledger])
    assert [f["id"] for f in final] == ["F20"]
    assert [c["finding_id"] for c in final[0]["same_item"]["copies"]] == ["F5"]
    # A copy that must stay (a recorded difference) is never folded away.
    reds.clear()
    assert len(_apply([reviewer, {**ledger, "record_id": "R1"}])) == 2
