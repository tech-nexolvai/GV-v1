"""The product selection is a strict scope (Anant, 2026-10-18). $0: no AI call, no database.

What is asserted:

- the scope is the pick, validated against the registry's ACTIVE products; nothing picked is the
  set's own product, else every active product; "All" is every active product;
- `other` and unknown categories are in scope only for "All"; a code-made "other" item named with
  one product's words gets that product (a filler check is a cabinet item);
- the finding book never records an out-of-scope finding (a helper is told why), whoever made it:
  a helper, a code step (ledger, vendor consistency, tag reader) or a GV check seeded by its rule's
  product; the count is kept; a category changed to another product is refused;
- the final filter drops any out-of-scope finding block and notes the count in the trust block;
- GV Review's recorded results are cut to the products in scope, by each rule's product;
- an area whose every page names only out-of-scope products gets no helper (fewer helpers, a lower
  fixed price); a page naming no product, or an in-scope one, keeps its area;
- a made-up product added only to the registry works in any two or three with no code change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.review.assistant.chat_service import focus_line
from app.review.assistant.chat_trust import recorded_from
from app.review.assistant.full_check_brief import product_checklist
from app.review.assistant.full_check_plan import plan_jobs
from app.review.assistant.product_scope import (
    ProductScope,
    category_for,
    final_blocks,
    named_products,
    no_rules_note,
    outside_unit,
    refiled,
    scaled_price,
    scoped_document,
    scoped_loader,
)
from app.review.assistant.sandbox import SandboxRefused
from tests.dummy_products import DUMMY, use_dummy_registry
from tests.review.assistant.test_chat_findings import book, finding_input

__all__ = ["use_dummy_registry"]  # the fixture, imported so pytest finds it

COUNTERTOP = ProductScope.of(["countertop"])
CABINET = ProductScope.of(["cabinet"])
BOTH = ProductScope.of(["cabinet", "countertop"])


# ---- the scope -------------------------------------------------------------------------------------


def test_the_scope_is_the_pick_else_the_sets_product_else_every_active_one() -> None:
    assert COUNTERTOP.products == ("countertop",) and not COUNTERTOP.every
    assert BOTH.products == ("countertop", "cabinet") and BOTH.every  # registry order
    assert ProductScope.of(None, "cabinet").products == ("cabinet",)
    assert ProductScope.of(None).products == ("countertop", "cabinet")
    # An inactive or unknown id never widens or narrows anything: it is left out.
    assert ProductScope.of(["lighting", "cabinet", "nope"]).products == ("cabinet",)
    assert ProductScope.of(["lighting"], "countertop").products == ("countertop",)
    assert COUNTERTOP.as_dict() == {
        "products": ["countertop"],
        "all_products": False,
        "products_label": "Countertops",
    }
    assert BOTH.label == "Countertops and Cabinets"


def test_other_and_unknown_categories_are_kept_only_for_all() -> None:
    assert COUNTERTOP.allows("countertop") and not COUNTERTOP.allows("cabinet")
    assert not COUNTERTOP.allows("other") and not COUNTERTOP.allows(None)
    assert not COUNTERTOP.allows("lighting")
    assert BOTH.allows("other") and BOTH.allows("cabinet") and BOTH.allows("anything")


def test_a_products_words_are_read_in_any_spacing() -> None:
    assert named_products("BASE CAB. W/ TOE-KICK") == {"cabinet"}
    assert named_products('toekick 4"') == {"cabinet"}
    assert named_products('QUARTZ TOP W/ 4" BACK SPLASH') == {"countertop"}
    assert named_products("Outlet height") == set()
    # A word two products might share is not a product word: no guess.
    assert named_products("door, base, sink, top") == set()


def test_a_code_made_other_item_gets_its_products_category() -> None:
    code = {"category": "other", "item": "Bussing station: filler width", "created_by": "gv_review"}
    assert category_for(code) == "cabinet"
    assert category_for({**code, "item": "Overall length"}) == "other"
    assert category_for({**code, "item": "filler under the countertop"}) == "other"  # two named
    # A helper was told the products: its own "other" stays its word.
    assert category_for({**code, "created_by": "plan-3"}) == "other"
    assert category_for({**code, "category": "countertop"}) == "countertop"


# ---- the hard filter -----------------------------------------------------------------------------


def _scoped_book(tmp_path: Path, scope: ProductScope) -> Any:
    found = book(tmp_path)
    found.product_scope = scope
    return found


def test_an_out_of_scope_finding_is_never_recorded(tmp_path: Path) -> None:
    found = _scoped_book(tmp_path, COUNTERTOP)

    kept, _notes = found.report(finding_input(), "plan-1")
    with pytest.raises(SandboxRefused, match="cabinets are outside this check"):
        found.report(finding_input(category="cabinet", item="Sink base width"), "plan-2")
    with pytest.raises(SandboxRefused, match="items of no product"):
        found.report(finding_input(category="other", item="Outlet height"), "plan-2")
    # A code step: the vendor consistency check's string, the ledger's sum, the tag reader's row.
    assert (
        found.add_code_finding(
            finding_input(category="other", item="Market: filler string"), created_by="consistency"
        )
        is None
    )
    assert found.add_code_finding(finding_input(category="other", item="Overall")) is None
    tagged = found.add_code_finding(
        finding_input(category="other", item="Vanity quartz top: tag"), created_by="tag_reader"
    )
    assert tagged is not None and tagged["category"] == "countertop"

    assert [f["id"] for f in found.live()] == [kept["id"], tagged["id"]]
    assert found.out_of_scope == 4
    with pytest.raises(SandboxRefused, match="cabinets are outside"):
        found.update({"finding_id": kept["id"], "category": "cabinet"})


def test_gv_checks_are_seeded_by_their_rules_product(tmp_path: Path) -> None:
    """The filler check is a cabinet rule (its rule's product), never "other"."""
    document = {
        "records": {
            "countertops": [
                {"id": "C1", "outcome": "Needs correction", "page": 4, "label": "Countertop run A"}
            ],
            "other_checks": [
                {
                    "id": "F1",
                    "outcome": "Needs correction",
                    "pages": [8],
                    "check": "Filler width check",
                },
                {"id": "F2", "outcome": "Needs correction", "pages": [9], "check": "Unknown check"},
            ],
        },
        "record_products": {"F1": "cabinet"},
    }
    recorded = recorded_from(document)
    assert [d["category"] for d in recorded.differences] == ["countertop", "cabinet", "other"]

    for scope, kept in ((CABINET, ["F1"]), (COUNTERTOP, ["C1"]), (BOTH, ["C1", "F1", "F2"])):
        found = _scoped_book(tmp_path / scope.label, scope)
        for item in recorded.differences:
            found.seed(item)
        assert [f["rule_check_id"] for f in found.live()] == kept, scope.label


def test_the_final_filter_drops_any_out_of_scope_block_and_keeps_the_count() -> None:
    blocks = [
        {"type": "text", "text": "Done."},
        {"type": "finding", "finding": {"id": "F1", "category": "countertop"}},
        {"type": "finding", "finding": {"id": "F2", "category": "cabinet"}},
        {"type": "finding", "finding": {"id": "F3", "category": "other"}},
        {"type": "trust", "trust": {"counts": {}}},
    ]

    kept, dropped = final_blocks(blocks, COUNTERTOP, refused=5)

    assert [b["finding"]["id"] for b in kept if b["type"] == "finding"] == ["F1"]
    assert dropped == 7
    note = kept[-1]["trust"]["product_scope"]
    assert note == {**COUNTERTOP.as_dict(), "dropped_out_of_scope": 7}
    every, none = final_blocks(blocks, BOTH)
    assert len(every) == len(blocks) and none == 0


def test_recorded_results_are_cut_to_the_products_in_scope() -> None:
    document = {
        "records": {
            "countertops": [{"id": "C1"}],
            "other_checks": [{"id": "F1"}, {"id": "F2"}],
        },
        "record_products": {"F1": "cabinet", "F2": "countertop"},
        "ai_page_review": [
            {
                "page": 3,
                "different": [{"key": "countertop depth"}, {"key": "outlet height"}],
                "leads": [{"key": "filler", "result": "unsure"}],
            }
        ],
    }

    cabinet = scoped_document(document, CABINET)
    assert cabinet["records"]["countertops"] == []
    assert cabinet["records"]["other_checks"] == [{"id": "F1"}]
    assert cabinet["ai_page_review"][0]["different"] == []
    assert cabinet["ai_page_review"][0]["leads"] == [{"key": "filler", "result": "unsure"}]
    countertop = scoped_document(document, COUNTERTOP)
    assert countertop["records"]["countertops"] == [{"id": "C1"}]
    assert countertop["ai_page_review"][0]["different"] == [{"key": "countertop depth"}]
    assert scoped_document(document, BOTH) is document
    load = scoped_loader(lambda page: document, CABINET)
    assert load is not None and load(None)["records"]["countertops"] == []


# ---- fewer helpers, a lower price ----------------------------------------------------------------


def _half(role: str) -> dict[str, Any]:
    return {"box": [0, 0, 100, 100], "role": role, "heading": role, "round": None}


def _map(areas: list[tuple[str, int]]) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    for area, count in areas:
        for n in range(count):
            role = "architect" if n == 0 else "vendor"
            pages.append(
                {"file": "V.pdf", "page": len(pages) + 1, "area": area, "halves": [_half(role)]}
            )
    return {"version": 1, "files": ["V.pdf"], "pages": pages}


def test_an_area_of_only_other_products_gets_no_helper() -> None:
    page_map = _map([("1. Market", 2), ("2. Coffee", 2), ("3. Vanity", 2), ("4. Buffet", 2)])
    texts = {
        1: 'FILLER 3/4" SCRIBE',
        2: "SHELF ADJ. FILLER",  # 1. Market: cabinets on every page
        3: "",  # 2. Coffee: a page with no words keeps its area
        4: "FILLER",
        5: "QUARTZ TOP",  # 3. Vanity: a countertop page keeps the area for countertops
        6: "DRAWER",  # no hint of a countertop, but the area has one
        7: "BACKSPLASH",  # 4. Buffet: countertops on every page
        8: "SOLID SURFACE TOP",
    }

    whole = plan_jobs(page_map, "V.pdf", parallel=8)
    counter = plan_jobs(page_map, "V.pdf", 8, outside=lambda p: outside_unit(p, texts, COUNTERTOP))
    cabinet = plan_jobs(page_map, "V.pdf", 8, outside=lambda p: outside_unit(p, texts, CABINET))
    every = plan_jobs(page_map, "V.pdf", 8, outside=lambda p: outside_unit(p, texts, BOTH))

    assert whole is not None and counter is not None and cabinet is not None and every is not None
    assert counter.pages == 6 and {1, 2} == {p for p, _why in counter.skipped}
    assert "only cabinets drawn here" in dict(counter.skipped)[1]
    assert cabinet.pages == 6 and {7, 8} == {p for p, _why in cabinet.skipped}
    assert every.pages == whole.pages == 8
    assert len(counter.jobs) < len(whole.jobs)
    assert scaled_price(5_000_000, counter.pages, whole.pages) == 3_750_000
    assert scaled_price(5_000_000, whole.pages, whole.pages) == 5_000_000
    assert scaled_price(5_000_000, 9, 3) == 5_000_000  # never more than the price


def test_a_hint_of_a_product_in_scope_keeps_its_page() -> None:
    """ "Sink" or "stone" may mean a countertop: such a page is never left out of a countertop
    check, though it names fillers; a hint never makes an item a product's."""
    assert outside_unit([1], {1: "FILLER, STONE TOP"}, COUNTERTOP) is None
    assert outside_unit([1], {1: "FILLER, U/M SINK"}, COUNTERTOP) is None
    assert outside_unit([1], {1: "FILLER"}, COUNTERTOP) is not None
    assert outside_unit([1], {1: "QUARTZ, DOORS BELOW"}, CABINET) is None
    assert named_products("sink") == set() and named_products("sink", hints=True) == {"countertop"}


def test_a_set_where_nothing_is_in_scope_is_still_checked() -> None:
    """Page words are a hint: when they would leave every page out, every page is checked (the
    hard filter still drops the other product's findings)."""
    page_map = _map([("1. Market", 2)])
    texts = {1: "FILLER", 2: "CABINET"}
    plan = plan_jobs(page_map, "V.pdf", 8, outside=lambda p: outside_unit(p, texts, COUNTERTOP))
    assert plan is not None and plan.pages == 2


# ---- what the AI is told ---------------------------------------------------------------------------


def test_the_ai_is_told_the_scope_is_strict() -> None:
    assert "ONLY countertops are checked" in product_checklist(["countertop"])
    assert "Cabinets (category cabinet" not in product_checklist(["countertop"])
    assert "category other" in product_checklist(["countertop", "cabinet"])
    assert "countertops only" in focus_line(["countertop"])
    assert "code drops it" in focus_line(["countertop"])
    assert "(all of them)" in focus_line(["cabinet", "countertop"])


# ---- a new product, no code change ---------------------------------------------------------------


@pytest.mark.usefixtures("use_dummy_registry")
def test_any_two_or_three_products_work_with_a_made_up_product() -> None:
    two = ProductScope.of([DUMMY, "countertop"])
    three = ProductScope.of(["cabinet", DUMMY, "countertop"])

    assert two.products == ("countertop", DUMMY) and not two.every
    assert two.allows(DUMMY) and two.allows("countertop") and not two.allows("cabinet")
    assert not two.allows("other")
    assert three.every and three.allows("other")
    assert two.label == "Countertops and Dummy widgets"
    assert "ONLY countertops and dummy widgets are checked" in product_checklist(two.products)
    assert "Dummy widgets (category dummy_widget" in product_checklist(two.products)
    blocks = [
        {"type": "finding", "finding": {"id": "F1", "category": DUMMY}},
        {"type": "finding", "finding": {"id": "F2", "category": "cabinet"}},
    ]
    kept, dropped = final_blocks(blocks, two)
    assert [b["finding"]["id"] for b in kept] == ["F1"] and dropped == 1
    # The made-up product has no words: its pages are never guessed, so never left out.
    assert outside_unit([1], {1: "WIDGET KNOB"}, ProductScope.of([DUMMY])) is None


def test_a_product_with_no_rules_is_said_plainly() -> None:
    assert no_rules_note([]) is None
    assert (
        no_rules_note(["cabinet"]) == "No GV rules for Cabinets yet: compared against the "
        "architect only."
    )
    assert "Countertops and Cabinets" in str(no_rules_note(["cabinet", "countertop"]))
    assert "No GV rules for Cabinets yet" in focus_line(["cabinet"], ["cabinet"])
    assert "No GV rules" not in focus_line(["cabinet"], ["countertop"])


# ---- re-filing an "other" item before the scope drops it -------------------------------------------


def test_an_other_item_is_refiled_to_its_product_before_it_is_dropped() -> None:
    """A pull size or a finish tag the AI filed as "other" is a cabinet item (orchestrator,
    2026-10-18): one product named = filed there; several = kept when one is picked; a product
    not active yet (a pendant light) is never picked; nothing named = dropped, as before."""
    pull = {"category": "other", "item": "Pull size and laminate frame note"}
    assert refiled(pull, CABINET) == "cabinet" and refiled(pull, COUNTERTOP) is None
    tags = {"category": "other", "item": "Material tags (counter / cabinets)"}
    assert refiled(tags, CABINET) == "cabinet" and refiled(tags, COUNTERTOP) == "countertop"
    light = {"category": "other", "item": "Pendant light count"}
    assert refiled(light, CABINET) is None and refiled(light, COUNTERTOP) is None
    assert refiled(light, BOTH) == "other"  # All drops nothing and re-files no AI item
    plain = {"category": "other", "item": "Outlet height", "why": "drawn at 46 in"}
    assert refiled(plain, CABINET) is None
    # Its other words, then its page's printed words and area title (read only then).
    worded = {**plain, "vendor": "outlet above the filler"}
    assert refiled(worded, CABINET) == "cabinet"
    read: list[int] = []

    def page() -> str:
        read.append(1)
        return "4. Market area  FILLER 3/4 SCRIBE"

    assert refiled(plain, CABINET, page) == "cabinet" and read == [1]
    assert refiled(pull, CABINET, page) == "cabinet" and read == [1]  # its name was enough
    assert refiled(plain, COUNTERTOP, page) is None
    assert refiled(plain, COUNTERTOP, "QUARTZ TOP, FILLER") == "countertop"  # several: kept
    # A finding of a product is never re-filed; an unknown category is treated like "other".
    assert refiled({"category": "cabinet", "item": "Quartz top"}, COUNTERTOP) is None
    assert refiled({"category": "spaceship", "item": "Toe kick"}, CABINET) == "cabinet"


def test_the_book_refiles_with_its_pages_words(tmp_path: Path) -> None:
    found = _scoped_book(tmp_path, CABINET)
    found.page_words = lambda page: {2: "3. Coffee bar  FILLER  TOE KICK"}.get(page, "")

    kept, _notes = found.report(finding_input(category="other", item="Outlet height"), "plan-1")
    assert kept["category"] == "cabinet"
    seeded = found.seed(
        {"record_id": "r9", "page": 2, "what": "AI page review: outlet", "category": "other"}
    )
    assert seeded is not None and seeded["category"] == "cabinet"
    with pytest.raises(SandboxRefused, match="items of no product"):
        found.report(finding_input(page=1, category="other", item="Outlet height"), "plan-1")
    # Changing an item to "other" re-files it too; a product out of scope is still refused.
    changed, _notes = found.update({"finding_id": kept["id"], "category": "other"})
    assert changed["category"] == "cabinet"
    with pytest.raises(SandboxRefused, match="countertops are outside"):
        found.update({"finding_id": kept["id"], "category": "countertop"})
    assert found.out_of_scope == 1
