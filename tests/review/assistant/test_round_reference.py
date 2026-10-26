"""A match with the vendor's own earlier round is never Right (Anant, 2026-10-22), $0.

"If it is not matched with our rules or the architect's drawing, then it is wrong, no matter how
many rounds it took." Made-up values only:

- a round-2 value equal to the vendor's own round-1 value is not Right: Not compared
  (`round_only`), with the round match kept as information ("Unchanged since round 1");
- code compares such an item with the architect instead where it links the vendor's label to an
  architect label (another item on the same vendor label, code's pairs, the earlier round's item);
- the architect printing another size on both rounds is red when code proved the pairing, and the
  round tracker says "still wrong";
- the room-end (bulkhead) guard and the brand-prototype rule hold.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.review.assistant import classification, proof
from app.review.assistant.chat_trust import judged, round_statuses, trust_report
from app.review.assistant.escalation import EscalationContext, escalate
from app.review.assistant.full_check_advocate import Check, decide
from app.review.assistant.round_reference import (
    ROUND_MATCH,
    compare_with_architect,
    round_only,
)

FILE = "set.pdf"
#: Page 7: the architect's drawing (left) and the vendor's round 1 (right); page 8: round 2.
PAGE_MAP = {
    "pages": [
        {
            "file": FILE,
            "page": 7,
            "area": "Lounge cabinet",
            "halves": [
                {"role": "architect", "box": [0, 0, 400, 600], "round": None},
                {"role": "vendor", "box": [400, 0, 800, 600], "round": 1},
            ],
        },
        {
            "file": FILE,
            "page": 8,
            "area": "Lounge cabinet",
            "area_from_page": 7,
            "halves": [{"role": "vendor", "box": [0, 0, 800, 600], "round": 2}],
        },
        {
            "file": FILE,
            "page": 9,
            "area": "Lounge cabinet",
            "halves": [{"role": "brand_prototype", "box": [0, 0, 800, 600], "round": None}],
        },
    ]
}


def _label(
    id_: str, page: int, side: str, inches: str, at: tuple[float, float], text: str | None = None
) -> dict[str, Any]:
    x, y = at
    return {
        "id": id_,
        "page": page,
        "file": FILE,
        "text": text or f'{inches}"',
        "inches": inches,
        "box": [x - 8, y - 4, x + 8, y + 4],
        "side": side,
        "orientation": "v",
        "route": "text",
    }


#: The vendor's round 2 prints 45 11/16" (p8.D4); its round 1 prints the same (p7.D12); the
#: architect prints 3'-5 5/16" (p7.D2) for the same height. Made-up values.
ROUND2 = _label("D4", 8, "vendor", "45.6875", (300, 300), '45 11/16"')
ROUND1 = _label("D12", 7, "vendor", "45.6875", (600, 300), '45 11/16"')
ARCHITECT = _label("D2", 7, "architect", "41.3125", (200, 300), "3'-5 5/16\"")
LABELS = {(FILE, 7): [ROUND1, ARCHITECT], (FILE, 8): [ROUND2]}


def _box(label: dict[str, Any]) -> dict[str, Any]:
    x0, y0, x1, y1 = label["box"]
    return {"page": label["page"], "x0": x0, "y0": y0, "x1": x1, "y1": y1, "file": FILE}


def _dim(label: dict[str, Any]) -> dict[str, Any]:
    return {k: label[k] for k in ("id", "page", "file", "text", "inches", "route", "side", "box")}


def _round_match(**changes: Any) -> dict[str, Any]:
    """A helper's "same": the vendor's round 2 against the vendor's own round 1."""
    finding: dict[str, Any] = {
        "id": "F1",
        "page": 8,
        "vs_page": 7,
        "round": 2,
        "item": "Lounge cabinet: overall height",
        "category": "cabinet",
        "architect": '45 11/16"',
        "vendor": '45 11/16"',
        "status": "same",
        "source": "architect",
        "created_by": "helper:3",
        "why": "floor to top of the unit",
        "compared": True,
        "checks": {
            "printed_architect": "match",
            "printed_vendor": "match",
            "verify_architect": {"result": "match", "route": "text", "read": '45 11/16"'},
            "verify_vendor": {"result": "match", "route": "text", "read": '45 11/16"'},
        },
        "inches": {"architect": "45.6875", "vendor": "45.6875", "difference": "0"},
        "vendor_box": _box(ROUND2),
        "arch_box": _box(ROUND1),
        "dims": {"vendor": _dim(ROUND2), "architect": _dim(ROUND1)},
    }
    finding.update(changes)
    return judged(finding)


