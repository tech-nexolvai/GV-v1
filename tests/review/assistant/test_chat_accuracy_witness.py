"""Chat accuracy rules ($0): a non-AI witness for Confirmed, evidence kinds, not seen is not right,
unknowns as records, the calibrated accuracy score and the "check the rest" work list."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import witness
from app.review.assistant.chat_trust import (
    ACCURACY_POINTS,
    MIN_SEEN_DPI,
    accuracy,
    apply_checks,
    judged,
    ledger_witness,
    mark_compared,
    recorded_from,
    trust_report,
    with_ledger,
)
from app.review.assistant.chat_unknowns import (
    asks_for_the_rest,
    last_unknowns,
    refused_from_diff,
    unknown_records,
    work_list_note,
)
from app.review.assistant.finding_checks import units_check
from app.review.assistant.ledger import ReadingLedger
from tests.review.assistant.test_chat_findings import book


def _checks(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "printed_architect": "not_run",
        "printed_vendor": "not_run",
        "second_read": "not_run",
        "rule": None,
        "arithmetic": None,
        "lead_checked": False,
    }
    return {**base, **changes}


PRINTED = {"printed_architect": "match", "printed_vendor": "match"}


# ---- 1 and 2: evidence kinds and the trust rule ---------------------------------------------------


def test_two_ai_reads_without_a_non_ai_witness_are_likely_at_best() -> None:
    checks = _checks(second_read="agrees", lead_checked=True)
    assert witness.trust_level(checks, "different") == "likely"
    assert witness.evidence_kind(checks, "different") == "inferred"
    assert witness.witnesses(checks, "different") == []


@pytest.mark.parametrize(
    ("changes", "kind", "found"),
    [
        (PRINTED, "observed", ["printed"]),
        ({"rule": "agrees"}, "derived", ["rule"]),
        ({"arithmetic": "does_not_add_up"}, "derived", ["arithmetic"]),
        ({"ledger": "agrees"}, "derived", ["ledger"]),
        ({"units": "agrees"}, "derived", ["units"]),
    ],
)
def test_each_non_ai_witness_lets_an_agreeing_read_confirm(
    changes: dict[str, Any], kind: str, found: list[str]
) -> None:
    for ai in ({"second_read": "agrees"}, {"lead_checked": True}):
        checks = _checks(**changes, **ai)
        assert witness.trust_level(checks, "different") == "confirmed"
        assert witness.evidence_kind(checks, "different") == kind
        assert witness.witnesses(checks, "different") == found
    # The witness alone, with no AI reading agreeing: likely.
    assert witness.trust_level(_checks(**changes), "different") == "likely"


@pytest.mark.parametrize(
    "changes",
    [
        {"second_read": "disagrees"},
        {"rule": "disagrees"},
        {"arithmetic": "adds_up"},  # on a "different" finding
        {"ledger": "disagrees"},
        {"units": "disagrees"},
    ],
)
def test_any_disagreement_is_to_check(changes: dict[str, Any]) -> None:
    checks = _checks(**PRINTED, lead_checked=True, **{"second_read": "agrees", **changes})
    assert witness.trust_level(checks, "different") == "to_check"
    assert witness.disagreement(checks, "different")


def test_pen_strokes_are_neutral() -> None:
    """`no_text` neither backs nor contradicts: two AI reads stay likely, a rule still confirms."""
    pen = {"printed_architect": "no_text", "printed_vendor": "no_text"}
    assert witness.trust_level(_checks(**pen, second_read="agrees", lead_checked=True)) == "likely"
    assert witness.trust_level(_checks(**pen, rule="agrees", second_read="agrees")) == "confirmed"
    one_side = {"printed_architect": "match", "printed_vendor": "no_text"}
    assert witness.evidence_kind(_checks(**one_side)) == "inferred"


def test_judged_stores_kind_and_witnesses() -> None:
    finding = judged({"status": "different", "checks": _checks(**PRINTED, lead_checked=True)})
    assert finding["trust_level"] == "confirmed"
    assert finding["evidence_kind"] == "observed" and finding["witnesses"] == ["printed"]


def test_a_ledger_cross_view_finding_is_derived(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.review.assistant import chat_trust

    monkeypatch.setattr(chat_trust, "load_module", lambda name: None)
    made = apply_checks(
        {
            "status": "different",
            "source": "cross_view",
            "created_by": "ledger",
            "architect": '37"',
            "vendor": '38"',
        },
        {},
        None,
    )
    assert made["checks"]["ledger"] == "agrees" and made["evidence_kind"] == "derived"


@pytest.mark.parametrize(
    ("text", "result"),
    [
        ("940 [37]", "agrees"),
        ("940 (37)", "agrees"),
        ('952 mm [37 1/2"]', "agrees"),
        ("1000 [37]", "disagrees"),
        ('940 [37 1/2"]', "disagrees"),
        ('37"', None),
        ("940 mm", None),
        (None, None),
    ],
)
def test_millimetres_and_inches_printed_together(text: Any, result: str | None) -> None:
    assert units_check(text) == result


def test_the_ledger_backs_a_value_by_code() -> None:
    ledger = ReadingLedger(page_count=10)
    ledger.record_string(
        {"page": 2, "side": "vendor", "parts": ['12"', '25"'], "printed_total": '37"'}
    )
    for page in (1, 3):
        ledger.record_reading(
            {
                "page": page,
                "side": "architect",
                "item": "Vanity top",
                "view": "plan",
                "kind": "width",
                "value": '37"',
            }
        )
    finding = {
        "item": "Vanity top",
        "page": 2,
        "vs_page": 1,
        "status": "same",
        "architect": '37"',
        "vendor": '37"',
        "checks": _checks(lead_checked=True),
    }
    assert ledger_witness(finding, ledger.as_dict()) == "agrees"
    backed = with_ledger(finding, ledger.as_dict())
    assert backed["trust_level"] == "confirmed" and backed["evidence_kind"] == "derived"
    # A value the ledger has nothing for: no witness.
    assert ledger_witness({**finding, "vendor": '38"'}, ledger.as_dict()) is None
    assert ledger_witness(finding, None) is None


# ---- 3: not seen is not right -------------------------------------------------------------------


def _same(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "F1",
        "page": 2,
        "vs_page": 1,
        "item": "Sink centre",
        "status": "same",
        "arch_box": {"page": 1, "x0": 0, "y0": 0, "x1": 9, "y1": 9},
        "vendor_box": {"page": 2, "x0": 0, "y0": 0, "x1": 9, "y1": 9},
        "trust_level": "likely",
        "evidence_kind": "inferred",
        "checks": _checks(lead_checked=True, second_read="agrees"),
    }
    return {**base, **changes}


COVERED = {"pages": [{"page": 2, "status": "same"}], "counts": {"total": 1}}


def test_a_same_item_counts_only_when_seen_and_covered() -> None:
    assert mark_compared([_same()], COVERED) == [
        {**_same(), "compared": True, "not_compared_why": None}
    ]
    unseen = _same(checks=_checks(second_read="agrees"))
    assert mark_compared([unseen], COVERED)[0]["not_compared_why"] == "not_viewed"
    uncovered = {"pages": [{"page": 2, "status": "not_opened"}], "counts": {"total": 1}}
    assert mark_compared([_same()], uncovered)[0]["not_compared_why"] == "pair_not_covered"
    no_pair = _same(arch_box=None, vs_page=None)
    assert mark_compared([no_pair], None)[0]["not_compared_why"] == "pair_not_covered"
    # A difference is never moved.
    assert mark_compared([_same(status="different")], None) == []
    # Already marked the same way: nothing changed.
    marked = {**_same(), "compared": True, "not_compared_why": None}
    assert mark_compared([marked], COVERED) == []


def test_the_seen_log_wins_over_the_leads_own_check() -> None:
    both = [{"page": 1}, {"page": 2}]
    assert mark_compared([_same()], COVERED, seen=both)[0]["compared"] is True
    only_vendor = [{"page": 2}]
    marked = mark_compared([_same()], COVERED, seen=only_vendor)[0]
    assert marked["compared"] is False and marked["not_compared_why"] == "not_viewed"


def _crop(page: int, x0: float, y0: float, x1: float, y1: float, dpi: int = 300) -> dict[str, Any]:
    box = {"x0": x0, "y0": y0, "x1": x1, "y1": y1}
    return {"by": "lead", "file": "set.pdf", "page": page, "box": box, "dpi": dpi}


def test_a_crop_counts_only_when_sharp_and_over_the_box() -> None:
    sharp = [_crop(1, 0, 0, 20, 20), _crop(2, 0, 0, 20, 20)]
    assert mark_compared([_same()], COVERED, seen=sharp)[0]["compared"] is True
    # A whole sheet at overview sharpness (below MIN_SEEN_DPI) is too blurry to read a box by.
    blurry = [_crop(1, 0, 0, 20, 20), _crop(2, 0, 0, 2000, 2000, dpi=100)]
    assert mark_compared([_same()], COVERED, seen=blurry)[0]["not_compared_why"] == "not_viewed"
    assert MIN_SEEN_DPI == 150
    edge = [_crop(1, 0, 0, 20, 20), _crop(2, 0, 0, 20, 20, dpi=MIN_SEEN_DPI)]
    assert mark_compared([_same()], COVERED, seen=edge)[0]["compared"] is True
    # A sharp crop of the right page that misses the box does not count.
    elsewhere = [_crop(1, 0, 0, 20, 20), _crop(2, 300, 300, 400, 400)]
    marked = mark_compared([_same()], COVERED, seen=elsewhere)[0]
    assert marked["not_compared_why"] == "not_viewed"
    # A crop of another file does not count when the box names its file.
    other = _same(vendor_box={"page": 2, "file": "other.pdf", "x0": 0, "y0": 0, "x1": 9, "y1": 9})
    assert mark_compared([other], COVERED, seen=sharp)[0]["not_compared_why"] == "not_viewed"


def test_not_compared_items_are_not_scored_or_counted() -> None:
    right = {**_same(), "compared": True}
    unseen = {**_same(id="F2"), "compared": False, "not_compared_why": "not_viewed"}
    score = accuracy([right, unseen], [], None)
    assert score["claims"] == 1 and score["not_compared"] == 1
    report = trust_report([right, unseen], None, None)
    assert report["counts"]["total"] == 1 and report["not_compared"] == 1
    assert report["evidence"] == {"observed": 0, "derived": 0, "inferred": 1}


# ---- 4: unknowns as records ---------------------------------------------------------------------


def test_unknowns_are_built_by_code_from_every_gap() -> None:
    coverage = {
        "pages": [
            {"page": 1, "status": "same"},
            {"page": 2, "status": "not_opened", "note": None},
            {"page": 3, "status": "not_opened", "note": "cover sheet"},
        ],
        "counts": {"total": 3},
    }
    findings = [
        {**_same(id="F1", page=4), "compared": False, "not_compared_why": "not_viewed"},
        {**_same(id="F2", page=5), "compared": False, "not_compared_why": "pair_not_covered"},
        {
            "id": "F3",
            "page": 6,
            "item": "Fridge opening",
            "status": "different",
            "checks": _checks(printed_vendor="no_text", second_read="unsure"),
        },
        {
            "id": "F4",
            "page": 7,
            "item": "Read by the second read",
            "status": "different",
            "checks": _checks(printed_vendor="no_text", second_read="agrees"),
        },
    ]
    unknowns = unknown_records(
        findings,
        coverage,
        waiting=[{"record_id": "X1", "page": 8, "what": "sink front offset"}],
        refused_diffs=[{"page": 9, "round2_page": 19, "reason": "the drawings do not line up"}],
    )
    assert [(u["id"], u["page"], u["why"], u["scope"]) for u in unknowns] == [
        ("U1", 2, "not_opened", "page"),
        ("U2", 4, "not_opened", "item"),
        ("U3", 5, "pairing_uncertain", "item"),
        ("U4", 6, "unreadable", "item"),
        ("U5", 8, "missing_job_value", "item"),
        ("U6", 9, "refused_alignment", "item"),
    ]
    assert all(u["needs"] and u["what"] for u in unknowns)
    assert "page 19" in unknowns[5]["what"]
    report = trust_report([], coverage, None, unknowns=unknowns)
    assert report["unknowns"]["total"] == 6
    assert report["unknowns"]["by_why"]["not_opened"] == 2


def test_missing_job_values_come_from_the_records() -> None:
    document = {
        "records": {
            "countertops": [
                {"id": "c1", "outcome": "Waiting on a value", "page": 4, "label": "Top"}
            ],
            "other_checks": [
                {"id": "o1", "outcome": "Waiting on a value", "pages": [6], "check": "Filler"},
                {"id": "o2", "outcome": "Looks right", "pages": [7], "check": "Sink"},
            ],
        }
    }
    waiting = recorded_from(document).waiting
    assert [(w["record_id"], w["page"], w["what"]) for w in waiting] == [
        ("c1", 4, "Top"),
        ("o1", 6, "Filler"),
    ]


def test_a_refused_round_diff_becomes_an_input() -> None:
    refused = {"text": "Round 1 p.4 vs round 2 p.9: no", "diff": {"aligned": False, "reason": "x"}}
    assert refused_from_diff(refused) == {
        "page": 4,
        "round2_page": 9,
        "reason": "the drawings do not line up",
    }
    assert refused_from_diff({"diff": {"aligned": True}}) is None
    assert refused_from_diff(refused, {"round1_page": 5})["page"] == 5  # type: ignore[index]


def test_check_the_rest_gets_the_last_unknowns_as_its_work_list() -> None:
    assert asks_for_the_rest("Please check the rest")
    assert asks_for_the_rest("can you look at the remaining pages?")
    assert not asks_for_the_rest("What is the sink width on page 4?")
    older = [{"type": "unknowns", "unknowns": [{"id": "U1", "page": 1}]}]
    newer = [{"type": "text", "text": "x"}, {"type": "unknowns", "unknowns": [{"id": "U9"}]}]
    assert last_unknowns([older, None, newer]) == [{"id": "U9"}]
    note = work_list_note(
        [{"id": "U1", "page": 3, "what": "Page 3", "why": "not_opened", "needs": "open it"}]
    )
    assert note is not None and "U1 page 3: Page 3 (not_opened); needs: open it" in note
    assert work_list_note([]) is None


# ---- 5: the accuracy score ----------------------------------------------------------------------


def test_points_come_from_the_calibrated_table() -> None:
    findings = [
        {"id": "F1", "status": "different", "checks": _checks(**PRINTED, second_read="agrees")},
        {
            "id": "F2",
            "status": "different",
            "checks": _checks(second_read="agrees", lead_checked=True),
        },
        {"id": "F3", "status": "different", "checks": _checks(printed_vendor="no_text")},
        {"id": "F4", "status": "different", "checks": _checks(second_read="disagrees")},
    ]
    findings = [judged(f) for f in findings]
    score = accuracy(findings, [], None)
    points = [item["points"] for item in score["breakdown"]]
    assert points == [
        ACCURACY_POINTS["confirmed"]["observed"],
        ACCURACY_POINTS["likely"]["inferred"],
        ACCURACY_POINTS["to_check"]["inferred"],
        0.0,
    ]
    assert [item["evidence_kind"] for item in score["breakdown"]] == [
        "observed",
        "inferred",
        "inferred",
        "inferred",
    ]
    assert "AI reading only" in score["breakdown"][1]["how"]
    assert "printed on the drawing" in score["breakdown"][0]["how"]
    assert "24 of 24" in score["calibration"]


def test_item_unknowns_lower_the_coverage_factor() -> None:
    finding = judged(
        {"id": "F1", "status": "different", "checks": _checks(**PRINTED, lead_checked=True)}
    )
    item = {"id": "U1", "why": "unreadable", "scope": "item"}
    page = {"id": "U2", "why": "not_opened", "scope": "page"}
    score = accuracy([finding], [], None, [item, page])
    # One claim, one item unknown: half; the page unknown counts through the coverage map only.
    assert score["coverage_factor"] == 0.5 and score["score"] == 50
    assert score["unknowns"] == 2


def test_a_cited_page_count_is_compared_as_a_number(tmp_path: Path) -> None:
    findings = book(tmp_path, pages=3)
    printed = lambda *args: "match"
    assert findings.cite({"value": "3 pages", "fact": "page_count"}, printed)["printed"] == "match"
    assert (
        findings.cite({"value": "3", "fact": "page_count"}, printed)["evidence_kind"] == "derived"
    )
    assert findings.cite({"value": "4 pages", "fact": "page_count"}, printed)["printed"] == (
        "not_found"
    )
    assert findings.cite({"value": "3 of 4", "fact": "page_count"}, printed)["printed"] == (
        "not_found"
    )


def test_findings_stating_a_value_need_its_box() -> None:
    from app.review.assistant.chat_findings import boxless_findings, boxless_note

    box = {"page": 2, "x0": 0, "y0": 0, "x1": 9, "y1": 9}
    findings = [
        {
            "id": "F1",
            "page": 8,
            "item": "Fillers",
            "source": "rules",
            "architect": "1-2",
            "vendor": "3",
        },
        {
            "id": "F2",
            "page": 3,
            "item": "Top",
            "source": "architect",
            "architect": "37",
            "vendor": "38",
            "vendor_box": box,
        },
        {
            "id": "F3",
            "page": 3,
            "item": "Ok",
            "source": "architect",
            "architect": "37",
            "vendor": "37",
            "vendor_box": box,
            "arch_box": box,
        },
        {
            "id": "F4",
            "page": 4,
            "item": "Recorded",
            "created_by": "gv_review",
            "architect": "1",
            "vendor": "2",
        },
        {
            "id": "F5",
            "page": 5,
            "item": "No values",
            "source": "architect",
            "architect": " ",
            "vendor": None,
        },
    ]
    assert boxless_findings(findings) == [
        {"id": "F1", "page": 8, "item": "Fillers", "missing": ["vendor_box"]},
        {"id": "F2", "page": 3, "item": "Top", "missing": ["arch_box"]},
    ]
    note = boxless_note(boxless_findings(findings))
    assert "F1 (page 8: Fillers; needs vendor_box)" in note and "F2" in note


def test_the_third_read_and_the_critic_are_ai_witnesses() -> None:
    """#1498: a third read's agreement and the critic passing count as AI witnesses (never as
    non-AI ones); a third read siding against it, or the critic objecting, is a disagreement."""
    settled = {"second_read": "disagrees", "adjudication": "agrees_after_adjudication"}
    assert witness.trust_level(_checks(**settled, lead_checked=True), "different") == "likely"
    assert witness.trust_level(_checks(**PRINTED, **settled), "different") == "confirmed"
    assert witness.trust_level(_checks(critic="passed", lead_checked=True), "different") == "likely"
    assert witness.trust_level(_checks(**PRINTED, critic="passed"), "different") == "confirmed"
    assert witness.evidence_kind(_checks(critic="passed", lead_checked=True)) == "inferred"
    against = {"second_read": "unsure", "adjudication": "disagrees"}
    assert witness.trust_level(_checks(**PRINTED, **against, lead_checked=True)) == "to_check"
    objects = _checks(**PRINTED, second_read="agrees", critic="objects")
    assert witness.trust_level(objects, "different") == "to_check"
    score = accuracy(
        [
            judged(
                {
                    "id": "F1",
                    "status": "different",
                    "checks": objects,
                    "critic": {"result": "objects", "note": "not the same cabinet"},
                }
            )
        ],
        [],
        None,
    )
    assert score["breakdown"][0]["points"] == 0.0
    assert score["breakdown"][0]["how"] == "the critic objects: not the same cabinet"
