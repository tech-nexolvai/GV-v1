"""The job tracker (#1578), pure code: items linked across rounds and checks, where each stands,
and the vendor scorecard. No database, no AI call."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from app.review.assistant.job_tracker import (
    TrackerAnswer,
    TrackerSet,
    build_tracker,
    item_kind,
    item_words,
    vendor_key,
)

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def box(page: int, x: float = 100.0, y: float = 100.0) -> dict[str, Any]:
    return {"page": page, "x0": x, "y0": y, "x1": x + 40, "y1": y + 12}


def finding(
    fid: str,
    item: str,
    page: int,
    status: str = "different",
    *,
    round: int | None = 1,
    vendor: str = '46"',
    architect: str = '37"',
    category: str = "cabinet",
    source: str = "architect",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "id": fid,
        "item": item,
        "page": page,
        "status": status,
        "round": round,
        "vendor": vendor,
        "architect": architect,
        "category": category,
        "source": source,
        "vendor_box": box(page),
        **extra,
    }


def answer(
    package: UUID,
    findings: Sequence[dict[str, Any]],
    *,
    minutes: int = 0,
    kind: str = "full_check",
) -> TrackerAnswer:
    return TrackerAnswer(
        package_id=package,
        chat_id=uuid4(),
        message_id=uuid4(),
        at=T0 + timedelta(minutes=minutes),
        kind=kind,  # type: ignore[arg-type]
        title="Full check" if kind == "full_check" else "A question",
        stopped=None,
        findings=findings,
    )


def one_set(vendor: str = "Acme Millwork") -> tuple[UUID, list[TrackerSet]]:
    package = uuid4()
    return package, [TrackerSet(package, vendor, vendor, "cabinet")]


def by_item(built: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["item"]: item for item in built["items"]}


def test_two_rounds_in_one_check_fixed_still_wrong_new_and_right_only() -> None:
    package, sets = one_set()
    built = build_tracker(
        sets,
        [
            answer(
                package,
                [
                    finding("F1", "Sink base width", 2),
                    finding("F2", "Filler at wall end", 2),
                    finding("F3", "Toe kick height", 2, "same"),
                    finding("F4", "Sink base width", 3, "same", round=2),
                    finding("F5", "Filler at wall end", 3, round=2),
                    finding("F6", "Outlet height", 3, round=2),
                    finding("F7", "Toe kick height", 3, "same", round=2),
                ],
            )
        ],
    )
    items = by_item(built)
    assert set(items) == {"Sink base width", "Filler at wall end", "Outlet height"}
    sink = items["Sink base width"]
    assert (sink["state"], sink["fixed_round"], sink["rounds"]) == ("fixed", 2, [1, 2])
    assert sink["first_round"] == 1 and sink["round_now"] == 2
    filler = items["Filler at wall end"]
    assert (filler["state"], filler["open_kind"], filler["tags"]) == (
        "open",
        "wrong",
        ["Still wrong"],
    )
    outlet = items["Outlet height"]
    assert outlet["state"] == "open" and outlet["tags"] == ["New in round 2"]
    # The click opens the newest finding.
    assert filler["link"]["finding_id"] == "F5"
    assert [h["finding_id"] for h in filler["history"]] == ["F2", "F5"]
    the_set = built["sets"][0]
    assert [(r["round"], r["open"], r["wrong"], r["fixed"]) for r in the_set["rounds"]] == [
        (1, 2, 2, 0),
        (2, 2, 2, 1),
    ]
    assert built["totals"]["open"] == 2 and built["totals"]["fixed"] == 1


def test_same_words_twice_in_a_round_stay_two_items_paired_by_page() -> None:
    package, sets = one_set()
    built = build_tracker(
        sets,
        [
            answer(
                package,
                [
                    finding("F1", "Counter overall width", 15),
                    finding("F2", "Counter overall width", 40),
                    finding("F3", "Counter overall width", 16, "same", round=2),
                    finding("F4", "Counter overall width", 41, round=2),
                ],
            )
        ],
    )
    states = sorted((i["page"], i["state"]) for i in built["items"])
    assert states == [(16, "fixed"), (41, "open")]


def test_later_checks_link_by_record_label_spot_or_words_on_the_same_page() -> None:
    package, sets = one_set()
    first = answer(
        package,
        [
            finding("F1", "Buffet outlet height", 4, record_id="R-7"),
            finding("F2", "Cooler enclosure height", 41, dims={"vendor": {"id": "D3", "page": 41}}),
            finding("F3", "Connection counter width", 3),
            finding("F4", "Vanity depth", 6),
        ],
    )
    second = answer(
        package,
        [
            # Other words each time: the record, the label, the spot carry the identity.
            finding("F1", "Outlet on the buffet", 4, record_id="R-7"),
            finding(
                "F2",
                "Enclosure overall height",
                41,
                dims={"vendor": {"id": "D3", "page": 41}},
                vendor_box=box(41, 400, 400),
            ),
            finding("F3", "Counter width at connection", 3),
            finding("F4", "Vanity depth", 6, vendor_box=box(6, 500, 500)),
            # Same words, another page: another item.
            finding("F5", "Vanity depth", 8),
        ],
        minutes=60,
    )
    built = build_tracker(sets, [second, first])
    assert built["totals"]["items"] == 5
    assert sorted(i["checks_seen"] for i in built["items"]) == [1, 2, 2, 2, 2]
    newest = {i["page"]: i["item"] for i in built["items"]}
    assert newest[4] == "Outlet on the buffet"


def test_the_latest_decision_wins_and_not_a_problem_names_the_person() -> None:
    package, sets = one_set()
    early = {"decision": "agree", "by": "reviewer-one", "at": T0 + timedelta(hours=1)}
    late = {"decision": "not_a_problem", "by": "reviewer-two", "at": T0 + timedelta(hours=2)}
    built = build_tracker(
        sets,
        [
            answer(package, [finding("F1", "Sink base width", 2, decision=early)]),
            answer(package, [finding("F1", "Sink base width", 2, decision=late)], minutes=30),
        ],
    )
    (item,) = built["items"]
    assert (item["state"], item["decision"], item["decided_by"]) == (
        "not_a_problem",
        "not_a_problem",
        "reviewer-two",
    )
    assert built["totals"]["not_a_problem"] == 1 and built["totals"]["open"] == 0
    assert built["vendors"][0]["mistakes"] == 0


def test_a_stored_round_status_wins_in_both_spellings() -> None:
    package, sets = one_set()
    built = build_tracker(
        sets,
        [
            answer(
                package,
                [
                    # compare_rounds' re-read (today's word) and the round-2 check's own words.
                    finding("F1", "Sink base width", 2, round_status="fixed_in_later_round"),
                    finding("F2", "Filler at wall end", 2, round_status="fixed"),
                    finding("F3", "Outlet height", 2, round_status="still_wrong"),
                    finding("F4", "Drawer count", 5, round=2, round_status="new"),
                ],
            )
        ],
    )
    items = by_item(built)
    assert items["Sink base width"]["state"] == "fixed"
    assert items["Sink base width"]["fixed_round"] == 2
    assert items["Filler at wall end"]["fixed_round"] == 2
    assert items["Outlet height"]["tags"] == ["Still wrong"]
    assert "New in round 2" in items["Drawer count"]["tags"]


def test_a_folded_round_one_page_and_a_right_round_two_carrier_is_fixed() -> None:
    package, sets = one_set()
    carrier = finding(
        "F9",
        "Market cooler enclosure height",
        42,
        "same",
        round=2,
        also_on=[{"finding_id": "F8", "page": 41, "round": 1, "item": "Cooler height"}],
    )
    built = build_tracker(sets, [answer(package, [carrier])])
    (item,) = built["items"]
    assert (item["state"], item["fixed_round"], item["rounds"]) == ("fixed", 2, [1, 2])
    assert [h["folded"] for h in item["history"]] == [True, False]
    assert item["link"]["finding_id"] == "F9"


def test_right_on_a_later_check_of_the_same_round_is_not_a_vendor_fix() -> None:
    package, sets = one_set()
    built = build_tracker(
        sets,
        [
            answer(package, [finding("F1", "Sink base width", 2)]),
            answer(package, [finding("F1", "Sink base width", 2, "same")], minutes=5),
        ],
    )
    (item,) = built["items"]
    assert item["state"] == "right_later" and item["fixed_round"] is None
    assert built["vendors"][0]["mistakes"] == 0


def test_a_right_that_was_not_compared_proves_nothing() -> None:
    package, sets = one_set()
    built = build_tracker(
        sets,
        [
            answer(
                package,
                [
                    finding("F1", "Sink base width", 2),
                    finding("F2", "Sink base width", 3, "same", round=2, compared=False),
                ],
            )
        ],
    )
    (item,) = built["items"]
    assert item["state"] == "open" and item["round_now"] == 1


def test_a_finding_with_no_round_takes_its_pages_round() -> None:
    package, sets = one_set()
    built = build_tracker(
        sets,
        [
            answer(package, [finding("F1", "Sink base width", 3, round=2)]),
            answer(
                package,
                [finding("F1", "Sink base width", 3, "same", round=None)],
                minutes=10,
                kind="question",
            ),
        ],
    )
    (item,) = built["items"]
    assert item["rounds"] == [2] and item["state"] == "right_later"
    assert [c["kind"] for c in built["sets"][0]["checks"]] == ["question", "full_check"]


def test_vendor_scorecard_counts_kinds_fixes_and_open_per_round() -> None:
    first, second = uuid4(), uuid4()
    sets = [
        TrackerSet(first, "Set A", "Acme Millwork", "cabinet"),
        TrackerSet(second, "Set B", "  ACME   millwork ", "countertop"),
        TrackerSet(uuid4(), "Never checked", "Other Co", None),
    ]
    built = build_tracker(
        sets,
        [
            answer(
                first,
                [
                    finding("F1", "Bench overall width", 2),
                    finding("F2", "Outlet height", 2),
                    finding("F3", "Bench overall width", 3, "same", round=2),
                    finding("F4", "Outlet height", 3, round=2),
                ],
            ),
            answer(
                second,
                [
                    finding(
                        "F1", "Countertop overall width", 7, category="countertop", source="rules"
                    )
                ],
                minutes=30,
            ),
        ],
    )
    (vendor,) = built["vendors"]
    assert vendor["vendor_name"] == "Acme Millwork" and vendor["sets"] == 2
    assert vendor["mistakes"] == 3 and vendor["open"] == 2
    assert (vendor["fixed_next_round"], vendor["fixed_next_of"], vendor["fixed_next_share"]) == (
        1,
        2,
        "50",
    )
    # Set A round 1: 2 open, round 2: 1 open; set B round 1: 1 open.
    assert vendor["average_open_per_round"] == "1.3"
    assert vendor["by_kind"][0] == {"label": "Overall size", "count": 2, "share": "67"}
    assert {r["label"] for r in vendor["by_product"]} == {"Cabinets", "Countertops"}
    assert {r["label"] for r in vendor["by_check"]} == {"Against the architect", "GV rule"}
    # One open each: the most recently checked first.
    assert [s["name"] for s in built["sets"]] == ["Set B", "Set A"]


def test_words_helpers() -> None:
    assert vendor_key("  ACME   Millwork ") == "acme millwork"
    assert vendor_key(None) == ""
    assert item_kind({"item": "Buffet outlet height (floor to CL)"}) == "Height"
    assert item_kind({"item": "Vanity finish tag"}) == "Tag, label or note"
    assert item_kind({"item": "Something"}) == "Other dimension"


def test_an_item_named_only_by_a_record_id_reads_as_its_reason() -> None:
    named = {
        "item": "air:7ef41808-eadf-478e-b7ae-116ed46d2ff3",
        "why": 'Vendor width is 39" vs 42". Checked again.',
    }
    assert item_words(named) == 'Vendor width is 39" vs 42"'
    assert item_words({"item": "air:7ef41808-eadf"}) == "GV Review's recorded difference"
    run_on = {
        "item": "air:00540b6f-5703",
        "why": "vendor 16 in vs 20 in Checked again by the evaluator: x.",
    }
    assert item_words(run_on) == "Vendor 16 in vs 20 in"
    assert item_words({"item": "  Sink   base width "}) == "Sink base width"


def test_nothing_found_is_an_empty_tracker() -> None:
    package, sets = one_set()
    built = build_tracker(sets, [])
    assert built["items"] == [] and built["sets"] == [] and built["vendors"] == []
    assert built["totals"]["checks"] == 0
    only_right = build_tracker(sets, [answer(package, [finding("F1", "Width", 2, "same")])])
    assert only_right["items"] == [] and only_right["sets"][0]["items"] == 0