def _against_architect(**changes: Any) -> dict[str, Any]:
    """Another item: the same vendor label against the architect's 3'-5 5/16"."""
    finding = _round_match(
        id="F2",
        architect="3'-5 5/16\"",
        status="different",
        inches={"architect": "41.3125", "vendor": "45.6875", "difference": "4.375"},
        arch_box=_box(ARCHITECT),
        dims={"vendor": _dim(ROUND2), "architect": _dim(ARCHITECT)},
    )
    finding.update(changes)
    return judged(finding)


def _link(findings: list[dict[str, Any]], pairs: dict[str, str] | None = None) -> Any:
    return compare_with_architect(
        findings, labels=LABELS, page_map=PAGE_MAP, pairs=pairs or {}, main_file=FILE
    )


def test_a_round_only_match_is_never_right() -> None:
    finding = _round_match()
    # Before anything else: code's own label side says the reference is the vendor's drawing.
    assert round_only(finding)
    assert proof.proof_group(finding) == "not_compared"
    assert proof.proof_of(finding)["not_compared"] == "round_only"
    # Never red either, whatever its values.
    different = _round_match(
        status="different",
        inches={"architect": "43.8125", "vendor": "45.6875", "difference": "1.875"},
    )
    assert proof.proof_group(different) == "not_compared" and not proof.code_proved(different)


def test_the_round_match_is_kept_as_information_and_says_why() -> None:
    result = _link([_round_match()])
    (finding,) = result.changed
    match = finding[ROUND_MATCH]
    assert match["unchanged"] is True and match["round"] == 1 and match["vendor_round"] == 2
    assert match["words"] == "Unchanged since round 1" and match["compared"] is None
    found = proof.proof_of(judged(finding))
    assert found["group"] == "not_compared" and found["not_compared"] == "round_only"
    assert found["reason"].startswith("Unchanged since round 1: compared only with the vendor")
    assert "the architect's value of the lounge cabinet" in found["settle"]
    assert result.marked == 1 and result.linked == {}


def test_a_reference_with_no_label_is_found_on_the_page_map() -> None:
    # A tag-like value typed with its box only: the box sits on the vendor's round-1 drawing.
    finding = _round_match(dims={"vendor": _dim(ROUND2)})
    assert not round_only(finding)  # nothing says so before the page map is read
    result = _link([finding])
    (marked,) = result.changed
    assert round_only(marked) and proof.proof_group(judged(marked)) == "not_compared"
    # A box on the architect's drawing is an architect comparison: nothing changes.
    plain = _round_match(dims={"vendor": _dim(ROUND2)}, arch_box=_box(ARCHITECT))
    assert _link([plain]).changed == [] and proof.proof_group(plain) == "right"


def test_an_item_comparing_the_same_vendor_label_with_the_architect_carries_it() -> None:
    result = _link([_round_match(), _against_architect()])
    assert set(result.absorbed) == {"F1"} and result.linked == {"F1": "carrier"}
    assert "F2" in result.absorbed["F1"]
    (carrier,) = result.changed
    assert carrier["id"] == "F2" and carrier[ROUND_MATCH]["words"] == "Unchanged since round 1"
    assert carrier[ROUND_MATCH]["compared"] == "carrier"


@pytest.mark.usefixtures("read_twice")
def test_the_architect_printing_another_size_on_both_rounds_is_red_and_still_wrong() -> None:
    # Code paired the round-2 label with the architect's by drawn position.
    result = _link([_round_match()], pairs={"p8.D4": "p7.D2"})
    (finding,) = result.changed
    finding = judged(finding)
    assert result.linked == {"F1": "code_pair"} and finding["paired_by"] == "code"
    assert (
        finding["architect"] == "3'-5 5/16\""
        and finding["dims"]["architect"]["side"] == "architect"
    )
    assert finding["status"] == "different" and finding["inches"]["difference"] == "4.375"
    assert proof.proof_group(finding) == "clouded"
    assert finding[ROUND_MATCH]["unchanged"] is True
    # The round tracker: the vendor repeats on round 2 a value the architect does not print.
    assert round_statuses([finding])["F1"] == "still_wrong"


def test_the_architect_printing_the_same_size_makes_it_right() -> None:
    architect = _label("D2", 7, "architect", "45.6875", (200, 300), "3'-9 11/16\"")
    labels = {(FILE, 7): [ROUND1, architect], (FILE, 8): [ROUND2]}
    result = compare_with_architect(
        [_round_match()], labels=labels, page_map=PAGE_MAP, pairs={"p8.D4": "p7.D2"}, main_file=FILE
    )
    finding = judged(result.changed[0])
    assert finding["status"] == "same" and proof.proof_group(finding) == "right"
    assert finding["checks"]["printed_architect"] == "match"


def _earlier(**changes: Any) -> dict[str, Any]:
    """Round 1's own item: p7.D12 against the architect's p7.D2 (a helper's pairing)."""
    return _round_match(
        id="F3",
        page=7,
        round=1,
        architect="3'-5 5/16\"",
        status="different",
        inches={"architect": "41.3125", "vendor": "45.6875", "difference": "4.375"},
        vendor_box=_box(ROUND1),
        arch_box=_box(ARCHITECT),
        dims={"vendor": _dim(ROUND1), "architect": _dim(ARCHITECT)},
        **changes,
    )


def test_an_earlier_round_comparison_code_did_not_prove_gives_no_second_chance() -> None:
    result = _link([_round_match(), _earlier()])
    assert result.linked == {}
    finding = judged(next(f for f in result.changed if f["id"] == "F1"))
    assert finding[ROUND_MATCH]["earlier_item"] == "F3"
    found = proof.proof_of(finding)
    assert found["not_compared"] == "round_only"
    assert "the earlier round's own comparison with the architect (F3) is not proved" in (
        found["reason"]
    )


@pytest.mark.usefixtures("read_twice")
def test_a_proved_earlier_round_links_it_but_a_red_still_needs_the_advocate() -> None:
    proved = _earlier(paired_by="code")
    assert proof.proof_group(proved) == "clouded"
    result = _link([_round_match(), proved])
    assert result.linked == {"F1": "earlier_round"}
    finding = judged(next(f for f in result.changed if f["id"] == "F1"))
    assert finding["status"] == "different" and "paired_by" not in finding
    # Code read both values, but the round-2 pairing is the AI's: no red before the advocate.
    assert proof.proof_group(finding) == "not_compared"
    assert finding[ROUND_MATCH]["via"].startswith("F3 compares p7.D12")


def test_the_bulkhead_guard_holds_for_an_item_compared_with_the_architect_instead() -> None:
    passing = [Check(name, "pass") for name in ("ink", "annotation", "roles", "axis", "round")]
    pick = {
        "pick": "p7.D2",
        "vendor_spans": {"from": "floor", "to": "other"},
        "architect_spans": {"from": "floor", "to": "other"},
        "vendor_ink_black": "yes",
        "strong": True,
    }
    verdict = decide(
        mode="ai_verified",
        checks=passing,
        printed=True,
        helper_pick="p7.D2",
        advocate=pick,
        extent=("cannot_tell", "too few dimensions printed on both drawings to line them up"),
        code_paired=False,
        item="Lounge cabinet: overall height to the bulkhead",
        words="floor to underside of the bulkhead box",
    )
    assert verdict.result == "objects"


def test_the_escalation_runs_it_first_and_drops_the_folded_copy() -> None:
    ctx = EscalationContext(main_file=FILE, labels=LABELS, page_map=PAGE_MAP)
    result = escalate([_round_match(), _against_architect()], ctx)
    assert set(result.absorbed) == {"F1"} and result.round_matches == 1
    assert [f["id"] for f in result.changed].count("F2") == 1


def test_a_match_with_the_brand_prototype_is_right_only_where_it_is_the_reference() -> None:
    brand = _label("D5", 9, "brand", "45.6875", (300, 300), '45 11/16"')
    finding = _round_match(
        source="brand_prototype",
        vs_page=9,
        arch_box=_box(brand),
        dims={"vendor": _dim(ROUND2), "architect": _dim(brand)},
    )
    assert proof.proof_of(finding)["not_compared"] == "prototype_only"
    governs = judged({**finding, "reference": classification.PROTOTYPE_REFERENCE})
    assert proof.proof_group(governs) == "right"
    # A note or a layout read against the prototype, with no label, the same.
    note = _round_match(
        source="brand_prototype", vs_page=9, architect="TWO DOORS", vendor="two doors", inches={}
    )
    note["dims"] = {}
    assert proof.proof_of(note)["not_compared"] == "prototype_only"
    # The "vendor" value on the prototype's own drawing is never a vendor confirmation.
    swapped = _round_match(dims={"vendor": _dim(brand), "architect": _dim(ARCHITECT)})
    assert proof.proof_of(swapped)["not_compared"] == "prototype_only"


def test_code_s_own_vendor_consistency_finding_is_untouched() -> None:
    span = _round_match(
        source="cross_view",
        created_by="consistency",
        reference_side="vendor",
        status="different",
    )
    assert not round_only(span) and _link([span]).changed == []


def test_the_round_summary_counts_unchanged_items_as_information() -> None:
    marked = judged(_link([_round_match()]).changed[0])
    earlier = _against_architect(id="F9", page=7, round=1, round_status="not_in_later_round")
    report = trust_report([marked, earlier], None, None)
    rounds = report["rounds"]
    assert rounds["unchanged"] == 1
    assert "1 unchanged since an earlier round (information only" in rounds["line"]


def test_a_later_round_only_match_never_closes_an_earlier_comment() -> None:
    earlier = _against_architect(id="F9", page=7, round=1)
    later = _round_match()  # the same item words on round 2, against round 1 only
    statuses = round_statuses([earlier, later])
    assert statuses == {"F9": "still_wrong", "F1": "still_wrong"}


def test_a_helper_naming_the_vendor_s_round_1_label_as_the_architect_s_is_told_why() -> None:
    from app.review.assistant.dim_findings import fill_from_labels

    class _Labels:
        def resolve(self, ref: str, file: str, page: int) -> dict[str, Any]:
            return next(
                dict(label)
                for label in [ROUND1, ROUND2, ARCHITECT]
                if label["id"] == ref.split(".")[-1] and label["page"] == page
            )

    raw = {"page": 8, "vs_page": 7, "architect_dim": "D12", "vendor_dim": "D4"}
    _out, dims, notes = fill_from_labels(
        raw,
        _Labels(),  # type: ignore[arg-type]
        vendor_file=FILE,
        architect_file=FILE,
    )
    assert dims is not None and dims["architect"]["side"] == "vendor"
    said = (
        "D12 is on the vendor's own drawing (an earlier round or another view), not the "
        "architect's: a match with it is never counted as right; give the architect's label for "
        "the same dimension"
    )
    assert notes == [said]
